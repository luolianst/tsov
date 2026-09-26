"""批次与事件存储（M-V8 E4 段3）：<工程>/arrange/ 下的批次 JSON + events.jsonl。

- 批次：``arrange/<batch_ts>.json`` = 计划（LLM 选 ID）+ 轨规格 + 事实快照 + 决定回填（load/save/list/update）
- 目录：``arrange/<batch_ts>/`` = 试听音频（preview-mix.wav）
- 事件：``arrange/events.jsonl`` = 结构化记账（generate/apply/preview/discard），append-only

纯 stdlib；不碰工程 score（落地走路由的命令层事务）。ts 合法性一律先过 ``_safe_ts``。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

ARRANGE_DIR = "arrange"
EVENTS_FILE = "events.jsonl"


def arrange_dir(root) -> Path:
    return Path(root) / ARRANGE_DIR


def _safe_ts(ts) -> str:
    """批次时间戳合法性（防路径穿越）：只允许 [0-9A-Za-z_-]。"""
    s = str(ts or "")
    if not s or not all(c.isalnum() or c in "-_" for c in s):
        raise ValueError(f"非法批次时间戳：{ts!r}")
    return s


def new_batch_ts(root) -> str:
    """新批次时间戳（YYYYmmdd-HHMMSS；同秒撞名 → -2/-3…）。"""
    base = time.strftime("%Y%m%d-%H%M%S")
    ts, k = base, 2
    while (arrange_dir(root) / f"{ts}.json").exists():
        ts = f"{base}-{k}"
        k += 1
    return ts


def save_batch(root, batch: dict) -> Path:
    ts = _safe_ts(batch.get("batch_ts"))
    p = arrange_dir(root) / f"{ts}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(batch, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def load_batch(root, ts) -> dict:
    """读批次；不存在 → FileNotFoundError，内容坏 → ValueError。"""
    s = _safe_ts(ts)
    p = arrange_dir(root) / f"{s}.json"
    if not p.is_file():
        raise FileNotFoundError(f"配器批次不存在：{s}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data.get("batch_ts"):
        raise ValueError(f"配器批次损坏：{s}")
    return data


def update_batch(root, ts, **fields) -> dict:
    """合并字段回填（决定/试听）→ 返回更新后的批次。"""
    data = load_batch(root, ts)
    data.update(fields)
    save_batch(root, data)
    return data


def list_batches(root) -> list[dict]:
    """批次摘要（新→旧）：{batch_ts, created_at, pack, strength, n_tracks, n_notes, source, state}。"""
    d = arrange_dir(root)
    if not d.is_dir():
        return []
    out: list[dict] = []
    for f in sorted(d.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not data.get("batch_ts"):
            continue
        tracks = data.get("tracks") or []
        out.append({
            "batch_ts": str(data["batch_ts"]),
            "created_at": float(data.get("created_at") or 0.0),
            "pack": data.get("pack"),
            "strength": data.get("strength"),
            "n_tracks": len(tracks),
            "n_notes": sum(int(t.get("n_notes") or 0) for t in tracks),
            "source": (data.get("stats") or {}).get("source"),
            "state": str(data.get("state") or "pending"),
            "applied": len(data.get("applied") or []),
        })
    out.sort(key=lambda x: (x["created_at"], x["batch_ts"]), reverse=True)
    return out


def batch_dir(root, ts) -> Path:
    """批次音频目录（建好即用）。"""
    p = arrange_dir(root) / _safe_ts(ts)
    p.mkdir(parents=True, exist_ok=True)
    return p


def append_event(root, type_: str, data: dict | None = None) -> dict:
    """追加一条结构化事件（JSONL）→ 返回写入的事件。"""
    ev: dict = {"ts": round(time.time(), 3), "type": str(type_)}
    ev.update(data or {})
    p = arrange_dir(root) / EVENTS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return ev


def load_events(root, *, limit: int = 200, type_: str | None = None) -> list[dict]:
    """读事件（时间顺序；可选按类型过滤；limit 取尾部）。"""
    p = arrange_dir(root) / EVENTS_FILE
    if not p.is_file():
        return []
    out: list[dict] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            if type_ and ev.get("type") != type_:
                continue
            out.append(ev)
    except OSError:
        return []
    return out[-int(limit):] if limit else out
