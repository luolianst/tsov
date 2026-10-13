"""批I 会话存储层单测（session_store）：懒计算 / 物化 / 绑定 / 列表 / 软删 / 改名。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

import tsov.webapp.config as cfg
from tsov.webapp import session_store

from unit._cleanup import rmtree_force


@pytest.fixture()
def sdir(monkeypatch):
    d = Path("output") / f"session-store-test-{uuid.uuid4().hex[:8]}"
    sd = d / "agent-sessions"
    sd.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cfg, "AGENT_SESSION_DIR", str(sd))
    try:
        yield sd
    finally:
        rmtree_force(d)


def _mk(sd: Path, sid: str, user_text: str = "把 pad 音量抬一点", n_users: int = 1) -> str:
    lines = [json.dumps({"role": "system", "content": "sys", "ts": "2026-10-14T00:00:00"}, ensure_ascii=False)]
    for i in range(n_users):
        lines.append(json.dumps(
            {"role": "user", "content": user_text if i == 0 else f"继续 {i}", "ts": f"2026-10-14T00:0{i}:01"},
            ensure_ascii=False))
        lines.append(json.dumps(
            {"role": "assistant", "content": "好", "ts": f"2026-10-14T00:0{i}:09"}, ensure_ascii=False))
    (sd / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sid


def test_lazy_meta_no_write(sdir):
    sid = _mk(sdir, "s1")
    m = session_store.get_meta(sid)
    assert m and m["title"] == "把 pad 音量抬一点" and m["turns"] == 1
    assert not (sdir / f"{sid}.meta.json").exists()          # 懒计算不动盘
    assert not list(sdir.glob("*.tmp"))


def test_touch_materializes(sdir):
    sid = _mk(sdir, "s1")
    session_store.touch_session(sid, project="p1")
    p = sdir / f"{sid}.meta.json"
    assert p.is_file()
    m = json.loads(p.read_text(encoding="utf-8"))
    assert m["project"] == "p1" and m["title"] and m["turns"] == 1 and m["id"] == sid
    assert not list(sdir.glob("*.tmp"))


def test_binding_roundtrip(sdir):
    _mk(sdir, "s1")
    session_store.save_binding("p1", "s1")
    assert session_store.bound_session("p1") == "s1"
    session_store.clear_binding("p1")
    assert session_store.bound_session("p1") is None
    assert not list(sdir.glob("*.tmp"))


def test_list_filter_current(sdir):
    a = _mk(sdir, "a")
    b = _mk(sdir, "b")
    c = _mk(sdir, "c")
    session_store.touch_session(a, project="p1")
    session_store.touch_session(b, project="p2")
    session_store.save_binding("p1", a)
    ids1 = {s["id"] for s in session_store.list_sessions(project="p1")}
    assert a in ids1 and c in ids1 and b not in ids1          # 本工程 + 未归类
    ids2 = {s["id"] for s in session_store.list_sessions(project="p1", all_flag=True)}
    assert {a, b, c} <= ids2
    cur = [s for s in session_store.list_sessions(project="p1") if s["id"] == a][0]
    assert cur["current"] is True and cur["bound_project"] == "p1"


def test_soft_delete_moves_and_unbinds(sdir):
    a = _mk(sdir, "a")
    session_store.touch_session(a, project="p1")
    session_store.save_binding("p1", a)
    session_store.soft_delete(a)
    assert (sdir / ".trash" / "a.jsonl").is_file()
    assert (sdir / ".trash" / "a.meta.json").is_file()
    assert not (sdir / "a.jsonl").exists()
    assert session_store.bound_session("p1") is None
    with pytest.raises(FileNotFoundError):
        session_store.soft_delete("nope")


def test_rename_guards(sdir):
    a = _mk(sdir, "a")
    m = session_store.rename_session(a, "  新名字 A  ")
    assert m["title"] == "新名字 A"
    assert session_store.get_meta(a)["title"] == "新名字 A"
    with pytest.raises(ValueError):
        session_store.rename_session(a, "   ")
