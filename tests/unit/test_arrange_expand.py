"""E4 段3：配器展开器（tsov/arrange/expand.py）单测——网格/和弦/寄存器/加倍/fill/crash。"""

import pytest

from tsov.arrange.expand import expand_role_section, gm_name_for_number, role_program, role_track_name
from tsov.arrange.library import load_patterns
from tsov.core.notes import Note
from tsov.core.units import midi_to_hz


def _facts_68(*, root_pc=2, tones=(2, 6, 9), kind="major"):
    """两段 6/8 迷你事实（4 小节；前奏 1-2 / 主歌 3-4；全篇 D 和弦）。"""
    bar_sec, gpb = 1.5, 12
    grid_sec = bar_sec / gpb
    bars = [{"i": i + 1, "start": round(i * bar_sec, 9), "end": round((i + 1) * bar_sec, 9)} for i in range(4)]
    per_bar = [{"bar": b["i"], "root_pc": root_pc, "kind": kind, "tones": list(tones), "label": "D"}
               for b in bars]
    return {
        "pack": "wotaiko-fast-6-8", "strength": "standard", "key": "D major",
        "meter": {"time_signature": "6/8", "tempo": 120.0, "bar_sec": bar_sec, "gpb": gpb,
                  "grid_sec": grid_sec, "bars_total": 4},
        "bars": bars,
        "sections": [
            {"index": 0, "label": "前奏", "kind": "intro", "start": 0.0, "end": 3.0,
             "start_bar": 1, "end_bar": 2, "bars": 2, "energy": 0.5},
            {"index": 1, "label": "主歌", "kind": "verse", "start": 3.0, "end": 6.0,
             "start_bar": 3, "end_bar": 4, "bars": 2, "energy": 1.0},
        ],
        "chords": {"per_bar": per_bar},
    }


def _melody():
    return [Note(start=3.2, end=3.5, pitch_midi=64, pitch_hz=midi_to_hz(64), velocity=0.7, confidence=0.9),
            Note(start=4.0, end=4.6, pitch_midi=67, pitch_hz=midi_to_hz(67), velocity=0.8, confidence=0.9),
            Note(start=0.2, end=0.5, pitch_midi=60, pitch_hz=midi_to_hz(60), velocity=0.6, confidence=0.9)]


# ---------------------------------------------------------------------------
# 程序/命名
# ---------------------------------------------------------------------------


def test_role_program_and_names():
    lib = load_patterns("wotaiko-fast-6-8")
    assert role_program(lib, "e_drums") == "drums"
    assert role_program(lib, "synth_bass") == "synth_bass"
    assert role_program(lib, "piano") == "piano"
    assert role_program(lib, "synth_lead") == "synth_lead"
    assert role_program(lib, "e_guitar") == "guitar"
    assert role_track_name(lib, "synth_bass") == "合成贝斯"
    lib2 = load_patterns("pop-band-standard")
    assert role_program(lib2, "acoustic_guitar") == "guitar_acoustic"
    assert role_program(lib2, "strings_pad") == "strings"
    lib3 = load_patterns("edm-electro-4-4")
    assert role_program(lib3, "synth_arp") == "synth_arp"


# ---------------------------------------------------------------------------
# 鼓（绝对音高，网格）
# ---------------------------------------------------------------------------


def test_drums_expand_grid_and_velocity():
    lib = load_patterns("wotaiko-fast-6-8")
    facts = _facts_68()
    res = expand_role_section(lib, facts, 1, "e_drums", "base")
    # base 11 事件 × 2 小节 + 段头 crash（非首段）= 23
    assert len(res.notes) == 23
    assert res.crash
    assert res.notes[0].start == pytest.approx(3.0)  # 小节3 起点 = grid 1
    assert res.notes[0].pitch_midi == 36  # 同刻 kick 在 crash(49) 之前（按音高排序）
    assert res.notes[0].velocity == pytest.approx(0.95)  # 0.95×1.0（档2）×1.0（standard）
    # grid 7 = 3.0 + 6*0.125 = 3.75 有 snare 38
    snare = [n for n in res.notes if n.pitch_midi == 38]
    assert snare and snare[0].start == pytest.approx(3.75)


