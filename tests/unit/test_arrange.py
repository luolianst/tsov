"""M7 配器 + 节奏单元测试：BPM 估计 / 和弦选择 / 多轨导出 / 打击乐 pattern。"""

import pytest

from tsov.arrange import arrange, drums, harmonize
from tsov.core.notes import Note
from tsov.core.score import Instrument, Score, Track
from tsov.dsp.tempo import estimate_bpm


def _melody(notes):
    return [Note(start=s, end=s + 0.4, pitch_midi=p, pitch_hz=0.0) for s, p in notes]


def _score(notes):
    return Score(
        title="t", tempo=120.0, key_candidates=[],
        tracks=[Track(name="melody", instrument=Instrument(program="piano"), notes=_melody(notes))],
    )


# ---- BPM 估计 ----

def test_estimate_bpm_regular():
    # 每 0.5s 一个音 → 120 BPM
    notes = _melody([(i * 0.5, 60) for i in range(10)])
    bpm, conf = estimate_bpm(notes)
    assert abs(bpm - 120.0) < 5.0
    assert conf > 0.5


def test_estimate_bpm_fallback_few_notes():
    bpm, conf = estimate_bpm(_melody([(0.0, 60), (0.5, 62)]))
    assert bpm == 120.0 and conf == 0.0


# ---- 和弦选择 ----

def test_harmonize_chords_in_dorian_scale():
    notes = _melody([(0.0, 50), (0.5, 52), (1.0, 47), (1.5, 59), (2.0, 45), (2.5, 48)])  # D dorian 音
    chords = harmonize(notes, "D dorian", bpm=120.0)
    dorian = {2, 4, 5, 7, 9, 11, 0}
    for c in chords:
        assert c.tones <= dorian, f"和弦 {c} 有调外音"


def test_harmonize_tonic_priority():
    # 全 D 音旋律 → 首选 Dm
    notes = _melody([(i * 0.5, 50) for i in range(8)])
    chords = harmonize(notes, "D dorian", bpm=120.0)
    assert all(c.root_pc == 2 for c in chords)


# ---- 多轨导出 ----

def test_score_to_midi_multitrack(tmp_path):
    from tsov.midi.export import score_to_midi

    import pretty_midi

    arranged = arrange(_score([(0.0, 60), (1.0, 62)]), key="C major")
    path = tmp_path / "multi.mid"
    score_to_midi(arranged, str(path))
    pm = pretty_midi.PrettyMIDI(str(path))
    assert len(pm.instruments) == 4
    names_programs = [(i.is_drum, i.program) for i in pm.instruments]
    assert any(is_drum for is_drum, _ in names_programs)  # 打击乐轨


# ---- 打击乐 pattern ----

def test_drums_pattern():
    from tsov.arrange.harmony import Chord

    chords = [Chord(root_pc=2, kind="minor", start=0.0, end=4.0, tones={2, 5, 9})]
    notes = drums(chords, style="pop")
    kicks = [n.pitch_midi for n in notes if n.pitch_midi == 36]
    snares = [n.pitch_midi for n in notes if n.pitch_midi == 38]
    hihats = [n.pitch_midi for n in notes if n.pitch_midi == 42]
    assert len(kicks) == 2 and len(snares) == 2  # kick 1/3 拍、snare 2/4 拍
    assert len(hihats) == 8  # 8 分音符 × 4 拍


# ---- arrange 组装 ----

def test_arrange_four_tracks():
    arranged = arrange(_score([(0.0, 60), (0.5, 62), (1.0, 64)]), key="C major")
    assert [t.name for t in arranged.tracks] == ["melody", "harmony", "bass", "drums"]
    assert all(len(t.notes) > 0 for t in arranged.tracks)
    assert arranged.tracks[1].instrument.program == "strings"
    assert arranged.tracks[2].instrument.program == "bass"
    assert arranged.tracks[3].instrument.program == "drums"


def test_arrange_empty_melody_rejected():
    score = Score(title="t", tracks=[Track(name="melody", instrument=Instrument(), notes=[])])
    with pytest.raises(ValueError):
        arrange(score)


def test_arrange_tracks_have_pitch_hz():
    """M7-FIX：配器轨（harmony/bass/drums）pitch_hz 不能为 0（ADR-0005 完整性）。"""
    arranged = arrange(_score([(0.0, 60), (0.5, 62), (1.0, 64)]), key="C major")
    for track in arranged.tracks:
        if track.name == "melody":  # 旋律继承输入谱，本测试只查配器轨
            continue
        for n in track.notes:
            assert n.pitch_hz > 0, f"{track.name} 存在 pitch_hz=0 的音"


def test_arrange_harmony_octave_reasonable():
    """M7-FIX：和声轨应在旋律下方 1-2 个八度，不能低到 3 个八度（midi 12-23）。"""
    arranged = arrange(_score([(0.0, 60), (0.5, 62), (1.0, 64)]), key="C major")
    mel = [n.pitch_midi for n in arranged.tracks[0].notes]
    harm = [n.pitch_midi for n in arranged.tracks[1].notes]
    mel_min, mel_max = min(mel), max(mel)
    assert all(mel_min - 24 <= p <= mel_max + 6 for p in harm), f"harmony 越界: {min(harm)}-{max(harm)} vs melody {mel_min}-{mel_max}"
