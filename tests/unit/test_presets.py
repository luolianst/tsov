"""预设库（tsov/presets.py）单测：加载/校验/应用/序列化。"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest

from tsov.core.notes import Note
from tsov.core.score import Instrument, Score, Track
from tsov.presets import apply_effect_preset, load_library


def _tmp_root() -> Path:
    return Path("output") / f"presettest-{uuid.uuid4().hex[:8]}"


def _write(root: Path, kind: str, name: str, data: dict) -> None:
    d = root / kind
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_repo_presets_load_clean():
    lib = load_library()  # 仓库 presets/ 目录
    assert lib.errors == []
    assert "piano-pop-reverb" in lib.effects
    p = lib.get_effect("piano-pop-reverb")
    assert p.chain and p.chain[0].type == "compressor"


def test_bad_presets_reported_not_silent():
    root = _tmp_root()
    try:
        _write(root, "effects", "bad-type", {"name": "bad-type", "chain": [{"type": "nope", "params": {}}]})
        _write(root, "effects", "bad-param", {"name": "bad-param", "chain": [{"type": "reverb", "params": {"bogus": 1}}]})
        _write(root, "effects", "ok", {"name": "ok", "chain": [{"type": "gain", "params": {"gain_db": 3}}]})
        lib = load_library(root)
        assert "ok" in lib.effects and len(lib.errors) == 2
        assert any("bad-type" in e for e in lib.errors)
        assert any("bad-param" in e for e in lib.errors)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_apply_effect_preset_by_index_and_name():
    score = Score(
        title="t", tempo=100.0,
        tracks=[
            Track(name="melody", instrument=Instrument(program="piano"),
                  notes=[Note(start=0.0, end=1.0, pitch_midi=60, pitch_hz=261.63, velocity=0.8)]),
            Track(name="bass", instrument=Instrument(program="bass"), notes=[]),
        ],
    )
    lib = load_library()
    s1 = apply_effect_preset(score, 0, "piano-pop-reverb", library=lib)
    s2 = apply_effect_preset(score, "melody", "piano-pop-reverb", library=lib)
    assert len(s1.tracks[0].instrument.effects) == 2
    assert [e.type for e in s2.tracks[0].instrument.effects] == ["compressor", "reverb"]
    # 原 Score 不被改动（深拷贝）
    assert score.tracks[0].instrument.effects == []
    # 序列化往返
    d = s1.to_dict()
    s3 = Score.from_dict(d)
    assert [e.type for e in s3.tracks[0].instrument.effects] == ["compressor", "reverb"]
    assert s3.tracks[0].instrument.effects[1].params["room_size"] == pytest.approx(0.62)


def test_apply_effect_preset_track_not_found():
    lib = load_library()
    score = Score(title="t", tempo=100.0, tracks=[Track(name="a")])
    with pytest.raises(KeyError):
        apply_effect_preset(score, "nope", "piano-pop-reverb", library=lib)
    with pytest.raises(KeyError):
        lib.get_effect("no-such-preset")


def test_arrangement_preset_schema_checks():
    root = _tmp_root()
    try:
        _write(root, "arrangements", "good", {
            "name": "good", "instruments": [
                {"role": "piano", "gm_program": 0},
                {"role": "bass", "gm_program": 38},
            ],
        })
        _write(root, "arrangements", "dup", {
            "name": "dup", "instruments": [{"role": "piano"}, {"role": "piano"}],
        })
        _write(root, "arrangements", "norole", {"name": "norole", "instruments": [{"gm_program": 0}]})
        _write(root, "arrangements", "prog", {"name": "prog", "instruments": [{"role": "x", "gm_program": 999}]})
        lib = load_library(root)
        assert "good" in lib.arrangements
        assert len(lib.errors) == 3
    finally:
        shutil.rmtree(root, ignore_errors=True)
