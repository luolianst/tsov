"""P0 A8：安全边界（render out / pack 白名单 / zip-slip / 录音跨工程）。"""

from __future__ import annotations

import importlib.util
import json
import threading
import uuid
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tsov.arrange.library import load_patterns
from tsov.core.score import Score
from tsov.tune.facts import resolve_targets
from tsov.web import create_app

from unit._cleanup import rmtree_force


@pytest.fixture()
def env(monkeypatch):
    d = Path("output") / f"sec-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("tsov.webapp.config.AGENT_SESSION_DIR", str(d / "agent-sessions"))
    app = create_app(output_dir=d)
    client = TestClient(app)
    try:
        yield {"dir": d, "app": app, "client": client}
    finally:
        rmtree_force(d)


def _payload():
    return {
        "title": "s", "tempo": 120.0, "time_signature": "4/4", "key_candidates": [],
        "tracks": [{
            "name": "m",
            "instrument": {"backend": "fluidsynth", "program": "piano", "volume": 0.8, "effects": []},
            "notes": [],
        }],
        "meta": {},
    }


class _Engine:
    samplerate = 44100


def test_render_out_must_stay_inside_project(env):
    env["client"].post("/api/projects", json={"name": "p1", "score": _payload()})
    env["app"].state.tsov._engine = _Engine()
    r = env["client"].post("/api/projects/p1/render", json={"out": "../../escape.wav"})
    assert r.status_code == 400 and "工程目录内" in r.json()["error"]
    assert not (env["dir"].parent.parent / "escape.wav").exists()


def test_pack_names_reject_traversal():
    with pytest.raises(ValueError):
        load_patterns("../evil")
    with pytest.raises(ValueError):
        resolve_targets(Score.from_dict(_payload()), pack="../../evil")


def _load_fetch_module():
    path = Path("scripts/fetch_engines.py").resolve()
    spec = importlib.util.spec_from_file_location("fetch_engines_test", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_zip_slip_member_is_skipped(tmp_path=None):
    base = Path("output") / f"zip-{uuid.uuid4().hex[:10]}"
    base.mkdir(parents=True, exist_ok=True)
    try:
        zpath = base / "evil.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("Evil.vst3/Contents/x86_64-win/Evil.vst3", b"binary")
            zf.writestr("Evil.vst3/../../pwned.txt", b"pwned")
        fe = _load_fetch_module()
        got = fe.extract_vst3_bundle(zpath, base / "vst3")
        assert got and got[0] == "Evil.vst3"
        assert (base / "vst3" / "Evil.vst3" / "Contents" / "x86_64-win" / "Evil.vst3").is_file()
        assert not (base / "pwned.txt").exists()
        assert not (base / "vst3" / "pwned.txt").exists()
    finally:
        rmtree_force(base)


def test_record_stop_rejects_other_project(env):
    from tsov.webapp.routes import audio as audio_route

    c = env["client"]
    c.post("/api/projects", json={"name": "a", "score": _payload()})
    c.post("/api/projects", json={"name": "b", "score": _payload()})

    class _AliveThread:
        def is_alive(self):
            return True

        def join(self, timeout=None):
            pass

    rec = audio_route._recorder
    old = (rec.thread, rec.project, rec.stop_event)
    try:
        rec.thread = _AliveThread()
        rec.project = "a"
        rec.stop_event = threading.Event()
        r = c.post("/api/projects/b/record/stop", json={})
        assert r.status_code == 409 and "属于工程" in r.json()["error"]
        assert rec.stop_event.is_set() is False   # 没有误停别人的录音
    finally:
        rec.thread, rec.project, rec.stop_event = old
