"""M-V2 Web 壳单测（docs/05 前端接口契约）：REST 路由 + SSE 事件流 + agent 会话（mock LLM）。

约定：
- 不用 pytest tmp_path（handoff 坑 81：被安全软件锁）——手动 output/<uuid> 目录 + teardown rmtree
- LLM 用 monkeypatch 假 chat（edit_score 走 annotations 纯程序路径，不触网）
- 渲染用例依赖 vendor/soundfonts/FluidR3_GM.sf2（缺则跳过）
"""

from __future__ import annotations

import shutil
import threading
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import tsov.agent
from tsov.web import create_app

_HAS_SF = Path("vendor/soundfonts/FluidR3_GM.sf2").is_file()


@pytest.fixture()
def env():
    """独立 output 目录 + app + client（每用例隔离）。"""
    d = Path("output") / f"webtest-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    app = create_app(output_dir=d)
    client = TestClient(app)
    try:
        yield {"dir": d, "app": app, "client": client}
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _score_payload(n_pitches=(60, 62, 64)):
    notes = []
    t = 0.0
    for i, p in enumerate(n_pitches):
        notes.append({
            "start": round(t, 3), "end": round(t + 0.25, 3), "pitch_midi": p,
            "pitch_hz": 440.0 * 2 ** ((p - 69) / 12), "velocity": 0.8, "confidence": 0.9,
            "deviation_cents": 0.0, "is_ornament": False,
        })
        t += 0.3
    return {
        "title": "webtest", "tempo": 120.0, "key_candidates": [{"key": "C major", "confidence": 0.7}],
        "tracks": [{
            "name": "melody",
            "instrument": {"backend": "fluidsynth", "program": "piano", "volume": 0.8, "effects": []},
            "notes": notes,
        }],
        "meta": {},
    }


def _make_project(env, name="p1", score=None):
    r = env["client"].post("/api/projects", json={"name": name, "score": score or _score_payload()})
    assert r.status_code == 200 and r.json()["ok"] is True
    return name


# ---------------------------------------------------------------------------
# 基础路由
# ---------------------------------------------------------------------------


def test_health_and_static(env):
    r = env["client"].get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["version"]

    r = env["client"].get("/")
    assert r.status_code == 200
    assert "可视化宿主" in r.text


def test_project_create_list_state(env):
    _make_project(env)
    r = env["client"].get("/api/projects")
    assert r.json() == {"projects": ["p1"]}

    r = env["client"].get("/api/projects/p1/state")
    assert r.status_code == 200
    s = r.json()
    assert set(s) == {"project", "score", "selection", "history", "git_log", "summary"}
    assert len(s["score"]["tracks"][0]["notes"]) == 3
    assert s["history"] == {"can_undo": False, "can_redo": False}
    assert any("init" in ln for ln in s["git_log"])
    assert "tempo=120" in s["summary"]


def test_project_create_duplicate_and_bad_name(env):
    _make_project(env)
    r = env["client"].post("/api/projects", json={"name": "p1"})
    assert r.status_code == 400 and "error" in r.json()

    for bad in ("../evil", "a/b", "", ".", "x:y"):
        r = env["client"].post("/api/projects", json={"name": bad})
        assert r.status_code == 400, bad


def test_missing_project_404(env):
    r = env["client"].get("/api/projects/nope/state")
    assert r.status_code == 404 and "工程不存在" in r.json()["error"]


# ---------------------------------------------------------------------------
# 编辑事务 + 历史
# ---------------------------------------------------------------------------


