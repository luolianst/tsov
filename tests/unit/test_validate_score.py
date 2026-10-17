"""P0 A3：validate_score 值域检查 + apply_score 入口闸门。"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

import pytest

from tsov.core.score import Score
from tsov.core.validate import validate_score
from tsov.host.project import Project

from unit._cleanup import rmtree_force


def _payload():
    return {
        "title": "v", "tempo": 120.0, "time_signature": "4/4", "key_candidates": [],
        "tracks": [{
            "name": "m",
            "instrument": {"backend": "fluidsynth", "program": "piano", "volume": 0.8, "effects": []},
            "notes": [{"start": 0.0, "end": 0.5, "pitch_midi": 60, "pitch_hz": 261.63,
                       "velocity": 0.8, "confidence": 0.9, "deviation_cents": 0.0, "is_ornament": False}],
        }],
        "meta": {},
    }


@pytest.fixture()
def base():
    d = Path("output") / f"validate-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        rmtree_force(d)


def test_good_score_passes():
    assert validate_score(Score.from_dict(_payload())) == []


@pytest.mark.parametrize("mutate, needle", [
    (lambda d: d["tracks"][0]["notes"][0].update({"pitch_midi": 128}), "pitch_midi"),
    (lambda d: d["tracks"][0]["notes"][0].update({"pitch_midi": -1}), "pitch_midi"),
    (lambda d: d["tracks"][0]["notes"][0].update({"start": -0.5}), "start"),
    (lambda d: d["tracks"][0]["notes"][0].update({"end": 0.0}), "end 必须大于 start"),
    (lambda d: d["tracks"][0]["notes"][0].update({"velocity": 1.5}), "velocity"),
    (lambda d: d["tracks"][0].update({"pan": 2.0}), "pan"),
    (lambda d: d["tracks"][0]["instrument"].update({"volume": 3.0}), "instrument.volume"),
    (lambda d: d.update({"tempo": 0}), "tempo"),
    (lambda d: d.update({"time_signature": "6/7"}), "time_signature"),
])
def test_bad_values_are_rejected_with_location(mutate, needle):
    d = _payload()
    mutate(d)
    errors = validate_score(Score.from_dict(d))
    assert any(needle in e for e in errors), errors
    assert any(e.startswith(("track[", "bus[", "tempo", "time_signature")) for e in errors), errors


def test_apply_score_rejects_invalid_and_keeps_file(base):
    proj = Project.create("p", Score.from_dict(_payload()), parent=base)
    path = proj.root / "score.json"
    before = path.read_text(encoding="utf-8")

    bad = copy.deepcopy(proj.score)
    bad.tracks[0].notes[0].pitch_midi = 200
    r = proj.apply_score(bad, "坏谱")

    assert r["ok"] is False and r["applied"] == 0
    assert any("pitch_midi" in e for e in r["errors"])
    assert path.read_text(encoding="utf-8") == before
    assert proj.score.tracks[0].notes[0].pitch_midi == 60


def test_apply_score_accepts_valid_change(base):
    proj = Project.create("p", Score.from_dict(_payload()), parent=base)
    good = copy.deepcopy(proj.score)
    good.title = "改好了"
    r = proj.apply_score(good, "好谱")
    assert r["ok"] is True and r["applied"] == 1
    assert json.loads((proj.root / "score.json").read_text(encoding="utf-8"))["title"] == "改好了"
