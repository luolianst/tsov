"""v0.2 批C 后段（P4/P5 轨管理）单测：move_track / move_folder（成员块移动）。

覆盖：
- move_track：向后/向前移动 / no-op 保序 / 越界与非法值 / 书签 ref=轨名不受影响
- move_folder：成员块（保序）整体移动 / 到任意位置 / 未找到与越界 / 块内成员连续化
"""

from __future__ import annotations

from tsov.core.score import Bookmark, Score, Track
from tsov.host.command import EditBatch


def _score(names=None) -> Score:
    return Score(title="t", tempo=120.0, tracks=[Track(name=n) for n in (names or ["a", "b", "c", "d"])])


def _apply(score: Score, op: str, track: int = 0, value=None):
    b = EditBatch()
    b.add(op, track=track, value=value)
    return b.apply(score)


# ---------------- move_track ----------------


def test_move_track_basic_and_noop():
    s = _score()
    out, r = _apply(s, "move_track", track=0, value={"to": 2})
    assert r.ok and r.applied == 1
    assert [t.name for t in out.tracks] == ["b", "c", "a", "d"]
    out2, r2 = _apply(out, "move_track", track=3, value={"to": 3})   # no-op
    assert r2.ok and [t.name for t in out2.tracks] == ["b", "c", "a", "d"]
    # 原 Score 不被修改（事务深拷贝）
    assert [t.name for t in s.tracks] == ["a", "b", "c", "d"]


def test_move_track_up():
    s = _score(["a", "b", "c"])
    out, r = _apply(s, "move_track", track=2, value={"to": 0})
    assert r.ok and [t.name for t in out.tracks] == ["c", "a", "b"]


def test_move_track_validation():
    s = _score(["a", "b"])
    _o, r1 = _apply(s, "move_track", track=0, value={"to": 5})
    assert r1.applied == 0 and "越界" in r1.errors[0]
    _o, r2 = _apply(s, "move_track", track=9, value={"to": 0})
    assert r2.applied == 0 and "越界" in r2.errors[0]
    _o, r3 = _apply(s, "move_track", track=0, value={"to": "x"})
    assert r3.applied == 0 and "非法" in r3.errors[0]


def test_move_track_bookmarks_by_name_safe():
    """track 层书签 ref=轨名——重排后引用仍正确（不需要索引换算）。"""
    s = _score(["a", "b", "c"])
    s.bookmarks = [Bookmark(scope="track", ref="a", kind="mark", start=1.0, label="A 记号")]
    out, r = _apply(s, "move_track", track=0, value={"to": 2})
    assert r.ok
    bm = out.bookmarks[0]
    assert bm.ref == "a" and bm.scope == "track"


# ---------------- move_folder ----------------


def _folder_score() -> Score:
    s = _score(["a", "b", "c", "d", "e"])
    for i, f in [(0, "F1"), (1, "F2"), (2, "F1"), (3, "F2"), (4, "")]:
        s.tracks[i].folder = f
    return s


def test_move_folder_block_keeps_order_and_contiguity():
    s = _folder_score()
    out, r = _apply(s, "move_folder", track=0, value={"folder": "F1", "to": 3})
    assert r.ok and r.applied == 1
    names = [t.name for t in out.tracks]
    assert names == ["b", "d", "e", "a", "c"]        # F1 块 [a,c] 保序移到（移除后数组中）末尾
    f1_idx = [i for i, t in enumerate(out.tracks) if t.folder == "F1"]
    assert f1_idx == [3, 4]                          # 成员连续化
    assert [t.folder for t in out.tracks if t.folder == "F1"] == ["F1", "F1"]


def test_move_folder_to_front():
    s = _folder_score()
    out, r = _apply(s, "move_folder", track=0, value={"folder": "F2", "to": 0})
    assert r.ok and [t.name for t in out.tracks] == ["b", "d", "a", "c", "e"]


def test_move_folder_validation():
    s = _folder_score()
    _o, r1 = _apply(s, "move_folder", track=0, value={"folder": "NOPE", "to": 0})
    assert r1.applied == 0 and "未找到" in r1.errors[0]
    _o, r2 = _apply(s, "move_folder", track=0, value={"folder": "F1", "to": 99})
    assert r2.applied == 0 and "越界" in r2.errors[0]
    _o, r3 = _apply(s, "move_folder", track=0, value={"folder": "", "to": 0})
    assert r3.applied == 0
