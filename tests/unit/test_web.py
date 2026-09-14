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


# ---------------------------------------------------------------------------
# M-V2.1：多轮续接 / 停止 / 工程改名 / 会话导入
# ---------------------------------------------------------------------------


def _install_fake_stream(monkeypatch, proj_root, seen_messages: list[int] | None = None):
    """把 web._stream_chat 换成假实现（工具调用→回答；可记录每轮消息数）。

    奇数调 = edit_score（annotations 纯程序路径，不触网）；偶数调 = 最终回答。
    """
    import tsov.web as web_mod

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
    lines = (proj_root.parent.parent / "agent-sessions" / (r1["session_id"] + ".jsonl")).read_text(encoding="utf-8").splitlines()
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
    assert any("改名" in ln for ln in s["git_log"])
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
    """render 带 rev：git show 旧版渲染到 .render-cache，不动 HEAD；wav?rev 可取流。"""
    name = _make_project(env)
    c = env["client"]
    # 先做一轮编辑，产生 HEAD~1 旧版
    proj_root = env["dir"] / name
    r = c.post(f"/api/projects/{name}/batch", json={"label": "+2", "commands": [{"op": "transpose", "track": 0, "index": None, "value": 2}]})
    assert r.status_code == 200
    log = c.get(f"/api/projects/{name}/log").json()["log"]
    head = log[0].split(" ")[0]

    # 旧版渲染（head~1 = 初始 [60]）
    r2 = c.post(f"/api/projects/{name}/render", json={"rev": head + "~1"})
    assert r2.status_code == 200
    body = r2.json()
    assert body["rev"] == (head + "~1")[:8]
    assert (proj_root / ".render-cache" / ((head + "~1")[:8] + ".wav")).is_file()
    # 坏版本 400
    r3 = c.post(f"/api/projects/{name}/render", json={"rev": "NO_SUCH_REV"})
    assert r3.status_code == 400
    # wav?rev 取流
    r4 = c.get(f"/api/projects/{name}/wav", params={"rev": head + "~1"})
    assert r4.status_code == 200 and r4.content[:4] == b"RIFF"
    # HEAD 未被改动
    state = c.get(f"/api/projects/{name}/state").json()
    assert state["score"]["tracks"][0]["notes"][0]["pitch_midi"] == 62


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
    import tsov.web as web_mod

    def fake_stream(bus, project, session_id, messages, tools, stop_event):
        if "messages" not in seen:
            seen["messages"] = messages
        return {"content": "好的，已收到。", "tool_calls": [], "message": {"role": "assistant", "content": "done"}}

    monkeypatch.setattr(web_mod, "_stream_chat", fake_stream)


def test_chat_annotations_applied_before_agent(env, monkeypatch):
    """M-V3：chat 带 annotations → 确定性先行应用（独立 commit）+ brief 注入（不得回退）+ 选区上下文。"""
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

    # 标注已确定性应用：score 更新 + 独立 commit
    state = env["client"].get(f"/api/projects/{name}/state").json()
    assert state["score"]["tracks"][0]["notes"][0]["pitch_midi"] == 74
    log = env["client"].get(f"/api/projects/{name}/log").json()["log"]
    assert any("人工标注" in ln for ln in log), log
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
    assert not (Path("output") / "agent-sessions" / f"{sid}.jsonl").exists()
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
    assert not (Path("output") / "agent-sessions" / f"{r['session_id']}.jsonl").exists()
