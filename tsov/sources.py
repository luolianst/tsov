"""全局音源库（S4.1）：sources.json + 扫描 + 稳定 id + 缺失/relink。

设计口径（实时引擎任务书 S4）：
- **全局库**：本机 ``sources.json``（默认仓库根；``TSOV_SOURCES_PATH`` 可覆盖），
  不写入工程目录；路径/收藏/最近是**本机**状态。
- **稳定 id**：
  - VST3：优先读取 bundle ``Contents/Resources/moduleinfo.json`` 的 ``CID`` →
    ``vst3:<CID>``（移动 bundle 不变）；无 moduleinfo 时退回路径指纹（可扫描，但 relink 能力受限）。
  - SFZ/SF2：文件内容 sha256 前 16 位 → ``sfz:<hash>`` / ``sf2:<hash>``（移动文件不变）。
- **增量扫描**：按 size+mtime 复用旧记录；内容变化/强制扫描才重算哈希。
- **缺失**：旧记录在扫描根下找不到 → ``missing=True`` 保留（供 relink），不静默删除。
- **relink**：显式把缺失记录绑到新路径；VST3 校验 class id，SFZ/SF2 校验内容哈希。

本模块只做数据/文件层，不依赖 FastAPI / host；mount 命令层与 UI 另行消费。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .core.atomic import write_json_atomic

SCHEMA_VERSION = 1
GLOBAL_FILE = "sources.json"
_REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_ROOTS = [
    "D:/VST3",
    "D:/Sound source",
    "D:/VPO",
    "vendor/vst3",
    "vendor/soundfonts",
]

_KIND_BY_SUFFIX = {".vst3": "vst3", ".sfz": "sfz", ".sf2": "sf2", ".sf3": "sf2"}


class SourceError(RuntimeError):
    """音源库操作失败（可读信息，调用方转 HTTP 400 / 工具错误）。"""


def default_sources_path() -> Path:
    env = (os.environ.get("TSOV_SOURCES_PATH") or "").strip()
    if env:
        return Path(env)
    return _REPO_ROOT / GLOBAL_FILE


def _norm_path(path: str | Path) -> str:
    try:
        return str(Path(path).expanduser().resolve())
    except (OSError, RuntimeError):
        return str(path)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _vst3_meta(bundle: Path) -> tuple[str, str]:
    """bundle → (class_id, display_name)；moduleinfo 缺失/非法返回空串。"""
    candidates = (
        bundle / "Contents" / "Resources" / "moduleinfo.json",
        bundle / "Contents" / "moduleinfo.json",
        bundle / "moduleinfo.json",
    )
    for path in candidates:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # VST3 moduleinfo 常带尾随逗号，不强行 json.loads；CID 是固定 32 hex（或更长）
        m = re.search(r'"CID"\s*:\s*"([0-9A-Fa-f]+)"', text)
        cid = m.group(1).upper() if m else ""
        m2 = re.search(r'"Name"\s*:\s*"([^"]+)"', text)
        title = m2.group(1).strip() if m2 else ""
        if cid:
            return cid, title
    return "", ""


def _is_vst3_bundle(path: Path) -> bool:
    """只要外层 *.vst3；跳过 Contents/... 里的内层二进制。"""
    if not path.is_dir():
        return False
    return not any(part.lower() == "contents" for part in path.parts)


@dataclass
class Source:
    id: str
    kind: str                         # vst3 / sfz / sf2
    name: str
    path: str                         # 绝对路径（本机）
    root: str = ""                    # 发现它的扫描根（相对/绝对原样）
    class_id: str = ""
    content_hash: str = ""
    size: int = 0
    mtime: float = 0.0
    missing: bool = False
    favorite: bool = False
    last_seen: float = 0.0
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Source":
        return cls(
            id=str(data.get("id") or ""),
            kind=str(data.get("kind") or ""),
            name=str(data.get("name") or ""),
            path=str(data.get("path") or ""),
            root=str(data.get("root") or ""),
            class_id=str(data.get("class_id") or ""),
            content_hash=str(data.get("content_hash") or ""),
            size=int(data.get("size") or 0),
            mtime=float(data.get("mtime") or 0.0),
            missing=bool(data.get("missing")),
            favorite=bool(data.get("favorite")),
            last_seen=float(data.get("last_seen") or 0.0),
            tags=[str(x) for x in (data.get("tags") or [])],
        )


class SourceLibrary:
    """本机全局音源库（读写 sources.json；不缓存扫描结果到模块全局）。"""

    def __init__(self, path: str | Path | None = None, roots: list[str] | None = None):
        self.path = Path(path) if path is not None else default_sources_path()
        self.roots = list(roots) if roots is not None else list(DEFAULT_ROOTS)
        self.sources: dict[str, Source] = {}
        self.recent: list[str] = []
        self.errors: list[str] = []
        self.load()

    # ---- 持久化 ----

    def load(self) -> "SourceLibrary":
        data = _read_json(self.path)
        raw_roots = data.get("roots")
        if not self.roots and isinstance(raw_roots, list):
            self.roots = [str(x) for x in raw_roots if str(x).strip()]
        if not self.roots:
            self.roots = list(DEFAULT_ROOTS)
        self.sources = {}
        for item in data.get("sources") or []:
            if not isinstance(item, dict):
                continue
            src = Source.from_dict(item)
            if src.id:
                self.sources[src.id] = src
        self.recent = [str(x) for x in (data.get("recent") or []) if str(x) in self.sources]
        self.errors = [str(x) for x in (data.get("errors") or [])]
        return self

    def save(self) -> None:
        """原子写 sources.json（本机档；不含工程数据）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "roots": list(self.roots),
            "sources": [self.sources[k].to_dict() for k in sorted(self.sources)],
            "recent": list(self.recent[:30]),
            "errors": list(self.errors[-50:]),
        }
        write_json_atomic(self.path, payload, indent=1)

    # ---- 路径 / 查找 ----

    def _root_path(self, root: str) -> Path:
        p = Path(root)
        return p if p.is_absolute() else (_REPO_ROOT / p)

    def get(self, source_id: str) -> Source:
        src = self.sources.get(source_id)
        if src is None:
            raise SourceError(f"音源不存在：{source_id}")
        return src

    def search(self, query: str = "", kind: str | None = None,
               favorites_only: bool = False, limit: int = 200) -> list[Source]:
        q = str(query or "").strip().lower()
        rows: list[Source] = []
        for src in self.sources.values():
            if kind and src.kind != kind:
                continue
            if favorites_only and not src.favorite:
                continue
            blob = " ".join((src.name, src.path, src.class_id, " ".join(src.tags))).lower()
            if q and q not in blob:
                continue
            rows.append(src)
        recent_rank = {sid: i for i, sid in enumerate(self.recent)}
        rows.sort(key=lambda s: (
            0 if s.favorite else 1,
            0 if not s.missing else 1,
            recent_rank.get(s.id, 9999),
            s.name.lower(),
            s.id,
        ))
        return rows[:max(0, int(limit))]

    def mark_recent(self, source_id: str) -> None:
        self.get(source_id)
        self.recent = [source_id] + [x for x in self.recent if x != source_id]
        self.recent = self.recent[:30]

    def set_favorite(self, source_id: str, on: bool = True) -> Source:
        src = self.get(source_id)
        src.favorite = bool(on)
        return src

    # ---- 扫描 ----

    def _discover(self, root: Path) -> list[tuple[str, Path]]:
        """发现 root 下的 vst3 bundle / sfz / sf2；返回 (kind, path)。"""
        found: list[tuple[str, Path]] = []
        if not root.is_dir():
            self.errors.append(f"扫描根不存在：{root}")
            return found
        try:
            entries = root.rglob("*")
            for path in entries:
                suffix = path.suffix.lower()
                kind = _KIND_BY_SUFFIX.get(suffix)
                if kind == "vst3":
                    if _is_vst3_bundle(path):
                        found.append(("vst3", path))
                elif kind in ("sfz", "sf2") and path.is_file():
                    found.append((kind, path))
        except OSError as exc:
            self.errors.append(f"扫描失败：{root}（{exc}）")
        return found

    def _identity(self, kind: str, path: Path, old: Source | None = None,
                  force: bool = False) -> tuple[str, str, str, int, float]:
        """返回 (id, class_id, content_hash, size, mtime)；能复用旧哈希时复用。"""
        st = path.stat()
        size, mtime = int(st.st_size), float(st.st_mtime)
        if (old is not None and not force and old.size == size
                and abs(old.mtime - mtime) < 1e-6 and old.id):
            # 只复用“内容身份”字段；名称/路径由调用方更新
            return old.id, old.class_id, old.content_hash, size, mtime
        if kind == "vst3":
            cid, title = _vst3_meta(path)
            name = title or path.stem
            if cid:
                return f"vst3:{cid.lower()}", cid, "", size, mtime
            # 无 moduleinfo：路径指纹兜底（同一路径稳定；移动后无法自动认亲）
            digest = hashlib.sha1(_norm_path(path).lower().encode("utf-8")).hexdigest()[:16]
            return f"vst3:path-{digest}", "", "", size, mtime
        digest = _file_hash(path)
        return f"{kind}:{digest[:16]}", "", digest, size, mtime

    def _display_name(self, kind: str, path: Path) -> str:
        if kind == "vst3":
            _cid, title = _vst3_meta(path)
            if title:
                return title
        return path.stem or path.name

    def scan(self, roots: list[str] | None = None, *, force: bool = False) -> dict:
        """增量扫描并原子落盘；返回统计（added/updated/missing/total/errors）。"""
        if roots is not None:
            self.roots = [str(r) for r in roots if str(r).strip()]
        self.errors = []
        old_by_path = {_norm_path(s.path): s for s in self.sources.values()}
        old_by_id = dict(self.sources)
        discovered: dict[str, Source] = {}
        added = updated = 0
        for root_s in self.roots:
            root = self._root_path(root_s)
            for kind, path in self._discover(root):
                ap = _norm_path(path)
                old = old_by_path.get(ap)
                try:
                    sid, cid, chash, size, mtime = self._identity(kind, path, old, force)
                except OSError as exc:
                    self.errors.append(f"读取失败：{path}（{exc}）")
                    continue
                name = self._display_name(kind, path)
                if sid in discovered:
                    # 同 class id 的多个安装：保留第一条，记录在 errors 便于排查
                    self.errors.append(f"重复音源 id {sid}：{path}（保留 {discovered[sid].path}）")
                    continue
                prior = old or old_by_id.get(sid)   # 同内容移动路径：id 不变，继承收藏
                if prior is None:
                    added += 1
                    src = Source(id=sid, kind=kind, name=name, path=ap, root=root_s,
                                 class_id=cid, content_hash=chash, size=size, mtime=mtime,
                                 last_seen=_now())
                else:
                    updated += 1
                    src = Source(id=sid, kind=kind, name=name, path=ap, root=root_s,
                                 class_id=cid, content_hash=chash, size=size, mtime=mtime,
                                 missing=False, favorite=prior.favorite, last_seen=_now(),
                                 tags=list(prior.tags))
                discovered[sid] = src
        # 保留未发现的旧记录并标 missing（不删；供 relink）
        for sid, old in self.sources.items():
            if sid not in discovered:
                old.missing = True
                old.last_seen = old.last_seen or _now()
                discovered[sid] = old
        self.sources = discovered
        self.recent = [x for x in self.recent if x in self.sources]
        self.save()
        missing = sum(1 for s in self.sources.values() if s.missing)
        return {
            "added": added, "updated": updated, "missing": missing,
            "total": len(self.sources), "errors": list(self.errors),
        }

    # ---- relink ----

    def relink(self, source_id: str, new_path: str | Path) -> Source:
        """把缺失/移动的音源绑到新路径；身份不符则拒绝（不偷换音色）。"""
        src = self.get(source_id)
        p = Path(new_path).expanduser()
        if not p.exists():
            raise SourceError(f"relink 路径不存在：{p}")
        kind = src.kind
        if kind == "vst3":
            if not _is_vst3_bundle(p):
                raise SourceError(f"relink 目标不是 VST3 bundle：{p}")
            cid, title = _vst3_meta(p)
            if src.class_id and cid and cid.upper() != src.class_id.upper():
                raise SourceError(f"relink 身份不符：class id {cid} ≠ {src.class_id}")
            src.path, src.class_id = _norm_path(p), cid or src.class_id
            src.name = title or p.stem
        elif kind in ("sfz", "sf2"):
            if not p.is_file() or _KIND_BY_SUFFIX.get(p.suffix.lower()) != kind:
                raise SourceError(f"relink 目标类型不符：{p}（期望 {kind}）")
            if src.content_hash:
                actual = _file_hash(p)
                if actual != src.content_hash:
                    raise SourceError("relink 内容哈希不符（拒绝偷换音源）")
                src.content_hash = actual
            src.path = _norm_path(p)
            src.name = p.stem
        else:
            raise SourceError(f"未知音源类型：{kind}")
        st = p.stat()
        src.size, src.mtime, src.missing = int(st.st_size), float(st.st_mtime), False
        src.last_seen = _now()
        self.save()
        return src

    def summary(self) -> dict:
        counts = {"vst3": 0, "sfz": 0, "sf2": 0}
        missing = 0
        for s in self.sources.values():
            counts[s.kind] = counts.get(s.kind, 0) + 1
            missing += 1 if s.missing else 0
        return {
            "schema_version": SCHEMA_VERSION,
            "path": str(self.path),
            "roots": list(self.roots),
            "counts": counts,
            "missing": missing,
            "total": len(self.sources),
            "errors": list(self.errors[-20:]),
        }


def _now() -> float:
    import time
    return time.time()


def load_library(path: str | Path | None = None) -> SourceLibrary:
    return SourceLibrary(path=path)
