"""谱面入站校验（P0 A3）：apply_score / 导入 / agent 采用前的统一闸门。

只做「结构性合法」检查（值域与类型），不判断音乐性：
- 音符：pitch 0..127、start≥0、end>start、velocity 0..1、deviation 有限
- 工程：tempo>0、拍号形如 n/d（n 1..16，d ∈ 1/2/4/8/16）
- 轨道/总线：volume 0..2、pan -1..1、kind ∈ midi/audio
- 效果：type 非空、params 为对象且值类型合法

返回错误清单（空 = 通过）；带 track[i]/note[j] 定位，供 LLM/用户直接修。
"""

from __future__ import annotations

import math

from .score import Score


def _finite(x) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _check_effect(e, where: str, errors: list[str]) -> None:
    t = getattr(e, "type", None)
    if not isinstance(t, str) or not t.strip():
        errors.append(f"{where}: effect.type 不能为空")
    params = getattr(e, "params", None)
    if not isinstance(params, dict):
        errors.append(f"{where}: effect.params 需为对象")
        return
    for k, v in params.items():
        if not isinstance(k, str) or not k:
            errors.append(f"{where}: effect 参数名非法：{k!r}")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if not math.isfinite(float(v)):
                errors.append(f"{where}: effect 参数 {k} 非有限数")
        elif not isinstance(v, (str, bool)) and v is not None:
            errors.append(f"{where}: effect 参数 {k} 类型非法（{type(v).__name__}）")


def validate_score(score: Score) -> list[str]:
    errors: list[str] = []

    meta = getattr(score, "meta", None)
    if isinstance(meta, dict) and meta.get("schema_version") not in (None, "", 1, "1"):
        errors.append(f"meta.schema_version 不支持：{meta.get('schema_version')!r}（本版支持 1）")

    tempo = getattr(score, "tempo", None)
    if not _finite(tempo) or float(tempo) <= 0:
        errors.append(f"tempo 非法：{tempo!r}（需 >0 的有限数）")

    ts = str(getattr(score, "time_signature", "") or "")
    try:
        num_s, den_s = ts.split("/", 1)
        num, den = int(num_s), int(den_s)
    except (ValueError, AttributeError):
        errors.append(f"time_signature 非法：{ts!r}（形如 6/8 / 3/4）")
    else:
        if not (1 <= num <= 16) or den not in (1, 2, 4, 8, 16) or f"{num}/{den}" != ts:
            errors.append(f"time_signature 非法：{ts!r}（1≤每小节拍数≤16；分母 ∈ 1/2/4/8/16）")

    for ti, track in enumerate(getattr(score, "tracks", []) or []):
        w = f"track[{ti}]({getattr(track, 'name', '')!r})"
        inst = getattr(track, "instrument", None)
        if inst is None:
            errors.append(f"{w}: 缺少 instrument")
        else:
            if not isinstance(getattr(inst, "program", None), str):
                errors.append(f"{w}: instrument.program 需为字符串")
            vol = getattr(inst, "volume", None)
            if not _finite(vol) or not (0.0 <= float(vol) <= 2.0):
                errors.append(f"{w}: instrument.volume 越界：{vol!r}（0~2）")
            for ei, e in enumerate(getattr(inst, "effects", []) or []):
                _check_effect(e, f"{w}.effect[{ei}]", errors)

        pan = getattr(track, "pan", None)
        if not _finite(pan) or not (-1.0 <= float(pan) <= 1.0):
            errors.append(f"{w}: pan 越界：{pan!r}（-1~1）")
        if getattr(track, "kind", "midi") not in ("midi", "audio"):
            errors.append(f"{w}: kind 非法：{getattr(track, 'kind', None)!r}（midi/audio）")

        for ni, n in enumerate(getattr(track, "notes", []) or []):
            nw = f"{w}.note[{ni}]"
            p = getattr(n, "pitch_midi", None)
            if isinstance(p, bool) or not isinstance(p, int) or not (0 <= p <= 127):
                errors.append(f"{nw}: pitch_midi 越界：{p!r}（0~127 整数）")
            start = getattr(n, "start", None)
            end = getattr(n, "end", None)
            if not _finite(start) or float(start) < 0:
                errors.append(f"{nw}: start 非法：{start!r}（≥0）")
            if not _finite(end):
                errors.append(f"{nw}: end 非法：{end!r}")
            elif _finite(start) and float(end) <= float(start):
                errors.append(f"{nw}: end 必须大于 start（{start!r} → {end!r}）")
            vel = getattr(n, "velocity", None)
            if not _finite(vel) or not (0.0 <= float(vel) <= 1.0):
                errors.append(f"{nw}: velocity 越界：{vel!r}（0~1）")
            dev = getattr(n, "deviation_cents", 0.0)
            if not _finite(dev):
                errors.append(f"{nw}: deviation_cents 非有限数")

    for bi, bus in enumerate([getattr(score, "master", None)] + list(getattr(score, "buses", []) or [])):
        if bus is None:
            errors.append("master 缺失")
            continue
        w = f"bus[{bi}]({getattr(bus, 'name', '')!r})"
        vol = getattr(bus, "volume", None)
        if not _finite(vol) or not (0.0 <= float(vol) <= 2.0):
            errors.append(f"{w}: volume 越界：{vol!r}（0~2）")
        pan = getattr(bus, "pan", None)
        if not _finite(pan) or not (-1.0 <= float(pan) <= 1.0):
            errors.append(f"{w}: pan 越界：{pan!r}（-1~1）")
        for ei, e in enumerate(getattr(bus, "effects", []) or []):
            _check_effect(e, f"{w}.effect[{ei}]", errors)

    return errors