def test_batch_transpose_undo_redo_rollback(env):
    _make_project(env)
    c = env["client"]

    r = c.post("/api/projects/p1/batch", json={
        "label": "+2", "commands": [{"op": "transpose", "track": 0, "index": None, "value": 2}],
        "commit_message": "第一轮：+2",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] == 1 and body["commit"]
    assert body["diff"]["total"] >= 1  # 全部音高变化 → added/removed 或 changed

    state = c.get("/api/projects/p1/state").json()
    assert state["history"]["can_undo"] is True
    assert any("第一轮" in ln for ln in state["git_log"])
    assert state["score"]["tracks"][0]["notes"][0]["pitch_midi"] == 62

    # undo → 回到 60；redo → 62
    assert c.post("/api/projects/p1/undo").json()["ok"] is True
    assert c.get("/api/projects/p1/state").json()["score"]["tracks"][0]["notes"][0]["pitch_midi"] == 60
    assert c.post("/api/projects/p1/redo").json()["ok"] is True
    assert c.get("/api/projects/p1/state").json()["score"]["tracks"][0]["notes"][0]["pitch_midi"] == 62

    # undo 无历史 → 400
    c.post("/api/projects/p1/undo")
    c.post("/api/projects/p1/undo")
    r = c.post("/api/projects/p1/undo")
    assert r.status_code == 400

    # 第二轮（此刻谱在 60）→ 63；rollback HEAD~1 → 回到 +2 版本（62）
    c.post("/api/projects/p1/batch", json={
        "label": "+3", "commands": [{"op": "transpose", "track": 0, "index": None, "value": 3}],
    })
    before = c.get("/api/projects/p1/state").json()["score"]["tracks"][0]["notes"][0]["pitch_midi"]
    assert before == 63
    r = c.post("/api/projects/p1/rollback", json={"rev": "HEAD~1"})
    assert r.status_code == 200
    assert c.get("/api/projects/p1/state").json()["score"]["tracks"][0]["notes"][0]["pitch_midi"] == 62


def test_batch_all_rejected_keeps_project(env):
    _make_project(env)
    c = env["client"]
    r = c.post("/api/projects/p1/batch", json={
        "label": "bad", "commands": [{"op": "set_pitch", "track": 0, "index": 99, "value": 72}],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] == 0 and body["errors"]
    state = c.get("/api/projects/p1/state").json()
    assert state["history"]["can_undo"] is False  # 工程未动


def test_log_and_summary(env):
    _make_project(env)
    c = env["client"]
    r = c.get("/api/projects/p1/log")
    assert r.status_code == 200 and r.json()["log"]
    r = c.get("/api/projects/p1/summary")
    assert "project=p1" in r.json()["summary"]


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------


def test_sse_heartbeat_only(env, monkeypatch):
    """无写入：连通帧 + 心跳帧（SSE 传输层；close_after 收流避免无限流）。"""
    import tsov.web as web_mod
    monkeypatch.setattr(web_mod, "SSE_HEARTBEAT_SEC", 0.1)

    _make_project(env)
    c = env["client"]

    r = c.get("/api/projects/p1/events?close_after=2")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert ": connected" in r.text and ": ping" in r.text


def test_sse_stream_events(env, monkeypatch):
    """并发写（batch）→ SSE 推 diff_applied + state_updated（跨线程 publish → asyncio 队列）。"""
    import tsov.web as web_mod
    monkeypatch.setattr(web_mod, "SSE_HEARTBEAT_SEC", 0.1)

    _make_project(env)
    c = env["client"]

    posted = threading.Event()

    def poster():
        time.sleep(0.3)
        pc = TestClient(env["app"])
        resp = pc.post("/api/projects/p1/batch", json={
            "label": "+1", "commands": [{"op": "transpose", "track": 0, "index": None, "value": 1}],
        })
        if resp.status_code == 200:
            posted.set()

    threading.Thread(target=poster, daemon=True).start()

    r = c.get("/api/projects/p1/events?close_after=15")  # 15 帧 ≈ 1.5s 余量；事件应在 ~0.4s 到
    assert r.status_code == 200
    assert posted.is_set(), "poster 线程未完成"
    assert "event: diff_applied" in r.text
    assert "event: state_updated" in r.text


# ---------------------------------------------------------------------------
# chat（mock LLM：edit_score annotations 纯程序路径 → 采用 → commit）
# ---------------------------------------------------------------------------


def test_chat_agent_round_trip(env, monkeypatch):
    name = _make_project(env)
    proj_root = env["dir"] / name
    c = env["client"]

    calls = {"n": 0}

    def fake_chat(messages, tools=None, **params):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "name": "edit_score",
                    "arguments": {
                        "score_path": str(proj_root / "score.json"),
                        "annotations": [{"index": 0, "action": "pitch", "value": 74}],
                        "output": str(proj_root / "agent-edited-score.json"),
                    },
                }],
                "message": {"role": "assistant", "content": "", "tool_calls": []},
            }
        return {"content": "已把第 0 个音改成 D5(74)。", "tool_calls": [], "message": {"role": "assistant", "content": "done"}}

    monkeypatch.setattr(tsov.agent, "chat", fake_chat)

    r = c.post("/api/chat", json={"project": name, "message": "把第一个音改成 D5"})
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] and body["session_path"].endswith(".jsonl")
    # agent 线程异步起跑：等会话 JSONL 落第一行（带 deadline，防竞态）
    deadline = time.time() + 10
    while time.time() < deadline and not Path(body["session_path"]).is_file():
        time.sleep(0.05)
    assert Path(body["session_path"]).is_file()

    # 等 agent 线程收尾（采用 + commit）
    deadline = time.time() + 10
    while time.time() < deadline:
        log = c.get(f"/api/projects/{name}/log").json()["log"]
        if any("agent：" in ln for ln in log):
            break
        time.sleep(0.1)

    log = c.get(f"/api/projects/{name}/log").json()["log"]
    assert any("agent：" in ln for ln in log)
    state = c.get(f"/api/projects/{name}/state").json()
    assert state["score"]["tracks"][0]["notes"][0]["pitch_midi"] == 74
    assert state["history"]["can_undo"] is True

    # 撤销本轮 → 回 60
    assert c.post(f"/api/projects/{name}/undo").json()["ok"] is True
    state = c.get(f"/api/projects/{name}/state").json()
    assert state["score"]["tracks"][0]["notes"][0]["pitch_midi"] == 60


def test_chat_lock_exclusive(env):
    name = _make_project(env)
    env["app"].state.tsov.agent_lock.acquire()
    try:
        r = env["client"].post("/api/chat", json={"project": name, "message": "hi"})
        assert r.status_code == 409
        assert "error" in r.json()
    finally:
        env["app"].state.tsov.agent_lock.release()


# ---------------------------------------------------------------------------
# 渲染 / wav（依赖 SoundFont）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_SF, reason="缺 vendor/soundfonts/FluidR3_GM.sf2")
def test_render_and_wav(env):
    name = _make_project(env)
    c = env["client"]

    r = c.post(f"/api/projects/{name}/render", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["duration"] > 0 and body["sr"] == 44100
    assert (env["dir"] / name / "render.wav").is_file()

    r = c.get(f"/api/projects/{name}/wav")
    assert r.status_code == 200
    assert r.content[:4] == b"RIFF"
