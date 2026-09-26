"""对拍（M-V8 E4 段2 · Q6）：应用后自动跑——音乐版 pre-commit。

清单：相对电平 diff（目标达成 ±1.5 dB）+ 峰值/削波（余量线 -0.5 dBFS）+
频谱前后（每轨最大频段份额变化）+ LUFS（ffmpeg 量档；缺失 → 跳过不报错）。
产出 ✅/⚠/❗/ℹ 判语清单（全数值化）→ 前端「应用小结」卡。

纯函数：吃前后两份 facts + 两个 LUFS 数 → 报告 dict；音频渲染/落盘在路由层。
"""

from __future__ import annotations

import time

from . import facts as facts_mod

TOLERANCE_DB = 1.5          # 目标带（电平达成判据）
PEAK_WARN_DBFS = -0.5       # 峰值余量告警线

_OK, _WARN, _BAD, _INFO = "ok", "warn", "bad", "info"
_RANK = {_OK: 0, _INFO: 0, _WARN: 1, _BAD: 2}


def build_report(facts_before: dict, facts_after: dict, *, applied_ids=None,
                 lufs_before: float | None = None, lufs_after: float | None = None,
                 files: dict | None = None) -> dict:
    items: list[dict] = []
    details: dict = {}

    # ---- 电平：相对目标缺口 前→后 ----
    targets, pole = facts_mod.target_relatives(facts_before)
    if targets:
        names = {int(r["index"]): r.get("name")
                 for r in ((facts_before.get("levels") or {}).get("tracks") or [])}
        met, total = 0, 0
        rows_l: list[dict] = []
        for i in sorted(targets):
            mb = facts_mod.measured_relative(facts_before, i, pole)
            ma = facts_mod.measured_relative(facts_after, i, pole)
            if mb is None or ma is None:
                continue
            gap_b = round(mb - float(targets[i]), 2)
            gap_a = round(ma - float(targets[i]), 2)
            ok = abs(gap_a) <= TOLERANCE_DB
            met += 1 if ok else 0
            total += 1
            rows_l.append({"track": i, "name": names.get(i), "gap_before": gap_b,
                           "gap_after": gap_a, "ok": ok})
        if total:
            st = _OK if met == total else (_WARN if met * 2 >= total else _BAD)
            items.append({"status": st, "text": f"电平：{met}/{total} 轨到目标带（±{TOLERANCE_DB:g} dB）"})
            details["levels"] = rows_l

    # ---- 峰值 / 削波 ----
    pb = (facts_before.get("levels") or {}).get("mix_peak_dbfs")
    pa = (facts_after.get("levels") or {}).get("mix_peak_dbfs")
    clip_a = bool((facts_after.get("levels") or {}).get("clipping"))
    if pb is not None and pa is not None:
        st = _BAD if clip_a else (_WARN if float(pa) > PEAK_WARN_DBFS else _OK)
        tail = "❗仍削波" if clip_a else ("⚠ 余量不足" if float(pa) > PEAK_WARN_DBFS else "✓")
        items.append({"status": st, "text": f"峰值：{float(pb):+.1f} → {float(pa):+.1f} dBFS {tail}"})
        details["peak"] = {"before": pb, "after": pa, "clipping": clip_a}

    # ---- 频谱前后（每轨最大频段份额变化，按变化排序取前 3）----
    sb = {int(s["index"]): s for s in ((facts_before.get("spectrum") or {}).get("tracks") or [])}
    sa = {int(s["index"]): s for s in ((facts_after.get("spectrum") or {}).get("tracks") or [])}
    spec_deltas: list[dict] = []
    for i in sorted(set(sb) & set(sa)):
        ba, bb = list(sb[i].get("bands") or []), list(sa[i].get("bands") or [])
        if not ba or len(ba) != len(bb):
            continue
        dmax = max(abs(float(x) - float(y)) for x, y in zip(ba, bb))
        if dmax > 0:
            spec_deltas.append({"track": i, "name": sb[i].get("name"),
                                "max_band_delta": round(dmax, 4)})
    spec_deltas.sort(key=lambda x: x["max_band_delta"], reverse=True)
    if spec_deltas:
        top = spec_deltas[:3]
        items.append({"status": _INFO,
                      "text": "频谱变化最大：" + "、".join(
                          f"{t['name']}({t['max_band_delta']:.3f})" for t in top)})
        details["spectrum"] = spec_deltas

    # ---- LUFS ----
    if lufs_before is not None and lufs_after is not None:
        items.append({"status": _INFO,
                      "text": f"LUFS：{float(lufs_before):.1f} → {float(lufs_after):.1f}"
                              f"（{float(lufs_after) - float(lufs_before):+.1f}）"})
        details["lufs"] = {"before": lufs_before, "after": lufs_after}
    else:
        items.append({"status": _INFO, "text": "LUFS：未测（ffmpeg 缺失或失败）"})
        details["lufs"] = None

    worst = max((_RANK.get(str(it.get("status")), 0) for it in items), default=0)
    return {
        "status": {0: _OK, 1: _WARN, 2: _BAD}[worst],
        "ok": worst < 2,
        "items": items,
        "details": details,
        "applied": [str(x) for x in (applied_ids or [])],
        "files": dict(files or {}),
        "built_at": time.time(),
    }
