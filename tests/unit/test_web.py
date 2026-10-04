"""M-V2 Web 壳单测（docs/05 前端接口契约）：REST 路由 + SSE 事件流 + agent 会话（mock LLM）。

约定：
- 不用 pytest tmp_path（已知坑 81：被安全软件锁）——手动 output/<uuid> 目录 + teardown rmtree
- LLM 用 monkeypatch 假 chat（edit_score 走 annotations 纯程序路径，不触网）
- 渲染用例依赖 vendor/soundfonts/FluidR3_GM.sf2（缺则跳过）
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import tsov.agent
import tsov.web
from tsov.web import create_app

from unit._cleanup import rmtree_force

_HAS_SF = Path("vendor/soundfonts/FluidR3_GM.sf2").is_file()


@pytest.fixture()
def env(monkeypatch):
    """独立 output 目录 + app + client（每用例隔离）。
    agent 会话落盘同样隔离到 <d>/agent-sessions——不再污染仓库 output/agent-sessions/（2026-09-21）。
    """
    d = Path("output") / f"webtest-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("tsov.webapp.config.AGENT_SESSION_DIR", str(d / "agent-sessions"))
    app = create_app(output_dir=d)
    client = TestClient(app)
    try:
        yield {"dir": d, "app": app, "client": client}
    finally:
        rmtree_force(d)   # 清理：.git/objects 只读属性 → chmod 强删（见 _cleanup.py）


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


def test_meta_endpoint(env):
    """UI 批A：面板元数据（GM 音色名 / 效果类型）。"""
    r = env["client"].get("/api/meta")
    assert r.status_code == 200
    body = r.json()
    assert "piano" in body["programs"] and "synth_bass" in body["programs"]
    assert set(body["effect_kinds"]) >= {"reverb", "delay", "compressor", "gain"}


def test_project_create_list_state(env):
    _make_project(env)
    r = env["client"].get("/api/projects")
    assert [p["name"] for p in r.json()["projects"]] == ["p1"]

    r = env["client"].get("/api/projects/p1/state")
    assert r.status_code == 200
    s = r.json()
    assert set(s) == {"project", "score", "selection", "history", "git_log", "summary", "duration", "saved_at"}
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

    log0 = c.get("/api/projects/p1/log").json()["log"]

    r = c.post("/api/projects/p1/batch", json={
        "label": "+2", "commands": [{"op": "transpose", "track": 0, "index": None, "value": 2}],
        "commit_message": "第一轮：+2",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] == 1 and body["commit"] is None and body["seq"] == 1   # M-V7 D2：零 commit
    assert body["diff"]["total"] >= 1  # 全部音高变化 → added/removed 或 changed

    state = c.get("/api/projects/p1/state").json()
    assert state["history"]["can_undo"] is True
    assert c.get("/api/projects/p1/log").json()["log"] == log0      # 编辑不落 git
    assert state["score"]["tracks"][0]["notes"][0]["pitch_midi"] == 62

    # undo → 回到 60；redo → 62（窗口游标，零 commit）
    assert c.post("/api/projects/p1/undo").json()["ok"] is True
    assert c.get("/api/projects/p1/state").json()["score"]["tracks"][0]["notes"][0]["pitch_midi"] == 60
    assert c.post("/api/projects/p1/redo").json()["ok"] is True
    assert c.get("/api/projects/p1/state").json()["score"]["tracks"][0]["notes"][0]["pitch_midi"] == 62

    # undo 无历史 → 400
    c.post("/api/projects/p1/undo")
    c.post("/api/projects/p1/undo")
    r = c.post("/api/projects/p1/undo")
    assert r.status_code == 400

    # redo 回 62 → 收藏（git 点）→ 第二轮 +3 至 65 → 再收藏 → rollback HEAD~1 回到 62
    assert c.post("/api/projects/p1/redo").json()["ok"] is True
    assert c.get("/api/projects/p1/state").json()["score"]["tracks"][0]["notes"][0]["pitch_midi"] == 62
    assert c.post("/api/projects/p1/favorite").json()["ok"] is True
    c.post("/api/projects/p1/batch", json={
        "label": "+3", "commands": [{"op": "transpose", "track": 0, "index": None, "value": 3}],
    })
    assert c.get("/api/projects/p1/state").json()["score"]["tracks"][0]["notes"][0]["pitch_midi"] == 65
    assert c.post("/api/projects/p1/favorite").json()["ok"] is True
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
    import tsov.webapp.config as cfg
    monkeypatch.setattr(cfg, "SSE_HEARTBEAT_SEC", 0.1)

    _make_project(env)
    c = env["client"]

    r = c.get("/api/projects/p1/events?close_after=2")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert ": connected" in r.text and ": ping" in r.text


def test_sse_stream_events(env, monkeypatch):
    """并发写（batch）→ SSE 推 diff_applied + state_updated（跨线程 publish → asyncio 队列）。"""
    import tsov.webapp.config as cfg
    monkeypatch.setattr(cfg, "SSE_HEARTBEAT_SEC", 0.1)

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

    _install_fake_stream(monkeypatch, proj_root)

    r = c.post("/api/chat", json={"project": name, "message": "把第一个音改成 D5"})
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] and body["session_path"].endswith(".jsonl")
    # agent 线程异步起跑：等会话 JSONL 落第一行（带 deadline，防竞态）
    deadline = time.time() + 10
    while time.time() < deadline and not Path(body["session_path"]).is_file():
        time.sleep(0.05)
    assert Path(body["session_path"]).is_file()

    # 等 agent 线程收尾（采用结果落盘；M-V7 D2：零 commit → 以 state 变化为信号）
    deadline = time.time() + 10
    while time.time() < deadline:
        st_ = c.get(f"/api/projects/{name}/state").json()
        if st_["score"]["tracks"][0]["notes"][0]["pitch_midi"] == 74:
            break
        time.sleep(0.1)

    state = c.get(f"/api/projects/{name}/state").json()
    assert state["score"]["tracks"][0]["notes"][0]["pitch_midi"] == 74
    assert state["history"]["can_undo"] is True
    assert not any("agent：" in ln for ln in state["git_log"])       # 编辑零 commit
    win = c.get(f"/api/projects/{name}/window").json()              # 快照窗口有 agent 条目
    assert win["entries"] and any(e["source"] == "agent" for e in win["entries"])

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


# ---------------------------------------------------------------------------
# M-V2.1：多轮续接 / 停止 / 工程改名 / 会话导入
# ---------------------------------------------------------------------------


def _install_fake_stream(monkeypatch, proj_root, seen_messages: list[int] | None = None):
    """把 web._stream_chat 换成假实现（工具调用→回答；可记录每轮消息数）。

    奇数调 = edit_score（annotations 纯程序路径，不触网）；偶数调 = 最终回答。
    """
    import tsov.webapp.agent_session as web_mod

    calls = {"n": 0}

    def fake_stream(bus, project, session_id, messages, tools, stop_event):
        calls["n"] += 1
        if seen_messages is not None:
            seen_messages.append(len(messages))
        if calls["n"] % 2 == 0:
            return {"content": "完成。", "tool_calls": [], "message": {"role": "assistant", "content": "done"}}
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

    monkeypatch.setattr(web_mod, "_stream_chat", fake_stream)
    return calls


def _wait_agent_done(env, name, timeout=10.0):
    """等 agent 线程落 lock（轮询 chat 会话锁）。"""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not env["app"].state.tsov.agent_lock.locked():
            return True
        time.sleep(0.05)
    return False


def test_chat_multiturn_same_session(env, monkeypatch):
    """同工程连续两次 /api/chat 续接同一 JSONL（session_id 不变，上下文增长）。"""
    import time as _t

    name = _make_project(env)
    proj_root = env["dir"] / name
    c = env["client"]
    seen: list[int] = []
    _install_fake_stream(monkeypatch, proj_root, seen_messages=seen)

    r1 = c.post("/api/chat", json={"project": name, "message": "第一轮：改第一个音"}).json()
    assert _wait_agent_done(env, name)
    r2 = c.post("/api/chat", json={"project": name, "message": "第二轮：继续"}).json()
    assert r2["session_id"] == r1["session_id"], "多轮应续接同一会话"
    assert _wait_agent_done(env, name)

    import json as _json
    lines = (env["dir"] / "agent-sessions" / (r1["session_id"] + ".jsonl")).read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 6, "两次会话应追加到同一 JSONL"

    # 上下文增长：第二轮首调看到的消息数 > 第一轮首调
    assert seen[2] > seen[0], f"第二轮上下文应更长：{seen}"

    # 新会话后 id 变化
    assert c.post("/api/chat/reset", json={"project": name}).json()["ok"] is True
    r3 = c.post("/api/chat", json={"project": name, "message": "第三轮"}).json()
    assert r3["session_id"] != r1["session_id"]
    assert _wait_agent_done(env, name)


def test_chat_stop_endpoint(env):
    name = _make_project(env)
    state = env["app"].state.tsov
    assert state.agent_lock.acquire(blocking=False) is True
    try:
        r = env["client"].post("/api/chat/stop").json()
        assert r["stopping"] is True and r["ok"]
        assert state.agent_stop.is_set()
        state.agent_stop.clear()
    finally:
        state.agent_lock.release()
    r = env["client"].post("/api/chat/stop").json()
    assert r["stopping"] is False


def test_project_title_rename(env):
    name = _make_project(env)
    c = env["client"]
    r = c.post(f"/api/projects/{name}/title", json={"title": "新的标题"})
    assert r.status_code == 200 and r.json()["ok"]
    s = c.get(f"/api/projects/{name}/state").json()
    assert s["score"]["title"] == "新的标题"
    assert not any("改名" in ln for ln in s["git_log"])               # M-V7 D2：零 commit
    win = c.get(f"/api/projects/{name}/window").json()                # 改为快照窗口条目
    assert any("改名" in e["label"] for e in win["entries"])
    # undo 可回标题
    assert c.post(f"/api/projects/{name}/undo").json()["ok"] is True
    assert c.get(f"/api/projects/{name}/state").json()["score"]["title"] != "新的标题"


def test_sessions_list_and_load(env, monkeypatch):
    name = _make_project(env)
    proj_root = env["dir"] / name
    c = env["client"]

    _install_fake_stream(monkeypatch, proj_root)
    c.post("/api/chat", json={"project": name, "message": "说点什么"})
    assert _wait_agent_done(env, name)

    r = c.get("/api/sessions").json()
    assert r["sessions"] and r["sessions"][0]["name"].endswith(".jsonl")
    fname = r["sessions"][0]["name"]

    r = c.post("/api/sessions/load", json={"project": name, "name": fname}).json()
    assert r["ok"] and r["messages"], "导入应回放消息"
    # 导出的会话成为当前续接对象：下次 chat 沿用其 session_id
    r2 = c.post("/api/chat", json={"project": name, "message": "续一段"}).json()
    assert r2["session_id"] == r["session_id"]
    assert _wait_agent_done(env, name)

    # 防穿越：非法名 404
    r = c.post("/api/sessions/load", json={"project": name, "name": "../../.env"})
    assert r.status_code == 404 or r.status_code == 400


# ---------------------------------------------------------------------------
# M-V2.2 议题 ④：render?rev / wav?rev（A/B 对比试听）/ chat base_rev
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_SF, reason="缺 vendor/soundfonts/FluidR3_GM.sf2")
def test_render_old_rev_and_wav_rev(env):
    """render 带 rev：git show 旧版按 stem 库拼装到 .mix-cache（ADR-0018），不动 HEAD；wav?rev 可取流。"""
    name = _make_project(env)
    c = env["client"]
    # 先做一轮编辑，产生 HEAD~1 旧版
    proj_root = env["dir"] / name
    r = c.post(f"/api/projects/{name}/batch", json={"label": "+2", "commands": [{"op": "transpose", "track": 0, "index": None, "value": 2}]})
    assert r.status_code == 200
    rf = c.post(f"/api/projects/{name}/favorite")          # M-V7 D2：编辑零 commit，收藏产生新版本
    assert rf.status_code == 200 and rf.json()["ok"]
    log = c.get(f"/api/projects/{name}/log").json()["log"]
    head = log[0].split(" ")[0]

    # 旧版渲染（head~1 = 初始 [60]）
    r2 = c.post(f"/api/projects/{name}/render", json={"rev": head + "~1"})
    assert r2.status_code == 200
    body = r2.json()
    assert body["rev"] == (head + "~1")[:8]
    assert body["rendered"] and body["cached"] == []          # 首次拼装：全轨现渲
    assert (proj_root / ".mix-cache" / ((head + "~1")[:8] + ".wav")).is_file()
    # 坏版本 400
    r3 = c.post(f"/api/projects/{name}/render", json={"rev": "NO_SUCH_REV"})
    assert r3.status_code == 400
    # wav?rev 取流
    r4 = c.get(f"/api/projects/{name}/wav", params={"rev": head + "~1"})
    assert r4.status_code == 200 and r4.content[:4] == b"RIFF"
    # HEAD 未被改动
    state = c.get(f"/api/projects/{name}/state").json()
    assert state["score"]["tracks"][0]["notes"][0]["pitch_midi"] == 62


@pytest.mark.skipif(not _HAS_SF, reason="缺 vendor/soundfonts/FluidR3_GM.sf2")
def test_stem_cache_hit_and_gc(env):
    """M-V7 D1（ADR-0018）：render 走 stem 库（首渲全量 → 全命中）；推子不失效；GC 保留引用并清退役目录。"""
    name = _make_project(env)
    c = env["client"]
    proj_root = env["dir"] / name

    r1 = c.post(f"/api/projects/{name}/render", json={})
    assert r1.status_code == 200
    b1 = r1.json()
    assert b1["rendered"] == ["melody"] and b1["cached"] == []
    assert (proj_root / ".stem-cache").is_dir()

    r2 = c.post(f"/api/projects/{name}/render", json={})
    b2 = r2.json()
    assert b2["rendered"] == [] and b2["cached"] == ["melody"]

    # 推子改动（set_track_mix）→ 不触发重渲（ADR-0018 刀口）
    rb = c.post(f"/api/projects/{name}/batch",
                json={"label": "vol", "commands": [{"op": "set_track_mix", "track": 0, "value": {"volume": 0.5}}]})
    assert rb.status_code == 200
    r3 = c.post(f"/api/projects/{name}/render", json={})
    b3 = r3.json()
    assert b3["rendered"] == [] and b3["cached"] == ["melody"]

    # GC：HEAD 引用者保留；退役 .render-cache 被清理
    (proj_root / ".render-cache").mkdir(exist_ok=True)
    g = c.post(f"/api/projects/{name}/cache/gc").json()
    assert g["removed"] == [] and g["keep"] >= 1
    assert ".render-cache" in g["legacy_removed"] and not (proj_root / ".render-cache").exists()


# ---------------------------------------------------------------------------
# M-V7 D2：留存双账本（快照窗口 / 自动收藏 / 设置）
# ---------------------------------------------------------------------------


def test_retention_autofavorite_threshold_and_reset(env):
    """M-V7 D2（ADR-0019）：迭代计数 → 阈值自动收藏（fav/<ts>-auto）+ 清零；未达阈值不收藏、无改动不空收。"""
    from tsov.host.state import ProjectState
    from tsov.web import _retention_tick

    name = _make_project(env)
    proj_root = env["dir"] / name
    c = env["client"]
    ws_state = env["app"].state.tsov          # 与接口同一 WebState（钩子同源）

    # 轮末累计 2 轮 → 只计数（默认阈值 40）
    _retention_tick(ws_state, name, agent_turns=2)
    assert ProjectState(proj_root).counter == 2

    # 阈值降为 3（工程档）→ 再来 1 轮（3 ≥ 3）但无改动 → 不空收、计数保留
    ps = ProjectState(proj_root)
    ps.data["settings"] = {"auto_favorite_iters": 3}
    ps.save()
    _retention_tick(ws_state, name, agent_turns=1)
    assert ProjectState(proj_root).counter == 3
    assert not any("自动收藏" in ln for ln in c.get(f"/api/projects/{name}/log").json()["log"])

    # 产生改动（/batch 自带留存检查）→ 自动收藏 + 清零 + tag -auto
    r = c.post(f"/api/projects/{name}/batch", json={
        "label": "改", "commands": [{"op": "transpose", "track": 0, "index": None, "value": 1}]})
    assert r.status_code == 200
    log = c.get(f"/api/projects/{name}/log").json()["log"]
    assert any("自动收藏" in ln for ln in log)
    favs = c.get(f"/api/projects/{name}/favorites").json()["favorites"]
    assert any(f["tag"].endswith("-auto") for f in favs)
    assert ProjectState(proj_root).counter == 0


def test_window_and_settings_endpoints(env, monkeypatch):
    """M-V7 D2：/window（条目+游标）与 /settings（工程档读写 + 合成视图）。"""
    monkeypatch.setattr("tsov.host.state.global_settings_path",
                        lambda: env["dir"] / "no-such-global.json")   # 隔离真实全局档

    name = _make_project(env)
    c = env["client"]

    c.post(f"/api/projects/{name}/batch", json={"label": "+1", "commands": [{"op": "transpose", "track": 0, "index": None, "value": 1}]})
    c.post(f"/api/projects/{name}/batch", json={"label": "+2", "commands": [{"op": "transpose", "track": 0, "index": None, "value": 1}]})
    win = c.get(f"/api/projects/{name}/window").json()
    assert [e["label"] for e in win["entries"]] == ["+1", "+2"]
    assert win["cursor"] == 2 and win["can_undo"] and not win["can_redo"]
    assert win["windows"] == {"user": 20, "agent_rounds": 5}
    assert all(e["source"] == "user" for e in win["entries"])

    # settings：默认 40 / 定时关；写工程档 → 合成视图立即反映；深合并不打掉兄弟字段
    s0 = c.get(f"/api/projects/{name}/settings").json()
    assert s0["settings"]["auto_favorite_iters"] == 40
    assert s0["settings"]["timed_favorite"]["enabled"] is False
    r = c.post(f"/api/projects/{name}/settings", json={"auto_favorite_iters": 7})
    assert r.status_code == 200 and r.json()["settings"]["auto_favorite_iters"] == 7
    c.post(f"/api/projects/{name}/settings", json={"timed_favorite": {"interval_min": 5}})
    s1 = c.get(f"/api/projects/{name}/settings").json()["settings"]
    assert s1["timed_favorite"]["interval_min"] == 5 and s1["timed_favorite"]["enabled"] is False
    assert (env["dir"] / name / ".tsov-state.json").is_file()


def test_settings_global_file(env, monkeypatch):
    """M-V7 D2：全局档（跨工程）写入 + 合成优先级（工程档 > 全局档 > 默认）。"""
    gpath = env["dir"] / "gsettings.json"
    monkeypatch.setattr("tsov.host.state.global_settings_path", lambda: gpath)

    name = _make_project(env)
    c = env["client"]
    r = c.post("/api/settings", json={"auto_favorite_iters": 21})
    assert r.status_code == 200
    assert gpath.is_file()                                     # 全局档已落盘
    assert c.get(f"/api/projects/{name}/settings").json()["settings"]["auto_favorite_iters"] == 21
    # 工程档覆盖全局档
    c.post(f"/api/projects/{name}/settings", json={"auto_favorite_iters": 5})
    assert c.get(f"/api/projects/{name}/settings").json()["settings"]["auto_favorite_iters"] == 5


def test_window_jump_and_favorite_delete(env):
    """M-V7 D3：窗口跳转端点（零 commit 恢复快照点）+ 收藏删除端点。"""
    name = _make_project(env)
    c = env["client"]
    log0 = c.get(f"/api/projects/{name}/log").json()["log"]

    c.post(f"/api/projects/{name}/batch", json={"label": "+1", "commands": [{"op": "transpose", "track": 0, "index": None, "value": 1}]})
    c.post(f"/api/projects/{name}/batch", json={"label": "+2", "commands": [{"op": "transpose", "track": 0, "index": None, "value": 1}]})
    assert c.get(f"/api/projects/{name}/state").json()["score"]["tracks"][0]["notes"][0]["pitch_midi"] == 62

    # 跳到 #0（窗口起点）→ 60；跳回 #2 → 62；全程零 commit
    r = c.post(f"/api/projects/{name}/window/jump", json={"cursor": 0})
    assert r.status_code == 200 and r.json()["cursor"] == 0
    assert c.get(f"/api/projects/{name}/state").json()["score"]["tracks"][0]["notes"][0]["pitch_midi"] == 60
    r = c.post(f"/api/projects/{name}/window/jump", json={"cursor": 2})
    assert r.status_code == 200 and r.json()["can_undo"] is True
    assert c.get(f"/api/projects/{name}/state").json()["score"]["tracks"][0]["notes"][0]["pitch_midi"] == 62
    assert c.get(f"/api/projects/{name}/log").json()["log"] == log0     # 零 commit
    # 空工程/越界行为：越界→钳制，故改用不存在工程路径验证 404 不需；这里验证坏工程名 404
    assert c.post("/api/projects/no_such/window/jump", json={"cursor": 0}).status_code == 404

    # 收藏删除：建 → 列出 → 删 → 空；坏 tag → 400
    assert c.post(f"/api/projects/{name}/favorite").json()["ok"] is True
    favs = c.get(f"/api/projects/{name}/favorites").json()["favorites"]
    assert len(favs) == 1
    tag = favs[0]["tag"]
    assert c.request("DELETE", f"/api/projects/{name}/favorites", params={"tag": tag}).json()["ok"] is True
    assert c.get(f"/api/projects/{name}/favorites").json()["favorites"] == []
    assert c.request("DELETE", f"/api/projects/{name}/favorites", params={"tag": "not-a-fav"}).status_code == 400


def test_chat_accepts_base_rev(env, monkeypatch):
    """chat 带 base_rev：路由接受并注入 fake stream 可见（不阻塞，只验证不 422）。"""
    name = _make_project(env)
    proj_root = env["dir"] / name
    _install_fake_stream(monkeypatch, proj_root)
    r = env["client"].post("/api/chat", json={"project": name, "message": "改一下", "base_rev": "HEAD"})
    assert r.status_code == 200
    assert _wait_agent_done(env, name)


def test_create_project_default_track(env):
    """审计回归 M-V2.3：新建空工程必须带一条空旋律轨（零轨工程会让空谱创作链路断掉）。"""
    r = env["client"].post("/api/projects", json={"name": "empty1"})
    assert r.status_code == 200
    s = env["client"].get("/api/projects/empty1/state").json()
    assert len(s["score"]["tracks"]) == 1
    assert s["score"]["tracks"][0]["notes"] == []


# ---------------------------------------------------------------------------
# M-V3 交互闭环：chat 带 annotations（确定性先行）/ selection 上下文
# ---------------------------------------------------------------------------


def _install_final_only_stream(monkeypatch, seen: dict):
    """假 stream：直接返回最终回答（不调工具）；记录首轮 messages 供 brief 断言。"""
    import tsov.webapp.agent_session as web_mod

    def fake_stream(bus, project, session_id, messages, tools, stop_event):
        if "messages" not in seen:
            seen["messages"] = messages
        return {"content": "好的，已收到。", "tool_calls": [], "message": {"role": "assistant", "content": "done"}}

    monkeypatch.setattr(web_mod, "_stream_chat", fake_stream)


def test_chat_annotations_applied_before_agent(env, monkeypatch):
    """M-V3 + M-V7 D2：chat 带 annotations → 确定性先行应用（快照窗口条目，零 commit）+ brief 注入（不得回退）+ 选区上下文。"""
    name = _make_project(env)
    seen: dict = {}
    _install_final_only_stream(monkeypatch, seen)

    r = env["client"].post("/api/chat", json={
        "project": name, "message": "第一音改成 D5，其余保持",
        "annotations": [{"index": 0, "action": "pitch", "value": 74}],
        "selection": {"track": 0, "indices": [0, 1]},
    })
    assert r.status_code == 200
    assert _wait_agent_done(env, name)

    # 标注已确定性应用：score 更新 + 快照窗口条目（M-V7 D2：零 commit）
    state = env["client"].get(f"/api/projects/{name}/state").json()
    assert state["score"]["tracks"][0]["notes"][0]["pitch_midi"] == 74
    log = env["client"].get(f"/api/projects/{name}/log").json()["log"]
    assert not any("人工标注" in ln for ln in log), log
    win = env["client"].get(f"/api/projects/{name}/window").json()
    assert any("人工标注" in e["label"] for e in win["entries"])
    # brief 注入：标注已应用（不得回退）+ 选区上下文
    assert "messages" in seen, "agent 会话应已启动"
    brief = "\n".join(str(m.get("content", "")) for m in seen["messages"] if m.get("role") == "user")
    assert "人工标注已确定性应用" in brief and "不得回退" in brief
    assert "用户当前选区" in brief and "[0、1]" in brief


def test_chat_annotations_invalid_rejected(env, monkeypatch):
    """M-V3：非法标注（越界）→ 拒绝整条消息：谱不变、无 commit、无会话 JSONL、锁释放。"""
    name = _make_project(env)
    seen: dict = {}
    _install_final_only_stream(monkeypatch, seen)

    r = env["client"].post("/api/chat", json={
        "project": name, "message": "改个不存在的音",
        "annotations": [{"index": 9, "action": "pitch", "value": 70}],
    }).json()
    sid = r["session_id"]
    assert _wait_agent_done(env, name)

    # 拒绝发生在会话创建前：无 JSONL、无 commit、谱不变、未进 LLM
    state = env["client"].get(f"/api/projects/{name}/state").json()
    assert state["score"]["tracks"][0]["notes"][0]["pitch_midi"] == 60
    log = env["client"].get(f"/api/projects/{name}/log").json()["log"]
    assert not any("人工标注" in ln for ln in log)
    assert not (env["dir"] / "agent-sessions" / f"{sid}.jsonl").exists()
    assert "messages" not in seen, "拒绝路径不应进入 LLM"


def test_chat_annotations_clear_all_guard(env, monkeypatch):
    """M-V3 × B2#2 守卫：标注 delete-all → 拒绝整条消息（谱不变、无 commit、无 JSONL）。"""
    name = _make_project(env)
    seen: dict = {}
    _install_final_only_stream(monkeypatch, seen)

    r = env["client"].post("/api/chat", json={
        "project": name, "message": "清空",
        "annotations": [{"index": 2, "action": "delete"}, {"index": 1, "action": "delete"}, {"index": 0, "action": "delete"}],
    }).json()
    assert _wait_agent_done(env, name)
    state = env["client"].get(f"/api/projects/{name}/state").json()
    assert len(state["score"]["tracks"][0]["notes"]) == 3
    log = env["client"].get(f"/api/projects/{name}/log").json()["log"]
    assert not any("人工标注" in ln for ln in log)
    assert not (env["dir"] / "agent-sessions" / f"{r['session_id']}.jsonl").exists()


