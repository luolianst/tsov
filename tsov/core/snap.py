"""调外音吸附（M-V8 E3 段2）：转录音符「调外且偏差足够 → 吸附最近调内音」。

配方来源：技能 singing-voice-transcription（2026-08-13 实测）——调外且
|deviation_cents| ≥ 42 的音吸附到最近调内音（半音级），保旋律轮廓。

- pitch_midi → 最近调内音；pitch_hz 保留（声学事实）；deviation_cents 按
  「相对新 pitch_midi」重算（dev -= 100 × 半音位移）
- 调性：key 参数（"C major" / "a minor" / "D dorian"）；缺省 auto（detect_key top1）
- 位置：core 层（纯音符数学；命令层 snap_scale op 与链工具 snap_scale 共用，
  ADR-0017 同路径；host 不得依赖 dsp —— F1 治理口径）
"""

from __future__ import annotations

from dataclasses import replace

from .key import detect_key, key_pitch_classes
from .notes import Note


def nearest_scale_pc(pc: int, scale_pcs: set[int], dev_cents: float) -> tuple[int, int]:
    """最近的调内 pitch class 及最短半音位移（delta ∈ [-6, 6]）。

    上下等距（±6 半音，如 C 大调里 F# 到 F/G）时按偏差方向取：
    偏高（dev ≥ 0）吸上方音，偏低吸下方音。
    """
    up = min(scale_pcs, key=lambda c: ((c - pc) % 12) or 12)
    dn = min(scale_pcs, key=lambda c: ((pc - c) % 12) or 12)
    du = (up - pc) % 12
    dd = (pc - dn) % 12
    if du == dd:
        target = up if dev_cents >= 0 else dn
    else:
        target = up if du < dd else dn
    delta = ((target - pc + 6) % 12) - 6
    return target, delta


def snap_out_of_key(
    notes: list[Note],
    key: str | None = None,
    threshold_cents: float = 42.0,
) -> tuple[list[Note], dict]:
    """吸附调外音 →（新音符列表, 统计）。

    - 判定：pitch class 不在调内 且 |deviation_cents| ≥ threshold（默认 42）
    - 统计：{key, auto, scanned, snapped, unchanged}
    - key 缺省 → detect_key 自动判定；样本不足（<4 音）→ 原样返回（stats.key=None）
    - 非法 key 字符串 → ValueError
    """
    used_key: str | None = key if key else None
    auto = not key
    if used_key is None:
        cands = detect_key(notes)
        if not cands:
            return list(notes), {"key": None, "auto": True, "scanned": len(notes),
                                 "snapped": 0, "unchanged": len(notes)}
        used_key = cands[0].key
    pcs = key_pitch_classes(used_key)
    if pcs is None:
        raise ValueError(f"无法解析调性：{used_key!r}（示例 'C major' / 'a minor' / 'D dorian'）")

    threshold = float(threshold_cents)
    out: list[Note] = []
    snapped = 0
    for n in notes:
        pc = int(n.pitch_midi) % 12
        dev = float(n.deviation_cents)
        if pc in pcs or abs(dev) < threshold or int(n.pitch_midi) <= 0:
            out.append(n)
            continue
        _target, delta = nearest_scale_pc(pc, pcs, dev)
        if delta == 0:
            out.append(n)
            continue
        out.append(replace(
            n,
            pitch_midi=int(n.pitch_midi) + delta,
            deviation_cents=round(dev - 100.0 * delta, 3),
        ))
        snapped += 1
    return out, {"key": used_key, "auto": auto, "scanned": len(notes),
                 "snapped": snapped, "unchanged": len(notes) - snapped}
