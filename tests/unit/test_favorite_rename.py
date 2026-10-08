"""v0.2 批D（P15）单测：收藏改名（rename_favorite）——前缀保留 / 指向旧 commit / 零改动负例。"""

import uuid
from pathlib import Path

import pytest

from tsov.core.notes import Note
from tsov.core.score import Instrument, Score, Track
from tsov.host import Project

from unit._cleanup import rmtree_force


@pytest.fixture()
def ws():
    d = Path("output") / f"wstest-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    rmtree_force(d)


def _note(start, end, pitch, vel=0.8):
    return Note(start=start, end=end, pitch_midi=pitch, pitch_hz=440.0 * 2 ** ((pitch - 69) / 12), velocity=vel)


def _mk(ws) -> Project:
    score = Score(
        title="t",
        tempo=100.0,
        tracks=[Track(name="melody", instrument=Instrument(program="piano", volume=0.8), notes=[_note(0, 0.5, 60)])],
    )
    return Project(score=score, root=ws / "proj", name="proj")


def test_rename_keeps_prefix_move_tag_not_head(ws):
    p = _mk(ws)
    old = p.favorite("v1")["tag"]                                        # fav/TS-v1
    commit_old = (p._git("rev-list", "-n1", old).stdout or "").strip()
    p.score.title = "t2"; p.save()                                       # 制造脏改动
    p.commit("later")                                                    # HEAD 前移
    r = p.rename_favorite(old, "副歌定稿")
    assert r["ok"] and r["old"] == old
    ts = old[len("fav/"):].split("-", 2)[:2]
    assert r["tag"] == f"fav/{ts[0]}-{ts[1]}-副歌定稿"                    # 时间戳前缀保留
    assert (p._git("tag", "--list", old).stdout or "").strip() == ""     # 旧 tag 已删
    assert r["tag"] in (p._git("tag", "--list").stdout or "")
    assert (p._git("rev-list", "-n1", r["tag"]).stdout or "").strip() == commit_old  # 指向旧 commit
    assert commit_old != (p._git("rev-parse", "HEAD").stdout or "").strip()          # 没打 HEAD


def test_rename_plain_tag_gets_tail(ws):
    p = _mk(ws)
    old = p.favorite()["tag"]                                            # fav/TS（无尾标）
    r = p.rename_favorite(old, "release1")
    assert r["ok"]
    assert r["tag"].startswith("fav/") and r["tag"].count("-") == 2 and r["tag"].endswith("-release1")


def test_rename_rejects_illegal_missing_nonfav(ws):
    p = _mk(ws)
    old = p.favorite("v1")["tag"]
    assert not p.rename_favorite(old, "")["ok"]                          # 空名
    assert not p.rename_favorite(old, "!!@@")["ok"]                      # 清洗后空
    assert not p.rename_favorite("fav/20200101-000000-x", "w")["ok"]     # 不存在
    assert not p.rename_favorite("v1", "w")["ok"]                        # 非 fav
    assert old in (p._git("tag", "--list").stdout or "")                 # 零改动


def test_rename_conflict_zero_change(ws):
    p = _mk(ws)
    old = p.favorite("v1")["tag"]
    ts = old[len("fav/"):].split("-", 2)[:2]
    p._git("tag", f"fav/{ts[0]}-{ts[1]}-occupied")                       # 预占位
    r = p.rename_favorite(old, "occupied")
    assert not r["ok"] and "占用" in r["error"]
    assert old in (p._git("tag", "--list").stdout or "")                 # 旧不动


def test_rename_cleaning_and_noop(ws):
    p = _mk(ws)
    old = p.favorite("v1")["tag"]
    r = p.rename_favorite(old, "副 歌!")                                  # 清洗 → 副歌
    assert r["ok"] and r["tag"].endswith("-副歌")
    r2 = p.rename_favorite(r["tag"], "副歌")                              # 同名 → no-op ok
    assert r2["ok"] and r2.get("renamed") is False
