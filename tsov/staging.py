"""暂存区（M-V8 E4 段1 · Q10.5/Q14）：AI/链产物默认先进暂存，人批才落地。

条目（item）抽象（全 producer 统一）：
    {id, producer, kind, title, created_at, state, ready, refs, meta}
- id = "<producer>:<ref>"（如 chain:<run_ts>）
- producer: chain | tune | arrange（三段均已接入）
- kind: notes | audio | commands
- state: pending → adopted / discarded（pending = 探针动态枚举减去已处置记录）
- ready: 是否具备采纳条件（链 = 03 原始 + 05 处理都在）

存储分工：
- pending：由 producer 探针**动态枚举**（链 = 扫 <工程>/chain/<run_ts>/ 目录），不落盘；
- adopted/discarded：写工程 `.tsov-state.json` 的 "staging.records" 键（该文件不进 git——
  工作态不污染工程版本；同 chain.json 先例）。

边界（Q10.5 守线）：AI/链产物默认进暂存；用户手操直接做（撤销在）。
采纳**执行**不在这里——走 webapp 路由（命令层事务，ADR-0017）；本模块 stdlib 零依赖。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

STATE_FILE = ".tsov-state.json"
CHAIN_DIR = "chain"
TUNE_DIR = "tune"
ARRANGE_DIR = "arrange"

# 链 run 目录产物命名（与 tsov/chain/tools.py 一致）
CHAIN_WAVS = ("01-denoised.wav", "02-loudnorm.wav")
CHAIN_RAW = "03-voice.json"
CHAIN_PROCESSED = "05-notes-snapped.json"


# ---------------------------------------------------------------------------
# 处置记录（.tsov-state.json · staging.records）
# ---------------------------------------------------------------------------


def _load_state(proj_root) -> dict:
    p = Path(proj_root) / STATE_FILE
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(proj_root, data: dict) -> None:
    p = Path(proj_root) / STATE_FILE
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def load_records(proj_root) -> dict:
    """已处置记录 {item_id: {state, ts, note?}}。"""
    recs = (_load_state(proj_root).get("staging") or {}).get("records")
    return recs if isinstance(recs, dict) else {}


def set_state(proj_root, item_id: str, state: str, *, note: str = "") -> dict:
    """记一条处置（adopted/discarded）→ .tsov-state.json（保留其他键）。返回该记录。"""
    if state not in ("adopted", "discarded"):
        raise ValueError(f"非法状态：{state!r}（只允许 adopted|discarded）")
    data = _load_state(proj_root)
    st = data.get("staging")
    if not isinstance(st, dict):
        st = {}
        data["staging"] = st
    recs = st.get("records")
    if not isinstance(recs, dict):
        recs = {}
        st["records"] = recs
    rec: dict = {"state": state, "ts": time.time()}
    if note:
        rec["note"] = str(note)
    recs[str(item_id)] = rec
    _save_state(proj_root, data)
    return rec


# ---------------------------------------------------------------------------
# producer 探针
# ---------------------------------------------------------------------------


def _count_notes(p: Path) -> int | None:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    notes = d.get("notes") if isinstance(d, dict) else d
    return len(notes) if isinstance(notes, list) else None


def chain_items(proj_root) -> list[dict]:
    """链产物探针：扫 <工程>/chain/<run_ts>/（新→旧）。

    只列「有内容」的 run（任一已知产物在）；就绪 = 03 原始 + 05 处理都在。
    """
    root = Path(proj_root) / CHAIN_DIR
    if not root.is_dir():
        return []
    out: list[dict] = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        raw_p = d / CHAIN_RAW
        proc_p = d / CHAIN_PROCESSED
        wavs = [w for w in CHAIN_WAVS if (d / w).is_file()]
        if not (raw_p.is_file() or proc_p.is_file() or wavs):
            continue  # 空目录/无关目录不列
        try:
            created = float(d.stat().st_mtime)
        except OSError:
            created = 0.0
        missing = []
        if not raw_p.is_file():
            missing.append(CHAIN_RAW)
        if not proc_p.is_file():
            missing.append(CHAIN_PROCESSED)
        out.append({
            "id": f"chain:{d.name}",
            "producer": "chain",
            "kind": "notes",
            "title": "哼唱链产物（转录双轨）",
            "created_at": created,
            "state": "pending",
            "ready": not missing,
            "refs": {"run_ts": d.name},
            "meta": {
                "run_ts": d.name,
                "notes_raw": _count_notes(raw_p) if raw_p.is_file() else None,
                "notes_processed": _count_notes(proc_p) if proc_p.is_file() else None,
                "audio": wavs,
                "missing": missing,
            },
        })
    out.sort(key=lambda x: x["created_at"], reverse=True)
    return out


def tune_items(proj_root) -> list[dict]:
    """调参批次探针：扫 <工程>/tune/*.json（新→旧）；就绪 = 建议集非空。

    与链产物同抽象：AI 出的批次默认 pending——人批（应用/丢弃）才写处置记录。
    """
    root = Path(proj_root) / TUNE_DIR
    if not root.is_dir():
        return []
    out: list[dict] = []
    for f in sorted(root.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict) or not d.get("batch_ts"):
            continue
        ts = str(d["batch_ts"])
        sugg = d.get("suggestions") or []
        try:
            created = float(f.stat().st_mtime)
        except OSError:
            created = 0.0
        out.append({
            "id": f"tune:{ts}",
            "producer": "tune",
            "kind": "commands",
            "title": f"调参建议批次（{len(sugg)} 条）",
            "created_at": created,
            "state": "pending",
            "ready": bool(sugg),
            "refs": {"batch_ts": ts},
            "meta": {
                "batch_ts": ts,
                "suggestions": len(sugg),
                "applied": len(d.get("applied") or []),
                "pack": d.get("pack"),
                "has_report": bool(d.get("report")),
                "stats": d.get("stats"),
            },
        })
    out.sort(key=lambda x: x["created_at"], reverse=True)
    return out


def arrange_items(proj_root) -> list[dict]:
    """配器批次探针：扫 <工程>/arrange/*.json（新→旧）；就绪 = 轨规格非空。

    与链/调参同抽象：AI 出稿默认 pending——人批（进工程/丢弃）才写处置记录。
    """
    root = Path(proj_root) / ARRANGE_DIR
    if not root.is_dir():
        return []
    out: list[dict] = []
    for f in sorted(root.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict) or not d.get("batch_ts"):
            continue
        ts = str(d["batch_ts"])
        tracks = d.get("tracks") or []
        n_notes = sum(int(t.get("n_notes") or 0) for t in tracks if isinstance(t, dict))
        try:
            created = float(f.stat().st_mtime)
        except OSError:
            created = 0.0
        out.append({
            "id": f"arrange:{ts}",
            "producer": "arrange",
            "kind": "commands",
            "title": f"配器初稿（{len(tracks)} 轨 / {n_notes} 音）",
            "created_at": created,
            "state": "pending",
            "ready": bool(tracks),
            "refs": {"batch_ts": ts},
            "meta": {
                "batch_ts": ts,
                "tracks": len(tracks),
                "notes": n_notes,
                "pack": d.get("pack"),
                "strength": d.get("strength"),
                "source": (d.get("stats") or {}).get("source"),
                "overall": str(d.get("overall") or "")[:200],
            },
        })
    out.sort(key=lambda x: x["created_at"], reverse=True)
    return out


_PRODUCERS = {"chain": chain_items, "tune": tune_items, "arrange": arrange_items}


# ---------------------------------------------------------------------------
# 聚合视图
# ---------------------------------------------------------------------------


def list_items(proj_root) -> dict:
    """全 producer 聚合 + 处置记录 → 统一列表。

    排序：pending 在前（新→旧）；已处置在后（处置时间新→旧）。
    """
    recs = load_records(proj_root)
    items: list[dict] = []
    for fn in _PRODUCERS.values():
        items.extend(fn(proj_root))
    for it in items:
        rec = recs.get(it["id"])
        if isinstance(rec, dict) and rec.get("state") in ("adopted", "discarded"):
            it["state"] = rec["state"]
            it["handled_at"] = rec.get("ts")
    pending = [i for i in items if i["state"] == "pending"]
    handled = [i for i in items if i["state"] != "pending"]
    handled.sort(key=lambda x: x.get("handled_at") or 0, reverse=True)
    counts = {
        "pending": len(pending),
        "adopted": sum(1 for i in items if i["state"] == "adopted"),
        "discarded": sum(1 for i in items if i["state"] == "discarded"),
    }
    return {"items": pending + handled, "counts": counts}


def get_item(proj_root, item_id: str) -> dict | None:
    """按 id 取条目（含处置后的 state）。"""
    for it in list_items(proj_root)["items"]:
        if it["id"] == str(item_id):
            return it
    return None
