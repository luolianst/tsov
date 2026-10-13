"""会话资产存储层（批I 段2）：meta 旁车 / 绑定持久化（热载 L2）/ 懒计算 / 原子写 / 软删。

- `<id>.meta.json`   逐会话元数据（schema v1）；旧会话**懒计算不写盘**，任何写动作时物化
- `_current.json`    工程 → 当前会话绑定；服务重启后 /api/chat 懒读恢复 = 热载 L2
- `.trash/`          软删落点（jsonl + meta 一并移入；无 UI 恢复，人工可捞）

边界：仅会话域资产；JSONL 本体零改动（ADR-0012）；不进 score 事务（ADR-0017，非工程数据）。
目录经 `config.AGENT_SESSION_DIR` **动态**读取（测试打桩点，勿缓存为常量）。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from . import config

META_SUFFIX = ".meta.json"
CURRENT_NAME = "_current.json"
TRASH_DIR = ".trash"
TITLE_MAX = 40        # 用户改名上限
AUTO_TITLE_MAX = 24   # P14 自动短名（首条用户消息截断）
LIST_LIMIT = 80


def _session_dir() -> Path:
    return Path(config.AGENT_SESSION_DIR)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _atomic_write(path: Path, text: str) -> None:
    """临时文件 + os.replace：杀服瞬间不半写。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001 —— 损坏/缺失一律当无
        return None


def _read_jsonl(path: Path) -> list[dict]:
    msgs: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
                if isinstance(m, dict):
                    msgs.append(m)
            except Exception:  # noqa: BLE001 —— 逐行容错
                pass
    except Exception:  # noqa: BLE001
        pass
    return msgs


