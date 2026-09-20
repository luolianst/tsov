"""M-V8 E1 单测：书签三层（段轨转正）+ 文件夹归属（组织层）。

- 书签 = score 一等公民：项目 / 文件夹 / 轨道 三作用域；section（区间）/ mark（记号）
- 与 UI 手势、agent 同一动作路径（ADR-0017）：add_bookmark / remove_bookmark / set_bookmark / set_track_folder
- 向后兼容：旧 score（无 bookmarks / folder 字段）读入缺省空
"""

from __future__ import annotations

from tsov.core.notes import Note
from tsov.core.score import Bookmark, Score, Track
from tsov.host import EditBatch


def _score() -> Score:
    return Score(
        title="t",
        tempo=120.0,
        tracks=[
            Track(name="gtr", notes=[Note(start=0.0, end=0.5, pitch_midi=60, pitch_hz=261.6, velocity=0.8)]),
            Track(name="bass", notes=[]),
        ],
    )


def _apply(score: Score, batch: EditBatch):
    return batch.apply(score)


# ---------------- add_bookmark ----------------

def test_add_bookmark_project_mark_and_section():
    s0 = _score()
    out, res = _apply(s0, EditBatch().add("add_bookmark", value={"scope": "project", "kind": "mark", "start": 3.2, "label": "riff"}))
    assert res.ok and res.applied == 1
    b = out.bookmarks[0]
    assert (b.scope, b.kind, b.start, b.end, b.label) == ("project", "mark", 3.2, None, "riff")
    out2, res2 = _apply(out, EditBatch().add("add_bookmark", value={"kind": "section", "start": 1.0, "end": 5.0, "label": "副歌"}))
    assert res2.ok and out2.bookmarks[-1].kind == "section" and out2.bookmarks[-1].end == 5.0
    assert s0.bookmarks == []           # 不脏原谱（批在深拷贝上执行）


def test_add_bookmark_track_and_folder_scope():
    out, res = _apply(_score(), EditBatch().add("set_track_folder", track=1, value={"folder": "band"}))
    assert res.ok and out.tracks[1].folder == "band"
    out2, res2 = _apply(out, EditBatch().add("add_bookmark", value={"scope": "track", "ref": "gtr", "kind": "mark", "start": 2.0, "label": "solo"}))
    assert res2.ok and out2.bookmarks[-1].ref == "gtr"
    out3, res3 = _apply(out2, EditBatch().add("add_bookmark", value={"scope": "folder", "ref": "band", "kind": "section", "start": 1.0, "end": 4.0}))
    assert res3.ok and out3.bookmarks[-1].scope == "folder"


def test_add_bookmark_rejects():
    cases = [
        {"value": {"scope": "nope", "kind": "mark", "start": 1.0}},                       # 未知 scope
        {"value": {"scope": "track", "kind": "mark", "start": 1.0}},                      # track 缺 ref
        {"value": {"scope": "track", "ref": "ghost", "kind": "mark", "start": 1.0}},      # 未知轨道
        {"value": {"scope": "folder", "ref": "ghost", "kind": "mark", "start": 1.0}},     # 未知文件夹
        {"value": {"kind": "blob", "start": 1.0}},                                        # 未知 kind
        {"value": {"kind": "mark", "start": -1.0}},                                       # start 越界
        {"value": {"kind": "section", "start": 1.0}},                                     # section 缺 end
        {"value": {"kind": "section", "start": 5.0, "end": 1.0}},                         # start>=end
        {"value": "3.2"},                                                                 # 非 dict
    ]
    for c in cases:
        _out, res = _apply(_score(), EditBatch().add("add_bookmark", value=c["value"]))
        assert res.applied == 0 and res.errors, c


# ---------------- set_bookmark ----------------

def test_set_bookmark_partial_update():
    out, _ = _apply(_score(), EditBatch().add("add_bookmark", value={"kind": "mark", "start": 2.0, "label": "a"}))
    out2, res = _apply(out, EditBatch().add("set_bookmark", index=0, value={"label": "b"}))
    assert res.ok
    b = out2.bookmarks[0]
    assert b.label == "b" and b.start == 2.0 and b.kind == "mark"
    out3, res3 = _apply(out2, EditBatch().add("set_bookmark", index=0, value={"start": 9.5, "kind": "section", "end": 12.0, "color": "#ff8800"}))
    assert res3.ok
    b3 = out3.bookmarks[0]
    assert (b3.start, b3.end, b3.kind, b3.color) == (9.5, 12.0, "section", "#ff8800")


