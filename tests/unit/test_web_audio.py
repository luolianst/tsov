"""M-V8 E2 音频端点单测：import（路径/上传双来源）/ peaks / file 服务 / 护栏 / agent 同路径（/batch）。

约定同 test_web：output/<uuid> 隔离 + rmtree_force；ffmpeg 缺失时相关用例跳过。
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

import tsov.web
from tsov.web import create_app, score_duration

from unit._cleanup import rmtree_force

_HAS_FFMPEG = shutil.which("ffmpeg") is not None


@pytest.fixture()
def env(monkeypatch):
    d = Path("output") / f"webaudiotest-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("tsov.webapp.config.AGENT_SESSION_DIR", str(d / "agent-sessions"))
    app = create_app(output_dir=d)
    client = TestClient(app)
    try:
        yield {"dir": d, "app": app, "client": client}
    finally:
        rmtree_force(d)


def _score_payload():
    return {
        "title": "t",
        "tempo": 120.0,
        "key_candidates": [{"key": "C major", "confidence": 0.7}],
        "tracks": [
            {
                "name": "melody",
                "instrument": {"backend": "fluidsynth", "program": "piano", "volume": 0.8, "effects": []},
                "notes": [],
            }
        ],
        "meta": {},
    }


def _make_project(env, name="p1"):
    r = env["client"].post("/api/projects", json={"name": name, "score": _score_payload()})
    assert r.status_code == 200 and r.json()["ok"] is True
    return name


def _write_wav(path, seconds=0.4, sr=44100):
    t = np.arange(int(seconds * sr)) / sr
    sig = (0.4 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), sig, sr)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 ffmpeg")
def test_audio_import_path_source(env):
    name = _make_project(env)
    src = env["dir"] / "素材.wav"
    _write_wav(src)
    r = env["client"].post(f"/api/projects/{name}/audio/import", json={"path": str(src), "name": "素材"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file"].startswith("audio/") and body["samplerate"] == 44100
    assert (env["dir"] / name / body["file"]).is_file()


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 ffmpeg")
def test_audio_import_upload_source(env):
    name = _make_project(env)
    src = env["dir"] / "up.wav"
    _write_wav(src)
    r = env["client"].post(
        f"/api/projects/{name}/audio/import",
        files={"file": ("up.wav", src.read_bytes(), "audio/wav")},
        data={"name": "上传素材"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["file"].startswith("audio/")


def test_audio_import_guards(env):
    name = _make_project(env)
    r = env["client"].post(f"/api/projects/{name}/audio/import", json={})
    assert r.status_code == 400
    r = env["client"].post(f"/api/projects/{name}/audio/import", json={"path": str(env["dir"] / "nope.wav")})
    assert r.status_code == 400


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 ffmpeg")
def test_audio_peaks_and_file(env):
    name = _make_project(env)
    src = env["dir"] / "p.wav"
    _write_wav(src, seconds=0.5)
    info = env["client"].post(f"/api/projects/{name}/audio/import", json={"path": str(src)}).json()
    rel = info["file"]

    r = env["client"].get(f"/api/projects/{name}/audio/peaks", params={"file": rel, "buckets": 16})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["buckets"] <= 16 and len(body["min"]) == body["buckets"]
    assert max(body["max"]) > 0.2 and min(body["min"]) < -0.2  # 正弦上下都到

    r = env["client"].get(f"/api/projects/{name}/audio/file", params={"file": rel})
    assert r.status_code == 200 and len(r.content) > 1000


def test_audio_path_guard_endpoints(env):
    name = _make_project(env)
    r = env["client"].get(f"/api/projects/{name}/audio/peaks", params={"file": "score.json"})
    assert r.status_code == 400
    r = env["client"].get(f"/api/projects/{name}/audio/peaks", params={"file": "audio/../score.json"})
    assert r.status_code == 400
    r = env["client"].get(f"/api/projects/{name}/audio/file", params={"file": "audio/missing.flac"})
    assert r.status_code == 404


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 ffmpeg")
def test_add_audio_track_via_batch(env):
    """agent 同路径小样：/batch 走命令层 add_audio_track（ADR-0017）。"""
    name = _make_project(env)
    src = env["dir"] / "r.wav"
    _write_wav(src, seconds=0.5)
    info = env["client"].post(f"/api/projects/{name}/audio/import", json={"path": str(src)}).json()
    r = env["client"].post(
        f"/api/projects/{name}/batch",
        json={
            "label": "导入音频",
            "commands": [{"op": "add_audio_track", "value": {"file": info["file"], "offset": 1.0}}],
        },
    )
    assert r.status_code == 200 and r.json()["applied"] == 1, r.text
    st = env["client"].get(f"/api/projects/{name}/state").json()
    tr = st["score"]["tracks"][-1]
    assert tr["kind"] == "audio" and tr["audio"]["offset"] == 1.0


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 ffmpeg")
def test_score_duration_includes_audio(env):
    from tsov.core.score import Score, Track

    name = _make_project(env)
    src = env["dir"] / "d.wav"
    _write_wav(src, seconds=0.5)
    info = env["client"].post(f"/api/projects/{name}/audio/import", json={"path": str(src)}).json()
    proj_root = env["dir"] / name
    score = Score(title="t", tracks=[Track(name="原曲", kind="audio", audio={"file": info["file"], "offset": 1.0})])
    got = score_duration(score, proj_root)
    assert got == pytest.approx(1.0 + 0.5 + 1.0, abs=0.02)  # offset + 文件时长 + 1s 释放尾
