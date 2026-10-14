"""VST3 参数表资产（批L L1-3；ADR-0025 决策 5「参数知识两层表」）。

- **机器层**：经插件 worker 反查全量参数（dawdreamer introspection），生成
  `presets/v3-maps/<slug>.json`（可重扫——重扫保留同名参数的既有策展字段）。
- **策展层**：同一文件内每条参数的可选字段 `alias_zh / group / suggested / notes`（人工维护）。

文件 schema（落盘实态；Q3 草案的「normalized」落为：值域全归一 0..1 + `default` 字段）：

    {"plugin": "Dexed", "plugin_path": "vendor/vst3/Dexed.vst3",
     "generator": {"tool": "tsov vst3-map", "protocol": 1, "dawdreamer": "0.9.0",
                   "generated_at": "…"},
     "param_count": 2238,
     "params": [{"index": 0, "name": "Cutoff", "default": 0.0, "default_text": "1",
                 "discrete": false, "labels": null,
                 "alias_zh": "截止", "group": "音色", "suggested": 0.8, "notes": "…"}]}

读侧（``load_map / resolve_param_name``）供命令层校验与 agent 检索共用。
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
MAP_DIR = _REPO_ROOT / "presets" / "v3-maps"

_CURATED_FIELDS = ("alias_zh", "group", "suggested", "notes")


def slug_of(plugin_path: str) -> str:
    """插件路径 → 表 slug（bundle 目录 / 内层二进制统一取末端 `*.vst3` 名小写，空白折成 `-`）。"""
    name = Path(str(plugin_path)).name
    stem = name[:-5] if name.lower().endswith(".vst3") else name
    return re.sub(r"\s+", "-", (stem or "plugin").lower())


def map_path(plugin_path: str, out_dir: Path | str | None = None) -> Path:
    d = Path(out_dir) if out_dir else MAP_DIR
    return d / f"{slug_of(plugin_path)}.json"


def load_map(plugin_path: str, out_dir: Path | str | None = None) -> dict | None:
    """读参数表；未生成/损坏 → None（调用方降级到现查或透传）。"""
    p = map_path(plugin_path, out_dir)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def is_curated(entry: dict) -> bool:
    return any(entry.get(k) not in (None, "", [], {}) for k in _CURATED_FIELDS)


def resolve_param_name(plugin_path: str, name: str) -> tuple[str | None, str | None]:
    """解析用户输入（名称 / 中文别名 / 十进制下标）→ 规范参数名。

    返回 ``(canonical, err)``；**无表**（未生成）→ ``(None, None)`` 透传
    （渲染期严格校验兜底）；有表但解析不到 → ``(None, 错误信息)``。
    """
    m = load_map(plugin_path)
    if m is None:
        return None, None
    entries = m.get("params") or []
    n = str(name).strip()
    if n.lstrip("-").isdigit():
        idx = int(n)
        for e in entries:
            if int(e.get("index", -1)) == idx:
                return str(e.get("name")), None
        return None, f"参数下标越界：{n}（现有 {len(entries)} 个参数）"
    for e in entries:
        if str(e.get("name")) == n:
            return n, None
    for e in entries:
        if e.get("alias_zh") and str(e["alias_zh"]) == n:
            return str(e.get("name")), None
    return None, f"参数不存在：{n!r}（可先用 list_instrument_params 检索核对；共 {len(entries)} 个参数）"


def generate_map(plugin_path: str, out_dir: Path | str | None = None) -> Path:
    """经 worker 反查全量参数并落盘（保留既有策展字段）。返回表文件路径。"""
    from . import vst_worker

    ap = str(Path(plugin_path).resolve())
    w = vst_worker.get_worker()
    if w is None:
        raise RuntimeError("生成参数表需要插件 worker（当前 TSOV_VST_INPROC=1；去掉该环境变量后重试）")
    data = w.list_params(ap)

    # 既有文件（重扫合并策展字段 + meta 级字段保留，P62a）
    target = map_path(plugin_path, out_dir)
    old_by: dict = {}
    old_meta: dict | None = None
    if target.is_file():
        try:
            old = json.loads(target.read_text(encoding="utf-8"))
            old_by = {str(e.get("name")): e for e in (old.get("params") or []) if isinstance(e, dict)}
            old_meta = old.get("meta") if isinstance(old.get("meta"), dict) else None
        except (OSError, ValueError):
            old_by = {}

    entries: list[dict] = []
    for e in data.get("params") or []:
        ent = {k: e[k] for k in ("index", "name", "default", "default_text", "discrete", "labels") if k in e}
        prev = old_by.get(str(ent.get("name"))) or {}
        for k in _CURATED_FIELDS:
            if prev.get(k) not in (None, "", [], {}):
                ent[k] = prev[k]
        entries.append(ent)

    try:
        rel_path = str(Path(ap).relative_to(_REPO_ROOT))
    except ValueError:
        rel_path = ap
    doc: dict = {
        "plugin": Path(ap).stem,
        "plugin_path": rel_path,
    }
    if old_meta:   # P62a：策展 meta（如静音提示 notes）重扫保留
        doc["meta"] = old_meta
    doc["generator"] = {
        "tool": "tsov vst3-map",
        "protocol": vst_worker.PROTOCOL,
        "dawdreamer": (getattr(w, "worker_info", {}) or {}).get("dawdreamer") or "?",
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    doc["param_count"] = len(entries)
    doc["params"] = entries
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return target
