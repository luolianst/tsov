"""E4 段3：配器事实底座（tsov/arrange/facts.py）单测。"""

import pytest

from tsov.arrange.facts import build_arrange_facts, classify_section
from tsov.core.notes import Note
from tsov.core.score import Bookmark, Instrument, KeyCandidate, Score, Track
from tsov.core.units import midi_to_hz


def _mel_notes(start=0.0, n=20, step=0.5):
    return [Note(start=start + i * step, end=start + i * step + 0.4,
                 pitch_midi=60 + (i % 8), pitch_hz=midi_to_hz(60 + (i % 8)),
                 velocity=0.8, confidence=0.9) for i in range(n)]


def _score_44():
    return Score(
        title="t", tempo=120.0, time_signature="4/4",
        key_candidates=[KeyCandidate(key="C major", confidence=1.0)],
        tracks=[
            Track(name="melody", instrument=Instrument(backend="fluidsynth", program="piano"),
                  notes=_mel_notes(n=20)),
            Track(name="other", instrument=Instrument(backend="fluidsynth", program="bass"), notes=[]),
        ],
        bookmarks=[
            Bookmark(scope="project", kind="section", start=0.0, end=2.0, label="前奏"),
            Bookmark(scope="project", kind="section", start=2.0, end=6.0, label="主歌"),
            Bookmark(scope="project", kind="section", start=6.0, end=9.9, label="副歌"),
        ],
    )


def _score_68():
    return Score(
        title="t68", tempo=120.0, time_signature="6/8",
        key_candidates=[KeyCandidate(key="D major", confidence=1.0)],
        tracks=[Track(name="melody", instrument=Instrument(backend="fluidsynth", program="piano"),
                      notes=_mel_notes(n=9, step=0.5))],  # 覆盖 0..4.4s
        bookmarks=[Bookmark(scope="project", kind="section", start=0.0, end=1.5, label="前奏"),
                   Bookmark(scope="project", kind="section", start=1.5, end=4.5, label="主歌")],
    )


# ---------------------------------------------------------------------------
# 段型分类
# ---------------------------------------------------------------------------


def test_classify_section_keywords():
    cases = {
        "前奏": "intro", "Intro": "intro", "prelude 前奏": "intro",
        "预副歌": "pre", "Pre-Chorus": "pre", "导歌": "pre",
        "副歌": "chorus", "Chorus 1": "chorus", "高潮段": "chorus",
        "主歌A": "verse", "Verse 2": "verse", "正歌": "verse",
        "桥段": "bridge", "Bridge": "bridge",
        "间奏": "interlude", "Solo": "interlude",
        "尾奏": "outro", "Outro": "outro",
    }
    for label, kind in cases.items():
        got, src = classify_section(label)
        assert got == kind, f"{label!r} → {got}（期望 {kind}）"
        assert src == "label"
    got, src = classify_section("段落X")
    assert got == "verse" and src == "fallback"
    assert classify_section("") == ("verse", "fallback")


def test_classify_pre_before_chorus():
    """「预副歌」含「副歌」——规则顺序必须 pre 优先。"""
    assert classify_section("预副歌")[0] == "pre"


# ---------------------------------------------------------------------------
# 4/4 事实
# ---------------------------------------------------------------------------


def test_facts_44_basic():
    facts = build_arrange_facts(_score_44(), pack="pop-band-standard", strength="standard", name="proj")
    m = facts["meter"]
    assert m["time_signature"] == "4/4" and m["gpb"] == 16
    assert m["bar_sec"] == pytest.approx(2.0)
    assert m["bars_total"] == 5
    assert facts["key"] == "C major"
    assert facts["melody"]["track"] == 0 and facts["melody"]["name"] == "melody"
    assert facts["melody"]["notes"] == 20
    secs = facts["sections"]
    assert [s["kind"] for s in secs] == ["intro", "verse", "chorus"]
    assert (secs[0]["start_bar"], secs[0]["end_bar"]) == (1, 1)
    assert (secs[1]["start_bar"], secs[1]["end_bar"]) == (2, 3)
    assert (secs[2]["start_bar"], secs[2]["end_bar"]) == (4, 5)
    assert all(s["kind_source"] == "label" for s in secs)
    # 能量：密度归一化
    assert max(s["energy"] for s in secs) == pytest.approx(1.0)
    per_bar = facts["chords"]["per_bar"]
    assert [r["bar"] for r in per_bar] == [1, 2, 3, 4, 5]
    assert all(r["label"] in ("C", "F", "G", "Am", "Dm", "Em") for r in per_bar)
    assert "e_drums" in facts["roles"] and "bass" in facts["roles"]


def test_facts_68_meter():
    facts = build_arrange_facts(_score_68(), pack="wotaiko-fast-6-8", name="p68")
    m = facts["meter"]
    assert m["time_signature"] == "6/8" and m["gpb"] == 12
    assert m["bar_sec"] == pytest.approx(1.5)
    assert m["grid_sec"] == pytest.approx(0.125)
    assert m["bars_total"] == 3
    assert facts["pack"] == "wotaiko-fast-6-8"
    assert [s["kind"] for s in facts["sections"]] == ["intro", "verse"]
    assert facts["sections"][1]["start_bar"] == 2 and facts["sections"][1]["end_bar"] == 3


def test_facts_melody_track_pick_and_errors():
    score = _score_44()
    # melody_track=1 无音符 → 明确报错（不静默回落）
    with pytest.raises(ValueError):
        build_arrange_facts(score, pack="pop-band-standard", melody_track=1)
    with pytest.raises(ValueError):
        build_arrange_facts(_score_44(), pack="wotaiko-fast-6-8", strength="max")
    empty = Score(title="e", tempo=120.0, tracks=[Track(name="melody")])
    with pytest.raises(ValueError):
        build_arrange_facts(empty, pack="pop-band-standard")


def test_facts_no_bookmarks_synthesizes_whole_song():
    """无段书签 → 合成「整曲」单段（kind=full，走角色综合默认档）。"""
    score = _score_44()
    score.bookmarks = []
    facts = build_arrange_facts(score, pack="pop-band-standard")
    secs = facts["sections"]
    assert len(secs) == 1
    s0 = secs[0]
    assert s0["synthetic"] is True and s0["kind"] == "full" and s0["kind_source"] == "synthetic"
    assert (s0["start_bar"], s0["end_bar"]) == (1, facts["meter"]["bars_total"])


def test_facts_meter_mismatch_rejected():
    """拍号错配（4/4 包 × 6/8 谱）→ 明确拒绝（拒绝而非修复）。"""
    score = _score_44()
    score.tempo = 120.0
    with pytest.raises(ValueError) as ei:
        build_arrange_facts(score, pack="wotaiko-fast-6-8")
    assert "拍号不匹配" in str(ei.value)


def test_facts_unknown_pack():
    with pytest.raises(FileNotFoundError):
        build_arrange_facts(_score_44(), pack="no-such-pack")