def _clean_title(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _derive_from_jsonl(path: Path) -> dict:
    """从 JSONL 现况推导：标题（首条用户消息截断）/ 轮数 / 时间。"""
    msgs = _read_jsonl(path)
    turns = sum(1 for m in msgs if m.get("role") == "user")
    title = None
    for m in msgs:
        if m.get("role") == "user" and (m.get("content") or "").strip():
            title = _clean_title(str(m.get("content")))[:AUTO_TITLE_MAX] or None
            break
    ts = None
    for m in msgs:
        if m.get("ts"):
            ts = str(m["ts"])
            break
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        mtime = _now()
    return {"title": title, "turns": turns, "created": ts or mtime, "updated": mtime}


def get_meta(sid: str) -> dict | None:
    """读 meta；无 meta 的旧会话 → 懒计算视图（不写盘）。会话文件不存在 → None。"""
    d = _session_dir()
    meta = _read_json(d / f"{sid}{META_SUFFIX}")
    if meta is not None:
        meta.setdefault("id", sid)
        return meta
    j = d / f"{sid}.jsonl"
    if not j.is_file():
        return None
    der = _derive_from_jsonl(j)
    return {
        "v": 1, "id": sid, "title": der["title"], "project": None,
        "created": der["created"], "updated": der["updated"], "turns": der["turns"],
        "status": "closed", "parent": None, "_virtual": True,
    }


def update_meta(sid: str, **fields) -> dict:
    """写路径：物化 + 合并 + 原子写。"""
    base = get_meta(sid) or {
        "v": 1, "id": sid, "title": None, "project": None,
        "created": _now(), "updated": _now(), "turns": 0, "status": "closed", "parent": None,
    }
    base.pop("_virtual", None)
    base.update(fields)
    base["updated"] = _now()
    _atomic_write(_session_dir() / f"{sid}{META_SUFFIX}",
                  json.dumps(base, ensure_ascii=False, indent=1))
    return base


def touch_session(sid: str, project: str | None = None) -> dict | None:
    """轮末钩子：按 JSONL 现况刷新 turns/updated；缺 title/created 时补（P14 自动短名落点）。"""
    j = _session_dir() / f"{sid}.jsonl"
    if not j.is_file():
        return None
    der = _derive_from_jsonl(j)
    cur = get_meta(sid) or {}
    fields: dict = {"turns": der["turns"], "updated": der["updated"]}
    if project:
        fields["project"] = project
    if not (cur.get("title") or "").strip():
        fields["title"] = der["title"]
    if not cur.get("created"):
        fields["created"] = der["created"]
    return update_meta(sid, **fields)


def rename_session(sid: str, title: str) -> dict:
    t = _clean_title(title)
    if not t:
        raise ValueError("标题为空")
    return update_meta(sid, title=t[:TITLE_MAX])


# ---------------- 绑定持久化（热载 L2） ----------------

def load_bindings() -> dict:
    obj = _read_json(_session_dir() / CURRENT_NAME) or {}
    b = obj.get("projects")
    return b if isinstance(b, dict) else {}


def _write_bindings(b: dict) -> None:
    _atomic_write(_session_dir() / CURRENT_NAME,
                  json.dumps({"v": 1, "projects": b}, ensure_ascii=False, indent=1))


def save_binding(project: str, sid: str) -> None:
    b = load_bindings()
    b[project] = {"session_id": sid, "updated": _now()}
    _write_bindings(b)


def clear_binding(project: str) -> None:
    b = load_bindings()
    if project in b:
        b.pop(project)
        _write_bindings(b)


def bound_session(project: str) -> str | None:
    e = load_bindings().get(project) or {}
    sid = e.get("session_id")
    return str(sid) if sid else None


# ---------------- 列表 / 当前会话 / 软删 ----------------

def current_info(project: str) -> dict | None:
    """状态载荷用：本工程当前会话摘要（无绑定 → None）。"""
    sid = bound_session(project)
    if not sid:
        return None
    m = get_meta(sid) or {}
    return {"session_id": sid, "title": m.get("title"), "turns": m.get("turns"),
            "status": m.get("status")}


def list_sessions(project: str | None = None, all_flag: bool = False) -> list[dict]:
    """新→旧会话列表（meta 富化）。默认过滤：本工程 + 未归类（旧会话）；all_flag = 全部。"""
    d = _session_dir()
    if not d.is_dir():
        return []
    binds_rev = {v.get("session_id"): k for k, v in load_bindings().items() if isinstance(v, dict)}
    items: list[dict] = []
    files = sorted(d.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True)
    for p in files[:LIST_LIMIT]:
        sid = p.stem
        st_ = p.stat()
        m = get_meta(sid) or {}
        proj = m.get("project")
        if (not all_flag and project is not None
                and proj not in (None, "", project) and binds_rev.get(sid) != project):
            continue
        mtime = datetime.fromtimestamp(st_.st_mtime).isoformat(timespec="seconds")
        items.append({
            "name": p.name,
            "size": st_.st_size,
            "mtime": mtime,
            "id": sid,
            "title": m.get("title"),
            "project": proj,
            "updated": m.get("updated") or mtime,
            "turns": m.get("turns", 0),
            "status": m.get("status", "closed"),
            "bound_project": binds_rev.get(sid),
            "current": bool(project is not None and binds_rev.get(sid) == project),
        })
    return items


def soft_delete(sid: str) -> str:
    """软删：jsonl（+meta）移入 `.trash/`；清掉一切指向它的绑定。"""
    d = _session_dir()
    trash = d / TRASH_DIR
    trash.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for name in (f"{sid}.jsonl", f"{sid}{META_SUFFIX}"):
        p = d / name
        if p.is_file():
            os.replace(p, trash / name)
            moved.append(name)
    if not moved:
        raise FileNotFoundError(sid)
    b = load_bindings()
    dirty = False
    for proj, e in list(b.items()):
        if isinstance(e, dict) and e.get("session_id") == sid:
            b.pop(proj)
            dirty = True
    if dirty:
        _write_bindings(b)
    return str(trash)