def test_set_bookmark_rejects():
    out, _ = _apply(_score(), EditBatch().add("add_bookmark", value={"kind": "section", "start": 1.0, "end": 3.0}))
    cases = [
        {"index": 5, "value": {"label": "x"}},                                  # 越界
        {"index": None, "value": {"label": "x"}},                               # 缺 index
        {"index": 0, "value": "x"},                                             # 非 dict
        {"index": 0, "value": {"start": 9.0}},                                  # start 越过 end
        {"index": 0, "value": {"kind": "mark", "start": 5.0}},                  # 变 mark 时 start 可越 end（应成功）
    ]
    for c in cases[:-1]:
        _o, res = _apply(out, EditBatch().add("set_bookmark", index=c["index"], value=c["value"]))
        assert res.applied == 0 and res.errors, c
    # 末例：section → mark 合法（end 清空）
    o2, res2 = _apply(out, EditBatch().add("set_bookmark", index=0, value={"kind": "mark", "start": 5.0}))
    assert res2.ok and o2.bookmarks[0].kind == "mark" and o2.bookmarks[0].end is None


def test_set_bookmark_scope_change_validation():
    out, _ = _apply(_score(), EditBatch().add("add_bookmark", value={"kind": "mark", "start": 1.0}))
    _o, res = _apply(out, EditBatch().add("set_bookmark", index=0, value={"scope": "track", "ref": "ghost"}))
    assert res.applied == 0 and res.errors


# ---------------- remove_bookmark ----------------

def test_remove_bookmark():
    out, _ = _apply(_score(), EditBatch().add("add_bookmark", value={"kind": "mark", "start": 1.0, "label": "x"}))
    out, _ = _apply(out, EditBatch().add("add_bookmark", value={"kind": "mark", "start": 2.0, "label": "y"}))
    out2, res = _apply(out, EditBatch().add("remove_bookmark", index=0))
    assert res.ok and [b.label for b in out2.bookmarks] == ["y"]
    _o, res2 = _apply(out2, EditBatch().add("remove_bookmark", index=3))
    assert res2.applied == 0 and res2.errors


# ---------------- set_track_folder ----------------

def test_set_track_folder_assign_and_clear():
    out, res = _apply(_score(), EditBatch().add("set_track_folder", track=0, value={"folder": "  band  "}))
    assert res.ok and out.tracks[0].folder == "band"       # strip
    out2, res2 = _apply(out, EditBatch().add("set_track_folder", track=0, value={"folder": ""}))
    assert res2.ok and out2.tracks[0].folder == ""
    _o, res3 = _apply(out, EditBatch().add("set_track_folder", track=0, value={"folder": "x" * 65}))
    assert res3.applied == 0 and res3.errors                # 名过长
    _o2, res4 = _apply(out, EditBatch().add("set_track_folder", track=0, value=None))
    assert res4.applied == 0 and res4.errors
    _o3, res5 = _apply(out, EditBatch().add("set_track_folder", track=9, value={"folder": "b"}))
    assert res5.applied == 0 and res5.errors                # 轨越界


# ---------------- 模型往返 / 向后兼容 ----------------

def test_score_roundtrip_bookmarks_and_folder():
    s = _score()
    s.tracks[0].folder = "band"
    s.bookmarks.append(Bookmark(scope="project", kind="section", start=0.0, end=4.0, label="段1", color="#123456"))
    s.bookmarks.append(Bookmark(scope="track", ref="gtr", kind="mark", start=2.5, label="点"))
    d = s.to_dict()
    s2 = Score.from_dict(d)
    assert s2.tracks[0].folder == "band"
    assert [b.to_dict() for b in s2.bookmarks] == [b.to_dict() for b in s.bookmarks]


def test_score_legacy_dict_defaults():
    d = _score().to_dict()
    d.pop("bookmarks", None)
    d["tracks"][0].pop("folder", None)
    s2 = Score.from_dict(d)
    assert s2.bookmarks == [] and s2.tracks[0].folder == ""
