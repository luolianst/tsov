"""M-V8 小修包命令层单测：scale_time / set_tempo_remap（Q47 变速重排）+ remove_track / rename_track。

语义：改 BPM 默认「跟速重排」——全谱时间等比缩放（音符/书签/三层 automation）；
set_tempo_remap 为单命令原子（BPM/拍号校验不过 → 整体拒绝，不出现「缩放了但 tempo 没改」的分裂状态）。
"""

from __future__ import annotations

import pytest

from tsov.core.notes import Note
from tsov.core.score import Bookmark, Bus, Score, Track
from tsov.host import EditBatch


def _track(name: str = "轨0", folder: str = "", n_notes: int = 2) -> Track:
    return Track(
        name=name,
        folder=folder,
        notes=[
            Note(start=float(i), end=float(i) + 0.5, pitch_midi=60 + i, pitch_hz=261.6, velocity=0.8)
            for i in range(n_notes)
        ],
    )


def _score(tracks: int = 1) -> Score:
    """轨 + 音符 + 三层 automation（track/bus/master）+ 书签（project 段 + track 旗）。"""
    s = Score(title="t", tempo=120.0, tracks=[_track(name=f"轨{i}") for i in range(tracks)])
    s.tracks[0].automation = {"volume": [[0.0, 0.8], [2.0, 0.4]]}
    s.buses = [Bus(name="drums", automation={"volume": [[1.0, 0.7]]})]
    s.master.automation = {"pan": [[0.5, -0.2]]}
    s.bookmarks = [
        Bookmark(scope="project", kind="section", start=0.0, end=2.0, label="段A"),
        Bookmark(scope="track", ref="轨0", kind="mark", start=1.0, label="旗"),
    ]
    return s


def _apply(score: Score, batch: EditBatch):
    return batch.apply(score)


# ---------------- scale_time（纯缩放）----------------


def test_scale_time_scales_notes_bookmarks_automation():
    out, res = _apply(_score(), EditBatch().add("scale_time", value={"factor": 2.0}))
    assert res.ok and res.applied == 1
    tr = out.tracks[0]
    assert tr.notes[0].start == 0.0 and tr.notes[0].end == 1.0
    assert tr.notes[1].start == 2.0 and tr.notes[1].end == 3.0
    assert out.bookmarks[0].start == 0.0 and out.bookmarks[0].end == 4.0
    assert out.bookmarks[1].start == 2.0
    assert tr.automation["volume"][1] == [4.0, 0.4]          # t 缩放、值不动
    assert out.buses[0].automation["volume"][0][0] == 2.0    # bus 层
    assert out.master.automation["pan"][0][0] == 1.0         # master 层
    assert out.tempo == 120.0                                # 纯缩放不动 tempo


def test_scale_time_bare_number_and_roundtrip():
    a, res = _apply(_score(), EditBatch().add("scale_time", value=0.5))
    assert res.ok and a.tracks[0].notes[1].start == 0.5
    b, res2 = _apply(a, EditBatch().add("scale_time", value={"factor": 2.0}))
    assert res2.ok and b.tracks[0].notes[1].start == pytest.approx(1.0, abs=1e-6)


def test_scale_time_rejects():
    for value in ({"factor": 0}, {"factor": -1}, {"factor": 0.001}, {"factor": 200},
                  {"factor": "x"}, {"factor": {"x": 1}}, {}, "x", {"bogus": 1}):
        _out, res = _apply(_score(), EditBatch().add("scale_time", value=value))
        assert res.applied == 0 and res.errors, value


def test_scale_time_empty_score_ok():
    s = Score(title="empty", tempo=100.0, tracks=[])
    _out, res = _apply(s, EditBatch().add("scale_time", value={"factor": 1.5}))
    assert res.ok


# ---------------- set_tempo_remap（改 BPM + 缩放，单命令原子）----------------


def test_set_tempo_remap_bpm_and_scale():
    out, res = _apply(_score(), EditBatch().add("set_tempo_remap", value={"tempo": 240}))
    assert res.ok
    assert out.tempo == 240.0
    assert out.tracks[0].notes[1].start == 0.5 and out.tracks[0].notes[1].end == 0.75  # ×0.5
    assert out.bookmarks[0].end == 1.0