def test_drums_energy_tier_scaling():
    lib = load_patterns("wotaiko-fast-6-8")
    facts = _facts_68()
    res = expand_role_section(lib, facts, 1, "e_drums", "energy", strength="full")
    # 0.98 × 1.12（档3）× 1.1（full）= 1.207 → 截顶 1.0
    assert res.notes[0].velocity == pytest.approx(1.0)
    assert res.notes[0].pitch_midi == 36


def test_crash_only_after_first_section():
    lib = load_patterns("wotaiko-fast-6-8")
    facts = _facts_68()
    r0 = expand_role_section(lib, facts, 0, "e_drums", "sparse")
    assert not r0.crash and not any(n.pitch_midi == 49 for n in r0.notes)
    r1 = expand_role_section(lib, facts, 1, "e_drums", "base")
    assert r1.crash and any(n.pitch_midi == 49 and n.start == pytest.approx(3.0) for n in r1.notes)


# ---------------------------------------------------------------------------
# 相对音高（和弦解析 + 寄存器）
# ---------------------------------------------------------------------------


def test_bass_pedal_resolves_root_in_register():
    lib = load_patterns("wotaiko-fast-6-8")
    facts = _facts_68()
    res = expand_role_section(lib, facts, 1, "synth_bass", "pedal")
    # 2 小节 × 4 事件 = 8
    assert len(res.notes) == 8
    # 寄存器 E1..E3 = 28..40；根音 D(2) 落 38（D2）
    assert all(n.pitch_midi == 38 for n in res.notes)
    starts = [round(n.start, 4) for n in res.notes[:4]]
    assert starts == [pytest.approx(3.0), pytest.approx(3.5), pytest.approx(3.75), pytest.approx(4.25)]
    # len：grid1 持续 4 格 = 0.5s
    assert res.notes[0].end - res.notes[0].start == pytest.approx(0.5)


def test_pad_chord_token_stacks_tones():
    lib = load_patterns("wotaiko-fast-6-8")
    facts = _facts_68()
    res = expand_role_section(lib, facts, 1, "synth_pad", "sustain")
    # 每小节 3 音（D F# A）× 2 小节
    assert len(res.notes) == 6
    pitches = sorted({n.pitch_midi for n in res.notes})
    # 寄存器 C4..C6=60..84；根 D 落 74（D5）→ 74/78/81
    assert pitches == [74, 78, 81]


def test_arp_token_index():
    lib = load_patterns("edm-electro-4-4")
    # 16 格事实：单段，根 A(9) major（A C# E）
    bar_sec, gpb = 2.0, 16
    grid_sec = bar_sec / gpb
    facts = {
        "pack": "edm-electro-4-4", "strength": "standard",
        "meter": {"time_signature": "4/4", "bar_sec": bar_sec, "gpb": gpb, "grid_sec": grid_sec, "bars_total": 1},
        "bars": [{"i": 1, "start": 0.0, "end": 2.0}],
        "sections": [{"index": 0, "label": "副歌", "kind": "chorus", "start": 0.0, "end": 2.0,
                      "start_bar": 1, "end_bar": 1, "bars": 1, "energy": 1.0}],
        "chords": {"per_bar": [{"bar": 1, "root_pc": 9, "kind": "major", "tones": [9, 1, 4], "label": "A"}]},
    }
    res = expand_role_section(lib, facts, 0, "synth_arp", "up16")
    # 16 事件；arp 循环 0,2,0+12,1：grid1=根、grid2=五度、grid3=根+12、grid4=三度
    assert len(res.notes) == 16
    # 寄存器 C5..C7=72..96；根 A(9) 落 81（A5=81? 计算：center 84 → k=round((84-9)/12)=6 → 81）
    first4 = [n.pitch_midi for n in res.notes[:4]]
    assert first4 == [81, 88, 93, 85]  # 81(A5) 88(E6=81+7) 93(A6) 85(C#6=81+4)


