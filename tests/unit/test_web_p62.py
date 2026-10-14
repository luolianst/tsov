"""P62a web 面回归（2026-10-14）：导出静音门 409（结构化 warnings 透传）+ 无 vst3 零成本直通。"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import tsov.web
from tsov.web import create_app

from unit._cleanup import rmtree_force


@pytest.fixture()
def env(monkeypatch):
    d = Path("output") / f"p62test-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("tsov.webapp.config.AGENT_SESSION_DIR", str(d / "agent-sessions"))
    app = create_app(output_dir=d)
    client = TestClient(app)
    yield {"dir": d, "client": client}
    rmtree_force(d)


def _note():
    return {"start": 0.0, "end": 0.5, "pitch_midi": 36, "pitch_hz": 73.42, "velocity": 0.9}


def _score_json(program: str) -> dict:
    return {
        "title": "p62-test", "tempo": 120.0, "time_signature": "4/4",
        "key_candidates": [], "meta": {},
        "tracks": [{
            "name": "MT",
            "instrument": {"backend": "fluidsynth", "program": program, "volume": 1.0,
                           "effects": [], "params": {}, "preset": None},
            "notes": [_note()],
        }],
    }


def _mk_project(root: Path, name: str, score: dict) -> Path:
    pdir = root / name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "score.json").write_text(json.dumps(score), encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=pdir, check=True)
    subprocess.run(["git", "config", "user.email", "p62test@local"], cwd=pdir, check=True)
    subprocess.run(["git", "config", "user.name", "p62test"], cwd=pdir, check=True)
    return pdir


def test_export_gate_blocks_with_structured_warnings(env, monkeypatch):
    root = env["dir"]
    _mk_project(root, "p62-gate", _score_json(r"vst3:D:\VST3\MT-PowerDrumKit.vst3"))
    fake = [{"code": "instrument_silent", "track": 0, "name": "MT",
             "plugin": r"D:\VST3\MT-PowerDrumKit.vst3", "plugin_name": "MT-PowerDrumKit",
             "notes_count": 1, "peak": 0.0, "hint": "点 START"}]
    monkeypatch.setattr("tsov.webapp.routes.render._pre_export_silence_check",
                        lambda engine, proj: fake)
    r = env["client"].post("/api/projects/p62-gate/export",
                           json={"mix": True, "stems": False, "midi": False})
    assert r.status_code == 409, r.text
    body = r.json()
    assert "error" in body and body["warnings"][0]["code"] == "instrument_silent"


def test_pre_export_check_zero_cost_without_vst3():
    from tsov.core.score import Score
    from tsov.webapp.routes.render import _pre_export_silence_check

    score = Score.from_dict(_score_json(""))    # 普通 GM 轨（空 program）

    class _P:   # 最小 proj 假件；无 vst3 时不应触碰 engine/root
        pass

    p = _P()
    p.score = score
    p.root = Path(".")
    assert _pre_export_silence_check(None, p) == []