def test_set_tempo_remap_with_time_signature():
    out, res = _apply(_score(), EditBatch().add("set_tempo_remap", value={"tempo": 60, "time_signature": "6/8"}))
    assert res.ok and out.tempo == 60.0 and out.time_signature == "6/8"
    assert out.tracks[0].notes[1].start == 2.0               # ×2（变慢）


def test_set_tempo_remap_atomic_reject():
    """tempo/拍号非法 → 整体拒绝；音符时间原样（原子性——不会「缩放了但 tempo 没改」）。"""
    for value in ({"tempo": 10}, {"tempo": "x"}, {"tempo": 500},
                  {"tempo": 200, "time_signature": "9/9"}, {"time_signature": "6/8"}, {}, "x"):
        out, res = _apply(_score(), EditBatch().add("set_tempo_remap", value=value))
        assert res.applied == 0 and res.errors, value
        assert out.tracks[0].notes[1].start == 1.0, value


def test_set_tempo_remap_same_tempo_no_scale():
    out, res = _apply(_score(), EditBatch().add("set_tempo_remap", value={"tempo": 120}))
    assert res.ok and out.tracks[0].notes[1].start == 1.0


# ---------------- remove_track ----------------


def test_remove_track_cleans_track_bookmark_and_keeps_folder():
    s = _score(tracks=3)
    s.tracks[0].folder = "弦乐"
    s.tracks[1].folder = "弦乐"
    s.bookmarks = [
        Bookmark(scope="project", kind="section", start=0.0, end=2.0),
        Bookmark(scope="track", ref="轨0", kind="mark", start=0.5),
        Bookmark(scope="track", ref="轨1", kind="mark", start=0.5),
        Bookmark(scope="folder", ref="弦乐", kind="mark", start=0.5),
    ]
    out, res = _apply(s, EditBatch().add("remove_track", track=0))
    assert res.ok and len(out.tracks) == 2
    assert [t.name for t in out.tracks] == ["轨1", "轨2"]    # 索引前移
    refs = [(b.scope, b.ref) for b in out.bookmarks]
    assert ("track", "轨0") not in refs                      # 被删轨书签清掉
    assert ("track", "轨1") in refs and ("folder", "弦乐") in refs   # 相关引用保留
    assert len(out.bookmarks) == 3


def test_remove_track_clears_folder_bookmark_when_folder_emptied():
    s = _score(tracks=2)
    s.tracks[0].folder = "弦乐"
    s.bookmarks = [
        Bookmark(scope="folder", ref="弦乐", kind="mark", start=0.5),
        Bookmark(scope="project", kind="mark", start=0.5),
    ]
    out, res = _apply(s, EditBatch().add("remove_track", track=0))
    assert res.ok and len(out.tracks) == 1
    assert [(b.scope, b.ref) for b in out.bookmarks] == [("project", "")]


def test_remove_track_out_of_range():
    _out, res = _apply(_score(tracks=1), EditBatch().add("remove_track", track=3))
    assert res.applied == 0 and "remove_track" in res.errors[0]


# ---------------- rename_track ----------------


def test_rename_track_updates_bookmark_refs():
    s = _score(tracks=2)
    s.bookmarks = [
        Bookmark(scope="track", ref="轨0", kind="mark", start=0.5),
        Bookmark(scope="track", ref="轨1", kind="mark", start=0.5),
    ]
    out, res = _apply(s, EditBatch().add("rename_track", track=0, value={"name": "小提琴"}))
    assert res.ok and out.tracks[0].name == "小提琴"
    refs = [(b.scope, b.ref) for b in out.bookmarks]
    assert ("track", "小提琴") in refs and ("track", "轨1") in refs


def test_rename_track_rejects():
    s = _score(tracks=2)
    for value in ({"name": "轨1"},          # 重名
                  {"name": "  "},           # 空
                  {"name": "x" * 65},       # 过长
                  {}, None):
        _out, res = _apply(s, EditBatch().add("rename_track", track=0, value=value))
        assert res.applied == 0 and res.errors, value


def test_rename_track_same_name_noop():
    out, res = _apply(_score(tracks=1), EditBatch().add("rename_track", track=0, value={"name": "轨0"}))
    assert res.ok and out.tracks[0].name == "轨0"