def test_register_fit_shifts_octave():
    """高音区 token：根落在寄存器上缘时 chord 层向下收拢不越界。"""
    lib = load_patterns("pop-band-standard")
    bar_sec, gpb = 2.0, 16
    facts = {
        "pack": "pop-band-standard", "strength": "standard",
        "meter": {"time_signature": "4/4", "bar_sec": bar_sec, "gpb": gpb, "grid_sec": bar_sec / gpb, "bars_total": 1},
        "bars": [{"i": 1, "start": 0.0, "end": 2.0}],
        "sections": [{"index": 0, "label": "主歌", "kind": "verse", "start": 0.0, "end": 2.0,
                      "start_bar": 1, "end_bar": 1, "bars": 1, "energy": 1.0}],
        "chords": {"per_bar": [{"bar": 1, "root_pc": 0, "kind": "major", "tones": [0, 4, 7], "label": "C"}]},
    }
    # strings_pad 寄存器 C4..C6 = 60..84；根 C(0) → center 72 → 72（C5）；chord → 72/76/79 全在域内
    res = expand_role_section(lib, facts, 0, "strings_pad", "sustain")
    assert sorted(n.pitch_midi for n in res.notes) == [72, 76, 79]


# ---------------------------------------------------------------------------
# 旋律加倍 / fill / 错误
# ---------------------------------------------------------------------------


def test_melody_double_offset():
    lib = load_patterns("wotaiko-fast-6-8")
    facts = _facts_68()
    res = expand_role_section(lib, facts, 1, "synth_lead", "unison", melody_notes=_melody())
    assert res.melody_offset == 0
    assert [n.pitch_midi for n in res.notes] == [64, 67]  # 段内（3.0-6.0）两音
    res2 = expand_role_section(lib, facts, 1, "synth_lead", "unison_high", melody_notes=_melody())
    assert [n.pitch_midi for n in res2.notes] == [76, 79]
    # 速度/时值保持
    assert res.notes[0].velocity == pytest.approx(0.7)


def test_melody_double_requires_notes():
    lib = load_patterns("wotaiko-fast-6-8")
    facts = _facts_68()
    with pytest.raises(ValueError):
        expand_role_section(lib, facts, 1, "synth_lead", "unison")


def test_fill_adds_events_on_last_bar():
    lib = load_patterns("wotaiko-fast-6-8")
    facts = _facts_68()
    base = expand_role_section(lib, facts, 1, "e_drums", "base")
    filled = expand_role_section(lib, facts, 1, "e_drums", "base", fill="toms_down")
    assert len(filled.notes) == len(base.notes) + 6
    # 最后一个 tom 在小节4 grid12 = 3*1.5 + 11*0.125 = 5.875
    tom = [n for n in filled.notes if n.pitch_midi == 41]
    assert tom and tom[0].start == pytest.approx(5.875)
    # fill 只落段末小节
    assert not any(n.pitch_midi == 41 for n in base.notes)


def test_fill_role_mismatch_rejected():
    lib = load_patterns("wotaiko-fast-6-8")
    facts = _facts_68()
    with pytest.raises(ValueError):
        expand_role_section(lib, facts, 1, "synth_bass", "pedal", fill="toms_down")


def test_section_span_clip():
    """段落 end 截在小节中间：超出段末的事件不落（clip）。"""
    lib = load_patterns("wotaiko-fast-6-8")
    facts = _facts_68()
    facts["sections"][1]["end"] = 4.0  # 主歌只到 4.0s（= 小节3 内 8 格处）
    res = expand_role_section(lib, facts, 1, "e_drums", "base")
    assert all(n.start < 4.0 - 1e-9 for n in res.notes)
    assert res.notes  # 仍有一击（grid 1..7 之内）
