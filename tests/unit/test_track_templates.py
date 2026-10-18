"""S4.2 轨模板：布局存取 / 命令层应用 / REST。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tsov.core.score import Effect, Instrument, Score, Track
from tsov.host.command import EditBatch
from tsov.host.project import Project
from tsov.track_templates import TemplateError, TrackTemplateLibrary
from tsov.web import create_app


def _score() -> Score:
    return Score(title="layout", tracks=[
        Track(name="lead", instrument=Instrument(
            program="piano", volume=0.7,
            effects=[Effect(type="reverb", params={})],
        ), pan=0.2),
        Track(name="bass", instrument=Instrument(program="bass", soundfont="/tmp/bass.sf2")),
    ])


def test_template_roundtrip_and_apply_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("TSOV_TEMPLATES_PATH", str(tmp_path / "templates"))
    lib = TrackTemplateLibrary()
    tpl = lib.save_from_score("band", _score(), title="Band layout")
    assert (tmp_path / "templates" / "band.json").is_file()
    assert lib.list()[0]["name"] == "band"

    again = lib.get("band")
    assert again.title == "Band layout"
    assert len(again.tracks) == 2
    assert "notes" not in again.to_dict()["tracks"][0]

    batch = EditBatch(label="apply-template")
    for cmd in again.commands(start_index=0):
        batch.add(cmd["op"], track=int(cmd.get("track", 0)), value=cmd.get("value"))
    out, result = batch.apply(Score(title="new", tracks=[]))
    assert result.ok, result.errors
    assert [t.name for t in out.tracks] == ["lead", "bass"]
    lead, bass = out.tracks
    assert lead.instrument.program == "piano"
    assert lead.instrument.volume == pytest.approx(0.7)
    assert lead.pan == pytest.approx(0.2)
    assert len(lead.instrument.effects) == 1
    assert bass.instrument.program == "bass"
    assert bass.instrument.soundfont == "/tmp/bass.sf2"


def test_template_name_and_overwrite_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("TSOV_TEMPLATES_PATH", str(tmp_path / "templates"))
    lib = TrackTemplateLibrary()
    lib.save_from_score("x", _score())
    with pytest.raises(TemplateError, match="已存在"):
        lib.save_from_score("x", _score())
    lib.save_from_score("x", _score(), overwrite=True)
    with pytest.raises(TemplateError, match="模板名非法"):
        lib.save_from_score("../bad", _score())


def test_template_api_save_and_apply(tmp_path, monkeypatch):
    monkeypatch.setenv("TSOV_TEMPLATES_PATH", str(tmp_path / "templates"))
    monkeypatch.setenv("TSOV_SOURCES_PATH", str(tmp_path / "sources.json"))
    out = tmp_path / "output"
    Project.create("src", _score(), parent=out)
    Project.create("dst", Score(title="dst", tracks=[]), parent=out)
    app = create_app(out)
    with TestClient(app) as c:
        r = c.post("/api/projects/src/template", json={"name": "band"})
        assert r.status_code == 200, r.text
        assert r.json()["template"]["tracks"] == 2

        r = c.get("/api/templates")
        assert r.status_code == 200, r.text
        assert r.json()["templates"][0]["name"] == "band"

        r = c.post("/api/projects/dst/template/apply", json={"name": "band"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["applied"] > 0 and not body["errors"], body

        state = c.get("/api/projects/dst/state").json()
        names = [t["name"] for t in state["score"]["tracks"]]
        assert names == ["lead", "bass"]
        assert state["score"]["tracks"][1]["instrument"]["soundfont"] == "/tmp/bass.sf2"
