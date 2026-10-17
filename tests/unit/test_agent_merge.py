"""P0 A6：agent 采用走三方合并 + agent_lock 不漏锁。"""

from __future__ import annotations

import copy
import threading
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

from tsov.core.score import Score
from tsov.host.project import Project
from tsov.webapp.agent_session import _adopt_mid_round, _run_agent_session
from tsov.webapp.state import WebState

from unit._cleanup import rmtree_force


def _payload():
    return {
        "title": "base", "tempo": 120.0, "time_signature": "4/4", "key_candidates": [],
        "tracks": [{
            "name": "m",
            "instrument": {"backend": "fluidsynth", "program": "piano", "volume": 0.8, "effects": []},
            "notes": [{"start": 0.0, "end": 0.5, "pitch_midi": 60, "pitch_hz": 261.63,
                       "velocity": 0.8, "confidence": 0.9, "deviation_cents": 0.0, "is_ornament": False}],
        }],
        "meta": {},
    }


@pytest.fixture()
def base_dir():
    d = Path("output") / f"agentmerge-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        rmtree_force(d)


class _Bus:
    def __init__(self):
        self.events = []

    def publish(self, project, event, data):
        self.events.append((event, data))


def test_mid_round_adopt_keeps_user_edit(base_dir):
    proj = Project.create("p", Score.from_dict(_payload()), parent=base_dir)
    preseed = proj.score.to_dict()

    # 用户轮中手改：音高 60 → 62
    proj.score.tracks[0].notes[0].pitch_midi = 62
    proj.save()

    # agent 只改标题（未碰音符）→ 合并应保留手改 + 落地标题
    post = copy.deepcopy(preseed)
    post["title"] = "agent 改标题"
    bus = _Bus()
    ok = _adopt_mid_round(proj, "p", bus, preseed, post, "agent 测试")

    assert ok is True
    assert proj.score.tracks[0].notes[0].pitch_midi == 62
    assert proj.score.title == "agent 改标题"


def test_mid_round_conflict_skips_adoption(base_dir):
    proj = Project.create("p", Score.from_dict(_payload()), parent=base_dir)
    preseed = proj.score.to_dict()
    proj.score.tracks[0].notes[0].pitch_midi = 62
    proj.save()

    post = copy.deepcopy(preseed)
    post["tracks"][0]["notes"][0]["pitch_midi"] = 64   # agent 也改同一音
    bus = _Bus()
    ok = _adopt_mid_round(proj, "p", bus, preseed, post, "agent 测试")

    assert ok is False
    assert proj.score.tracks[0].notes[0].pitch_midi == 62   # 手改保住
    assert any("冲突" in str(d.get("observation", "")) for _, d in bus.events)


def test_agent_lock_released_when_prepare_raises(base_dir):
    """准备段（get_project）抛错也不能漏锁。"""
    state = WebState(output_dir=base_dir)
    assert state.agent_lock.acquire(blocking=False)
    with pytest.raises(HTTPException):
        _run_agent_session(state, "no-such-project", "hi", "sid")
    assert state.agent_lock.locked() is False