# ---------------------------------------------------------------------------
# M-V6 批1：工程列表带信息 + 导入 score json 通道
# ---------------------------------------------------------------------------


def test_projects_list_info_shape(env):
    """列表带信息：name/title/tracks/notes/mtime（前端「标题 · N 轨 · M 音」用）。"""
    _make_project(env, name="p1")
    body = env["client"].get("/api/projects").json()
    assert len(body["projects"]) == 1
    p = body["projects"][0]
    assert p["name"] == "p1"
    assert p["title"] == "webtest"      # _score_payload 的 title
    assert p["tracks"] == 1 and p["notes"] == 3
    assert p["mtime"] > 0


def test_projects_list_order_by_mtime(env):
    """最近修改的工程在前（前端启动打开列表第一个 = 最近工程，不再无脑第一个）。"""
    _make_project(env, name="aaa")
    _make_project(env, name="bbb")
    old = time.time() - 3600
    os.utime(env["dir"] / "aaa" / "score.json", (old, old))   # aaa 改成一小时前
    names = [p["name"] for p in env["client"].get("/api/projects").json()["projects"]]
    assert names == ["bbb", "aaa"]


def test_import_project_from_script_output(env):
    """脚本直出产物（score_task2.json 形态）→ 导入成工程 + 基线 commit；原文件保留。"""
    c = env["client"]
    src_dir = env["dir"] / "task2"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "score_task2.json").write_text(json.dumps(_score_payload()), encoding="utf-8")

    # 候选列表：只列能解析成 Score 的 json，带建议名与统计
    cands = c.get("/api/import-candidates").json()["candidates"]
    assert [x["path"] for x in cands] == ["task2/score_task2.json"]
    assert cands[0]["name_hint"] == "task2" and cands[0]["notes"] == 3

    # 导入（缺省名 = 文件名推导 task2）
    r = c.post("/api/projects/import", json={"source": "task2/score_task2.json"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "name": "task2", "source": "task2/score_task2.json"}

    # 工程出现且可打开；基线 commit 存在；原文件未动（复制而非移动）
    names = [p["name"] for p in c.get("/api/projects").json()["projects"]]
    assert "task2" in names
    st = c.get("/api/projects/task2/state").json()
    assert len(st["score"]["tracks"][0]["notes"]) == 3
    assert any("init" in ln for ln in st["git_log"])
    assert (src_dir / "score_task2.json").is_file()


def test_import_explicit_name_and_duplicate(env):
    c = env["client"]
    d = env["dir"] / "proj"
    d.mkdir(parents=True, exist_ok=True)
    (d / "song.json").write_text(json.dumps(_score_payload()), encoding="utf-8")
    r = c.post("/api/projects/import", json={"source": "proj/song.json", "name": "我的曲子"})
    assert r.status_code == 200 and r.json()["name"] == "我的曲子"
    # 重名 → 400（提示换名）
    r = c.post("/api/projects/import", json={"source": "proj/song.json", "name": "我的曲子"})
    assert r.status_code == 400 and "已存在" in r.json()["error"]


def test_import_rejects_bad_sources(env):
    c = env["client"]
    # 路径穿越 → 400
    r = c.post("/api/projects/import", json={"source": "../evil.json"})
    assert r.status_code == 400
    # 不存在 → 404
    r = c.post("/api/projects/import", json={"source": "nope/none.json"})
    assert r.status_code == 404
    # 非 json 后缀 → 400
    (env["dir"] / "x.txt").write_text("hi", encoding="utf-8")
    assert c.post("/api/projects/import", json={"source": "x.txt"}).status_code == 400
    # 坏 JSON → 400
    (env["dir"] / "bad.json").write_text("{not json", encoding="utf-8")
    assert c.post("/api/projects/import", json={"source": "bad.json"}).status_code == 400
    # 非 Score 结构 → 400
    (env["dir"] / "other.json").write_text(json.dumps({"hello": 1}), encoding="utf-8")
    assert c.post("/api/projects/import", json={"source": "other.json"}).status_code == 400
    # 全部被拒后：无新工程
    assert c.get("/api/projects").json()["projects"] == []


def test_import_candidates_skips_projects_and_junk(env):
    """候选扫描：跳过工程自带 score.json、非 Score json、隐藏目录。"""
    c = env["client"]
    _make_project(env, name="p1")                                   # 工程自带 score.json → 不出现在候选
    (env["dir"] / "stage-01.json").write_text(json.dumps({"notes": []}), encoding="utf-8")   # 非 Score → 跳过
    (env["dir"] / "raw.json").write_text(json.dumps(_score_payload()), encoding="utf-8")     # 顶层 json → 候选
    (env["dir"] / "p1" / "agent-edited.json").write_text(json.dumps(_score_payload()), encoding="utf-8")  # 工程内部文件 → 跳过
    cands = c.get("/api/import-candidates").json()["candidates"]
    assert [x["path"] for x in cands] == ["raw.json"]


def test_favorite_and_list(env):
    """修正轮2：收藏当前版本（git tag）+ 收藏列表。"""
    c = env["client"]
    c.post("/api/projects", json={"name": "favproj"})
    r = c.post("/api/projects/favproj/favorite")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["tag"].startswith("fav/")
    lst = c.get("/api/projects/favproj/favorites").json()["favorites"]
    assert any(f["tag"] == body["tag"] for f in lst)


@pytest.mark.skipif(not _HAS_SF, reason="缺 vendor/soundfonts/FluidR3_GM.sf2")
def test_export_matrix_endpoint(env):
    """修正轮2：导出端点（mix + MIDI 真链路；stems 变体由 host 单测覆盖）。"""
    c = env["client"]
    c.post("/api/projects", json={"name": "expproj"})
    r = c.post("/api/projects/expproj/export",
               json={"mix": True, "stems": False, "buses": False, "midi": True, "midi_stems": False})
    assert r.status_code == 200, r.text
    body = r.json()
    files = body["files"]
    assert any(f.endswith("mix.wav") for f in files)
    assert any(f.endswith(".mid") for f in files)
    out = Path(body["out_dir"])
    assert out.is_dir() and all(Path(f).is_file() for f in files)


@pytest.mark.skipif(not _HAS_SF, reason="缺 vendor/soundfonts/FluidR3_GM.sf2")
def test_export_bit_depth_and_range_endpoint(env):
    """E6 段2：导出位深（24-bit 实测文件头）+ 选段（循环区间 [起, 止]）；非法参数 400。"""
    import soundfile as sf

    from tsov.core.score import Instrument, Note, Score, Track

    c = env["client"]
    score = Score(
        title="exp2", tempo=120.0, key_candidates=[],
        tracks=[Track(name="melody", instrument=Instrument(program="piano"),
                      notes=[Note(start=0.0, end=2.0, pitch_midi=60, pitch_hz=261.6, velocity=0.8)])],
    ).to_dict()
    c.post("/api/projects", json={"name": "expproj2", "score": score})
    r = c.post("/api/projects/expproj2/export",
               json={"mix": True, "stems": False, "buses": False, "midi": False, "midi_stems": False,
                     "bit_depth": "24", "range": [0.2, 1.2]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bit_depth"] == "PCM_24" and body["range"] == [0.2, 1.2]
    info = sf.info(body["mix"])
    assert info.subtype == "PCM_24" and abs(info.duration - 1.0) < 0.01
    # 非法：位深 / 选段 → 400 {"error": ...}
    r = c.post("/api/projects/expproj2/export", json={"bit_depth": "12"})
    assert r.status_code == 400 and "error" in r.json()
    r = c.post("/api/projects/expproj2/export", json={"range": [1.5, 1.0]})
    assert r.status_code == 400 and "error" in r.json()


def test_delete_project_safe_trash(env):
    """E6 段3：删除工程=安全删（移入 .trash）+ 列表消失 + 打开中防呆 + 找回 + 候选跳过 .trash。"""
    c = env["client"]
    d = env["dir"]
    c.post("/api/projects", json={"name": "delproj"})
    # 打开中（进注册表）并做一次编辑
    r = c.post("/api/projects/delproj/batch",
               json={"commands": [{"op": "set_tempo", "track": 0, "value": {"tempo": 123.0}}]})
    assert r.json()["applied"] == 1

    r = c.delete("/api/projects/delproj")
    assert r.status_code == 200, r.text
    dest = Path(r.json()["trashed_to"])
    assert dest.is_dir() and (dest / "score.json").is_file()
    assert dest.parent == (d / ".trash")

    # 列表消失 + state 404
    names = [p["name"] for p in c.get("/api/projects").json()["projects"]]
    assert "delproj" not in names
    assert c.get("/api/projects/delproj/state").status_code == 404
    # 打开中防呆：删除后任何写动作 404（不会把目录重建回来）
    assert c.post("/api/projects/delproj/batch", json={"commands": []}).status_code == 404
    assert not (d / "delproj").exists()
    # 二次删除 404
    assert c.delete("/api/projects/delproj").status_code == 404

    # 导入候选跳过 .trash（放一个二级 json 引它上钩）
    (d / ".trash" / "junk.json").write_text('{"title": "x"}', encoding="utf-8")
    cands = c.get("/api/import-candidates").json()["candidates"]
    assert all(".trash" not in str(cand.get("path") or "") for cand in cands)

    # 找回演练：整目录移回 → 列表再现（手动找回路径）
    import shutil as _sh

    _sh.move(str(dest), str(d / "delproj"))
    names = [p["name"] for p in c.get("/api/projects").json()["projects"]]
    assert "delproj" in names
    st = c.get("/api/projects/delproj/state").json()
    assert st["score"]["tempo"] == 123.0   # 内容原样（编辑未丢）


def test_agent_actions_list_and_undo(env):
    """批B B1-2 + M-V7 D2：动作日志列表 + 动作级撤销（窗口回跳，零 commit；后续置灰）。"""
    from tsov.host.journal import ActionJournal

    c = env["client"]
    c.post("/api/projects", json={"name": "actproj"})
    d = env["dir"] / "actproj"
    log0 = c.get("/api/projects/actproj/log").json()["log"]

    # 模拟一次 agent 写动作：pre = 当前谱，post = 音量 0.65 的谱
    before = json.loads((d / "score.json").read_text(encoding="utf-8"))
    after = json.loads(json.dumps(before))
    after["tracks"][0]["instrument"]["volume"] = 0.65
    j = ActionJournal(d)
    h1, h2 = j.snapshot(before), j.snapshot(after)
    j.append(source="agent", label="调音量/声像", session_id="s1", turn=1, round="s1:1",
             tool="set_track_mix", args="轨 melody · 音量 0.65",
             pre=h1, post=h2, impact={"text": "melody：音量 0.80→0.65"})
    # 工程当前状态 = 动作后（走命令层，模拟"已采用"；这条 /batch 也进窗口）
    r = c.post("/api/projects/actproj/batch", json={
        "label": "sim", "commands": [{"op": "set_track_mix", "track": 0, "value": {"volume": 0.65}}],
    })
    assert r.status_code == 200

    lst = c.get("/api/projects/actproj/agent-actions").json()["entries"]
    agent_rows = [e for e in lst if e["source"] == "agent"]
    assert len(agent_rows) == 1 and agent_rows[0]["seq"] == 1 and agent_rows[0]["tool"] == "set_track_mix"

    # 撤销 → 窗口回跳恢复 pre 快照（音量回初始）；零 commit
    r2 = c.post("/api/projects/actproj/agent-actions/1/undo")
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["ok"] is True and body["commit"] is None
    init_vol = float(before["tracks"][0]["instrument"]["volume"])
    st = c.get("/api/projects/actproj/state").json()
    assert abs(float(st["score"]["tracks"][0]["instrument"]["volume"]) - init_vol) < 1e-9
    # 日志里已标记失效
    lst2 = c.get("/api/projects/actproj/agent-actions").json()["entries"]
    assert lst2[0]["stale"] is True and lst2[0]["undone"] is True
    # git log 不因编辑/撤销增长（M-V7 D2：零 commit）
    assert c.get("/api/projects/actproj/log").json()["log"] == log0
    # 不存在的动作 → 404
    assert c.post("/api/projects/actproj/agent-actions/99/undo").status_code == 404


def test_batch_bookmark_folder_and_state(env):
    """M-V8 E1：书签 / 文件夹归属走 /batch（命令层）→ /state 读回（UI 与 agent 同一动作路径）。"""
    name = _make_project(env)
    c = env["client"]
    r = c.post(f"/api/projects/{name}/batch", json={"label": "书签", "commands": [
        {"op": "set_track_folder", "track": 0, "value": {"folder": "band"}},
        {"op": "add_bookmark", "value": {"scope": "project", "kind": "section", "start": 0.0, "end": 0.6, "label": "段1"}},
        {"op": "add_bookmark", "value": {"scope": "folder", "ref": "band", "kind": "mark", "start": 0.3, "label": "入点"}},
    ]})
    assert r.status_code == 200 and r.json()["applied"] == 3
    sc = c.get(f"/api/projects/{name}/state").json()["score"]
    assert sc["tracks"][0]["folder"] == "band"
    assert [b["label"] for b in sc["bookmarks"]] == ["段1", "入点"]

    r2 = c.post(f"/api/projects/{name}/batch", json={"label": "改名/删除", "commands": [
        {"op": "set_bookmark", "index": 0, "value": {"label": "前奏"}},
        {"op": "remove_bookmark", "index": 1},
    ]})
    assert r2.json()["applied"] == 2
    sc2 = c.get(f"/api/projects/{name}/state").json()["score"]
    assert [b["label"] for b in sc2["bookmarks"]] == ["前奏"]

    r3 = c.post(f"/api/projects/{name}/batch", json={"label": "bad", "commands": [
        {"op": "add_bookmark", "value": {"scope": "folder", "ref": "nope", "kind": "mark", "start": 1.0}},
    ]})
    assert r3.json()["applied"] == 0 and r3.json()["errors"]


def test_play_stop_idle(env):
    """M-V8 E1：/play/stop 空闲态安全返回（无播放 → 置空信号 + sd.stop 兜底）。"""
    name = _make_project(env)
    r = env["client"].post(f"/api/projects/{name}/play/stop")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert env["client"].post("/api/projects/no_such/play/stop").status_code == 404
