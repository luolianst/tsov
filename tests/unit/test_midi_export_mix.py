"""C 段：MIDI 出口混音/音频轨/拍号 + 422 契约 + schema_version。"""

from __future__ import annotations

import uuid
from pathlib import Path

import numpy as np
import pretty_midi
import pytest

from tsov.core.score import Score
from tsov.core.validate import validate_score
from tsov.host.export import export_matrix
from tsov.host.session import HostSession
from tsov.midi.export import parse_key_signature, score_to_midi

from unit._cleanup import rmtree_force


def _payload():
    return {
        "title": "midi", "tempo": 90.0, "time_signature": "6/8", "key_candidates": [{"key": "D dorian", "confidence": 0.8}],
        "tracks": [
            {
                "name": "lead",
                "instrument": {"backend": "fluidsynth", "program": "piano", "volume": 0.5, "effects": []},
                "pan": -0.5, "mute": False, "solo": False,
                "automation": {"volume": [[0.0, 1.0], [1.0, 0.5]]},
                "notes": [{"start": 0.0, "end": 0.5, "pitch_midi": 60, "pitch_hz": 261.63,
                           "velocity": 0.8, "confidence": 1.0, "deviation_cents": 0.0, "is_ornament": False}],
            },
            {
                "name": "vox", "kind": "audio",
                "instrument": {"backend": "fluidsynth", "program": "piano", "volume": 1.0, "effects": []},
                "audio": {"file": "audio/vox.flac"},
                "notes": [],
            },
        ],
        "meta": {},
    }


def _tmp():
    d = Path("output") / f"midiout-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_midi_export_mix_cc_and_audio_skip():
    d = _tmp()
    try:
        score = Score.from_dict(_payload())
        out = d / "s.mid"
        score_to_midi(score, str(out))
        pm = pretty_midi.PrettyMIDI(str(out))
        assert len(pm.instruments) == 1                 # 音频轨被跳过
        inst = pm.instruments[0]
        cc7 = [(round(e.time, 3), e.value) for e in inst.control_changes if e.number == 7]
        cc10 = [(round(e.time, 3), e.value) for e in inst.control_changes if e.number == 10]
        assert cc7 == [(0.0, 50), (1.0, 25)]            # 0.5 推子 × automation 1.0→0.5
        assert cc10 and cc10[0] == (0.0, 32)            # pan -0.5 → 32
        ts = pm.time_signature_changes[0]
        assert (ts.numerator, ts.denominator) == (6, 8)
    finally:
        rmtree_force(d)


def test_midi_export_mute_and_solo():
    d = _tmp()
    try:
        data = _payload()
        data["tracks"][0]["mute"] = True
        score = Score.from_dict(data)
        out = d / "m.mid"
        score_to_midi(score, str(out))
        inst = pretty_midi.PrettyMIDI(str(out)).instruments[0]
        cc7 = [e for e in inst.control_changes if e.number == 7]
        assert cc7 and all(e.value == 0 for e in cc7)
    finally:
        rmtree_force(d)


def test_key_signature_modes():
    assert parse_key_signature("D dorian") == 0        # C 大调调号
    assert parse_key_signature("E phrygian") == 0
    assert parse_key_signature("F lydian") == 0
    assert parse_key_signature("G mixolydian") == 0
    assert parse_key_signature("A minor") == 21
    assert parse_key_signature("F# lydian") == 1       # C# 大调


def test_midi_stems_keep_time_signature(monkeypatch):
    d = _tmp()
    try:
        score = Score.from_dict(_payload())
        (d / "audio").mkdir(parents=True, exist_ok=True)
        import soundfile as sf
        sf.write(str(d / "audio" / "vox.flac"), np.zeros((100, 2), dtype=np.float32), 44100, format="FLAC")
        session = HostSession.from_score(score, soundfont="dummy.sf2", samplerate=44100,
                                         base_dir=d, lazy=True)
        monkeypatch.setattr("tsov.host.export.render_buses",
                            lambda *a, **k: np.zeros((100, 2), dtype=np.float32))
        monkeypatch.setattr("tsov.host.export.write_wav",
                            lambda audio, path, sr, subtype="PCM_16": (Path(path).parent.mkdir(parents=True, exist_ok=True),
                                                                      Path(path).write_bytes(b"RIFF"),
                                                                      str(path))[2])
        rep = export_matrix(session, d, mix=False, buses=False, stems=False,
                            midi=False, midi_stems=True)
        assert list(rep["midi_stems"]) == ["lead"]     # 音频轨无 MIDI stem
        pm = pretty_midi.PrettyMIDI(rep["midi_stems"]["lead"])
        ts = pm.time_signature_changes[0]
        assert (ts.numerator, ts.denominator) == (6, 8)
    finally:
        rmtree_force(d)


def test_schema_version_gate():
    data = _payload()
    data["meta"] = {"schema_version": 1}
    assert validate_score(Score.from_dict(data)) == []
    data["meta"] = {"schema_version": 2}
    errs = validate_score(Score.from_dict(data))
    assert any("schema_version" in e for e in errs)
