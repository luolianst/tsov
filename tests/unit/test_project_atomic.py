"""P0 A2：Project 写锁 + 全链路原子写（score/journal/staging/arrange/tune）。"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

import pytest

from tsov import staging
from tsov.arrange import store as arrange_store
from tsov.core.score import Score
from tsov.host.journal import ActionJournal
from tsov.host.project import Project
from tsov.tune import store as tune_store

from unit._cleanup import rmtree_force


@pytest.fixture()
def base():
    d = Path("output") / f"atomic-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        rmtree_force(d)


def _payload(title="base"):
    return {
        "title": title, "tempo": 120.0, "key_candidates": [],
        "tracks": [{
            "name": "m",
            "instrument": {"backend": "fluidsynth", "program": "piano", "volume": 0.8, "effects": []},
            "notes": [],
        }],
        "meta": {},
    }


def _boom(*_a, **_k):
    raise OSError("replace failed (injected)")


def test_save_atomic_failure_keeps_old(base, monkeypatch):
    proj = Project.create("p", Score.from_dict(_payload("old")), parent=base)
    path = proj.root / "score.json"
    old = path.read_text(encoding="utf-8")
    proj.score.title = "new"

    monkeypatch.setattr("tsov.core.atomic.os.replace", _boom)
    with pytest.raises(OSError):
        proj.save()

    assert path.read_text(encoding="utf-8") == old          # 旧内容完好
    assert not list(path.parent.glob("score.json.tmp-*"))   # 临时件已清


def test_apply_score_serialized_by_lock(base):
    """两线程同时整谱替换：save 不重叠（锁生效），最终文件可解析。"""
    proj = Project.create("p", Score.from_dict(_payload("init")), parent=base)
    seen = {"n": 0, "overlap": False}
    orig = proj.save

    def slow_save():
        seen["n"] += 1
        if seen["n"] > 1:
            seen["overlap"] = True
        time.sleep(0.05)
        orig()
        seen["n"] -= 1

    proj.save = slow_save

    def worker(title: str):
        proj.apply_score(Score.from_dict(_payload(title)), f"set {title}")

    ts = [threading.Thread(target=worker, args=(t,)) for t in ("A", "B")]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)

    assert seen["overlap"] is False
    data = json.loads((proj.root / "score.json").read_text(encoding="utf-8"))
    assert data["title"] in ("A", "B")


def test_journal_index_atomic_on_replace_failure(base, monkeypatch):
    j = ActionJournal(base)
    j.append(source="user", label="one")
    old = j.index_path.read_text(encoding="utf-8")

    monkeypatch.setattr("tsov.core.atomic.os.replace", _boom)
    j.append(source="user", label="two")   # _save 吞 OSError；文件保持旧内容

    assert j.index_path.read_text(encoding="utf-8") == old


def test_arrange_and_tune_batch_atomic(base, monkeypatch):
    for mod, key in ((arrange_store, "arrange"), (tune_store, "tune")):
        p = mod.save_batch(base, {"batch_ts": "20261017-120000", "tracks": []})
        old = p.read_text(encoding="utf-8")
        monkeypatch.setattr("tsov.core.atomic.os.replace", _boom)
        with pytest.raises(OSError):
            mod.save_batch(base, {"batch_ts": "20261017-120000", "tracks": [], "x": 1})
        assert p.read_text(encoding="utf-8") == old, key
        monkeypatch.undo()


def test_staging_state_atomic(base, monkeypatch):
    staging.set_state(base, "chain:r1", "adopted")
    p = base / staging.STATE_FILE
    old = p.read_text(encoding="utf-8")

    monkeypatch.setattr("tsov.core.atomic.os.replace", _boom)
    with pytest.raises(OSError):
        staging.set_state(base, "chain:r2", "discarded")

    assert p.read_text(encoding="utf-8") == old
    assert not list(base.glob(".tsov-state.json.tmp-*"))
