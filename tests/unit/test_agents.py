"""M-V8 E4 段1 单测：工程上下文 agents.md（双层）——状态摘要 / 骨架 / 更新纪律 / REST。

- 状态摘要：确定性（同 score 同 ts → 逐位相同）；含调性/速度/拍号/编制
- 骨架：首次建档含「风格意图/历史与决策/偏好」；指针指向 agents-user.md
- 更新：只换标记区间——手写内容保留；无标记的用户自建文件 → 自动块插在首个标题后
- 全局：ensure_user_agents 幂等建档
- REST：GET 现状 / POST sync（工程 + 全局同建；agent 与 UI 同径）
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tsov import agents as agents_mod
from tsov.core.notes import Note
from tsov.core.score import Instrument, KeyCandidate, Score, Track
from tsov.web import create_app

from unit._cleanup import rmtree_force


def _note(i: int) -> Note:
    return Note(start=i * 0.5, end=i * 0.5 + 0.4, pitch_midi=60 + i, pitch_hz=261.63)


def _score(*, tempo=96.0, sig="6/8", keys=None, notes=5) -> Score:
    tracks = [
        Track(name="主题", instrument=Instrument(program="piano", volume=1.0, effects=[]),
              notes=[_note(i) for i in range(notes)]),
        Track(name="哼唱", kind="audio", audio={"file": "audio/x.wav", "offset": 0.0}),
    ]
    return Score(title="t", tempo=tempo, time_signature=sig,
                 key_candidates=[] if keys is None else keys, tracks=tracks)


def _keys():
    return [KeyCandidate(key="C major", confidence=0.8)]


@pytest.fixture()
def env(monkeypatch):
    d = Path("output") / f"agentstest-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("tsov.webapp.config.AGENT_SESSION_DIR", str(d / "agent-sessions"))
    app = create_app(output_dir=d)
    client = TestClient(app)
    try:
        yield {"dir": d, "app": app, "client": client}
    finally:
        rmtree_force(d)


# ---------------------------------------------------------------------------
# 状态摘要
# ---------------------------------------------------------------------------


def test_state_block_shape():
    block = agents_mod.build_state_block(_score(keys=_keys()), name="p1", ts=1_700_000_000)
    assert "速度：96 BPM" in block and "拍号：6/8" in block
    assert "C major（置信 0.8）" in block
    assert "2 轨 · 5 音符" in block
    assert "  - 0 主题" in block and "  - 1 哼唱 · 音频" in block


def test_state_block_deterministic():
    s = _score(keys=_keys())
    a = agents_mod.build_state_block(s, name="p1", ts=1_700_000_000)
    b = agents_mod.build_state_block(s, name="p1", ts=1_700_000_000)
    assert a == b


def test_state_block_key_fallback():
    # 无 key_candidates 且音符 ≥4 → 现场 detect（不显示 —）
    block = agents_mod.build_state_block(_score(notes=6), name="p1", ts=1)
    assert "调性：—" not in block.split("\n")[1] or "detect" not in block
    assert "调性：" in block and "（置信" in block
    # 音符 <4 → 样本不足 → —
    block2 = agents_mod.build_state_block(_score(notes=2), name="p1", ts=1)
    assert "调性：—" in block2


# ---------------------------------------------------------------------------
# 骨架 / 更新
# ---------------------------------------------------------------------------


def test_sync_creates_skeleton(tmp_path):
    r = agents_mod.sync_project_agents(tmp_path, _score(keys=_keys()), name="p1", ts=1)
    assert r["created"] is True and r["file"] == "agents.md"
    text = (tmp_path / "agents.md").read_text(encoding="utf-8")
    assert agents_mod.STATE_BEGIN in text and agents_mod.STATE_END in text
    assert "## 风格意图" in text and "## 历史与决策" in text and "## 偏好（工程特有）" in text
    assert "agents-user.md" in text
    assert "速度：96 BPM" in text


def test_sync_updates_block_preserves_handwritten(tmp_path):
    agents_mod.sync_project_agents(tmp_path, _score(keys=_keys()), name="p1", ts=1)
    p = tmp_path / "agents.md"
    text = p.read_text(encoding="utf-8")
    p.write_text(text.replace("## 风格意图\n", "## 风格意图\n手记：要更欢快点。\n"), encoding="utf-8")

    r2 = agents_mod.sync_project_agents(tmp_path, _score(tempo=128.0, keys=_keys()), name="p1", ts=2)
    assert r2["created"] is False and r2["updated"] is True
    text2 = p.read_text(encoding="utf-8")
    assert "手记：要更欢快点。" in text2          # 手写保留
    assert "速度：128 BPM" in text2               # 自动块已更新
    assert "速度：96 BPM" not in text2
    assert text2.count(agents_mod.STATE_BEGIN) == 1 and text2.count(agents_mod.STATE_END) == 1


def test_sync_inserts_block_into_foreign_file(tmp_path):
    p = tmp_path / "agents.md"
    p.write_text("# 我自己的文件\n\n这是用户手写内容。\n", encoding="utf-8")
    agents_mod.sync_project_agents(tmp_path, _score(keys=_keys()), name="p1", ts=1)
    text = p.read_text(encoding="utf-8")
    assert "这是用户手写内容。" in text
    assert agents_mod.STATE_BEGIN in text
    assert text.index("# 我自己的文件") < text.index(agents_mod.STATE_BEGIN)


def test_ensure_user_agents(tmp_path):
    up = tmp_path / "agents-user.md"
    r1 = agents_mod.ensure_user_agents(path=up)
    assert r1["created"] is True and up.is_file()
    text = up.read_text(encoding="utf-8")
    assert "## 偏好" in text and "## 统计" in text and "工程覆盖全局" in text
    r2 = agents_mod.ensure_user_agents(path=up)
    assert r2["created"] is False


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


def _make_project(env, name="p1"):
    r = env["client"].post("/api/projects", json={
        "name": name,
        "score": {
            "title": "t", "tempo": 120.0, "time_signature": "4/4",
            "key_candidates": [{"key": "C major", "confidence": 0.7}],
            "tracks": [{"name": "melody",
                        "instrument": {"backend": "fluidsynth", "program": "piano",
                                       "volume": 0.8, "effects": []},
                        "notes": []}],
            "meta": {},
        }})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text


def test_rest_agents_flow(env, monkeypatch, tmp_path_factory):
    up = Path("output") / f"agentsuser-{uuid.uuid4().hex[:10]}.md"
    monkeypatch.setattr(agents_mod, "user_agents_path", lambda: up)
    _make_project(env)
    client = env["client"]
    try:
        g0 = client.get("/api/projects/p1/agents").json()
        assert g0["project_md"] is None and g0["user_md"] is None

        s = client.post("/api/projects/p1/agents/sync")
        assert s.status_code == 200, s.text
        body = s.json()
        assert body["project"]["created"] is True and body["user"]["created"] is True
        assert Path(body["project"]["path"]).is_file() and up.is_file()

        g1 = client.get("/api/projects/p1/agents").json()
        assert "状态摘要" in g1["project_md"] and "速度：120 BPM" in g1["project_md"]
        assert "## 偏好" in g1["user_md"]

        s2 = client.post("/api/projects/p1/agents/sync").json()
        assert s2["project"]["created"] is False and s2["user"]["created"] is False
    finally:
        if up.exists():
            up.unlink()
