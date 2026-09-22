"""宿主编辑命令层单元测试（ADR-0015）：NoteDiff / EditBatch / Project（undo-redo + git）。"""

import json
import shutil
import time
import uuid
from pathlib import Path

import pytest

from tsov.core.key import detect_key
from tsov.core.notes import Note
from tsov.core.score import Instrument, KeyCandidate, Score, Track
from tsov.host import EditBatch, Project, diff_notes

from unit._cleanup import rmtree_force

_HAS_GIT = shutil.which("git") is not None


@pytest.fixture()
def ws():
    d = Path("output") / f"wstest-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    rmtree_force(d)   # 清理：.git/objects 只读属性 → chmod 强删（防残物堆积，见 _cleanup.py）


def _note(start, end, pitch, vel=0.8):
    return Note(start=start, end=end, pitch_midi=pitch, pitch_hz=440.0 * 2 ** ((pitch - 69) / 12), velocity=vel)


def _score(notes):
    return Score(
        title="t",
        tempo=100.0,
        tracks=[Track(name="melody", instrument=Instrument(program="piano", volume=0.8), notes=notes)],
    )


# ---------------------------------------------------------------------------
# NoteDiff
# ---------------------------------------------------------------------------


def test_diff_detects_added_removed_changed():
    before = [_note(0.0, 0.5, 60), _note(0.6, 1.0, 62, vel=0.8)]
    after = [_note(0.0, 0.5, 60), _note(0.6, 1.2, 62, vel=0.5), _note(1.3, 1.6, 64)]
    d = diff_notes(before, after)
    assert len(d.added) == 1 and d.added[0].pitch_midi == 64
    assert len(d.removed) == 0
    assert len(d.changed) == 1 and d.changed[0][0].pitch_midi == 62
    assert d.total == 2
    assert "1" in d.summary()
    data = d.to_dict()
    assert set(data) == {"added", "removed", "changed", "summary", "total"}


def test_diff_removed_note():
    d = diff_notes([_note(0.0, 0.5, 60), _note(0.6, 1.0, 62)], [_note(0.0, 0.5, 60)])
    assert len(d.removed) == 1 and d.removed[0].pitch_midi == 62
    assert d.total == 1


def test_diff_no_change():
    d = diff_notes([_note(0.0, 0.5, 60)], [_note(0.0, 0.5, 60)])
    assert d.total == 0 and d.summary() == "无改动"


# ---------------------------------------------------------------------------
# EditBatch
# ---------------------------------------------------------------------------


def test_batch_transpose_and_set():
    score = _score([_note(0.0, 0.5, 60), _note(0.6, 1.0, 62)])
    batch = EditBatch(label="整体+2")
    batch.add("transpose", value=2)
    batch.add("set_velocity", index=0, value=0.6)
    new, result = batch.apply(score)
    assert result.ok and result.applied == 2
    assert [n.pitch_midi for n in new.tracks[0].notes] == [62, 64]
    assert abs(new.tracks[0].notes[0].velocity - 0.6) < 1e-9
    # 原 Score 未被脏
    assert [n.pitch_midi for n in score.tracks[0].notes] == [60, 62]


def test_batch_partial_apply_records_errors():
    score = _score([_note(0.0, 0.5, 60)])
    batch = EditBatch()
    batch.add("set_pitch", index=0, value=72)
    batch.add("set_pitch", index=9, value=72)  # 越界 → 拒绝
    new, result = batch.apply(score)
    assert not result.ok and result.applied == 1 and len(result.errors) == 1
    assert new.tracks[0].notes[0].pitch_midi == 72  # 合法命令生效


def test_batch_add_remove_time():
    score = _score([_note(0.0, 0.5, 60)])
    batch = EditBatch()
    batch.add("add", value={"pitch_midi": 64, "start": 0.6, "end": 0.9})
    batch.add("set_time", index=0, value={"start": 0.05, "end": 0.55})
    batch.add("remove", index=1)
    new, result = batch.apply(score)
    assert result.ok and result.applied == 3
    notes = new.tracks[0].notes
    assert len(notes) == 1
    assert abs(notes[0].start - 0.05) < 1e-9 and notes[0].pitch_midi == 60


def test_batch_validation_rejects_bad_values():
    score = _score([_note(0.0, 0.5, 60)])
    for op, kw in [
        ("set_pitch", {"index": 0, "value": 200}),
        ("set_velocity", {"index": 0, "value": 2.0}),
        ("set_time", {"index": 0, "value": {"start": 1.0, "end": 0.5}}),
        ("add", {"value": {"pitch_midi": -1}}),
        ("transpose", {"value": "abc"}),
        ("unknown_op", {"index": 0}),
    ]:
        batch = EditBatch().add(op, **kw)
        _, result = batch.apply(score)
        assert not result.ok, f"{op} 应被拒绝"
        assert result.applied == 0 and result.errors


# ---------------------------------------------------------------------------
# Project（undo/redo + git）
# ---------------------------------------------------------------------------


def test_project_undo_redo(ws):
    p = Project.create("p1", _score([_note(0.0, 0.5, 60)]), parent=ws)
    batch = EditBatch(label="+12")
    batch.add("transpose", value=12)
    out = p.apply_batch(batch)
    assert out["ok"] and [n.pitch_midi for n in p.score.tracks[0].notes] == [72]
    assert p.undo() and [n.pitch_midi for n in p.score.tracks[0].notes] == [60]
    assert p.redo() and [n.pitch_midi for n in p.score.tracks[0].notes] == [72]
    assert (p.root / "score.json").exists()


def test_project_apply_score(ws):
    """apply_score（整谱落定）：agent 高层编辑结果采用 = diff + 快照窗口 + undo（M-V7 D2：零 commit）。"""
    p = Project.create("pa", _score([_note(0.0, 0.5, 60), _note(0.6, 1.0, 62)]), parent=ws)

    # 相同谱 → 拒绝（无空条目）
    same = _score([_note(0.0, 0.5, 60), _note(0.6, 1.0, 62)])
    out = p.apply_score(same)
    assert out["ok"] is False and out["applied"] == 0

    # 改音 → 快照窗口条目 + diff + 可撤销（不再 commit）
    edited = _score([_note(0.0, 0.5, 64), _note(0.6, 1.0, 62)])
    out = p.apply_score(edited, commit_message="agent：改第一个音")
    assert out["ok"] is True and out["commit"] is None and out["seq"] and out["diff"]["total"] >= 1
    assert [n.pitch_midi for n in p.score.tracks[0].notes] == [64, 62]
    assert not any("agent：" in ln for ln in p.log())   # M-V7 D2：编辑零 commit
    assert p.can_undo
    assert p.undo() and [n.pitch_midi for n in p.score.tracks[0].notes] == [60, 62]


def test_project_apply_batch_syncs_key_candidates(ws):
    """命令层编辑后 key_candidates 同步重算（M-V2.4 审计修复）。

    原 bug：/batch transpose 后调性标签不更新——apply_batch 只做 diff+save，
    重算逻辑只存在于 edit_score 路径。修后：与 detect_key(新音符) 结果一致。
    """
    notes = [_note(0.0, 0.5, 60), _note(0.6, 1.1, 62), _note(1.2, 1.7, 64), _note(1.8, 2.3, 67)]  # C D E G
    p = Project.create("pk", _score(notes), parent=ws)
    p.score.key_candidates = [KeyCandidate(key="陈旧标签", confidence=0.9)]
    p.save()
    out = p.apply_batch(EditBatch(label="+2").add("transpose", value=2))
    assert out["ok"] and out["applied"] == 1
    expected = [k.key for k in detect_key(p.score.tracks[0].notes)]
    got = [k.key for k in p.score.key_candidates]
    assert got and got == expected, f"key_candidates 应随新谱重算：{got} vs {expected}"


def test_project_summary_caps_notes(ws):
    notes = [_note(i * 0.3, i * 0.3 + 0.2, 60 + i % 5) for i in range(120)]
    p = Project.create("p2", _score(notes), parent=ws)
    s = p.summary()
    assert "project=p2" in s and "tracks=1" in s and "共 120 音" in s
    assert "120" in s and "[80]" not in s  # 封顶后不再列具体音符


@pytest.mark.skipif(not _HAS_GIT, reason="git 不可用")
def test_project_git_sparse_commits_and_rollback(ws):
    """M-V7 D2 双账本：编辑零 commit；收藏 = commit+tag 二连；回滚读收藏点。"""
    p = Project.create("p3", _score([_note(0.0, 0.5, 60)]), parent=ws)
    n0 = len(p.log())
    out1 = p.apply_batch(EditBatch(label="+2").add("transpose", value=2), commit_message="第一轮：+2")
    assert out1["commit"] is None and out1["seq"]                      # 编辑零 commit
    assert len(p.log()) == n0                                          # git log 不增长
    r1 = p.favorite("v1")                                              # 收藏 → commit + tag
    assert r1["ok"] and r1["committed"] and r1["tag"].startswith("fav/")
    p.apply_batch(EditBatch(label="+2").add("transpose", value=2), commit_message="第二轮：+2")
    assert [n.pitch_midi for n in p.score.tracks[0].notes] == [64]
    assert len(p.log()) == n0 + 1                                      # 仍只有收藏产生的新 commit
    r2 = p.favorite(auto=True)
    assert r2["ok"] and r2["tag"].endswith("-auto")
    # 回滚：HEAD~1 = 第一次收藏（[62]）；HEAD~2 = 初始（[60]）
    assert p.rollback("HEAD~1")
    assert [n.pitch_midi for n in p.score.tracks[0].notes] == [62]
    assert p.rollback("HEAD~2")
    assert [n.pitch_midi for n in p.score.tracks[0].notes] == [60]
    # 回滚后的工作区相对 HEAD 有改动 → 收藏正常 commit
    r3 = p.favorite("after-rollback")
    assert r3["ok"] and r3["committed"]
    # 收藏防空收：紧接着无改动再收藏 → 不再 commit（tag 照出）
    r4 = p.favorite("clean")
    assert r4["ok"] and r4["committed"] is None


@pytest.mark.skipif(not _HAS_GIT, reason="git 不可用")
def test_project_recovers_from_empty_git_residue(ws):
    """空 .git 目录残留（删目录失败场景）→ 重建仓库 + 基线提交，不向上穿透父仓库。

    2026-09-21 实测坑：`(root/'.git').exists()` 判定会被空目录穿透——init 被跳过、
    log 显示父仓库历史、commit 指错仓库；现以 `.git/HEAD` 判定。
    """
    root = ws / "proj"
    root.mkdir()
    (root / ".git").mkdir()                      # 模拟：删工程残留的空 .git 目录
    p = Project(score=_score([_note(0.0, 0.5, 60)]), root=root, name="proj")
    assert (root / ".git" / "HEAD").exists()     # 已重建为有效仓库
    entries = p.log(5)
    assert entries and entries[0].endswith("init: proj")   # 基线提交落在本工程


@pytest.mark.skipif(not _HAS_GIT, reason="git 不可用")
def test_project_ignore_wav(ws):
    p = Project.create("p4", _score([_note(0.0, 0.5, 60)]), parent=ws)
    (p.root / "song.wav").write_bytes(b"RIFFxxxx")
    p.apply_batch(EditBatch(label="x").add("set_velocity", index=0, value=0.5), commit_message="x")
    r = p.favorite("wav-check")     # M-V7 D2：收藏时才 commit
    assert r["ok"] and r["committed"]
    # score.json 入库；wav / 运行时状态不入库
    tracked = [ln.split()[-1] for ln in p._git("ls-files").stdout.strip().splitlines() if ln]
    assert "score.json" in tracked
    assert "song.wav" not in tracked
    assert not any(t.startswith(".agent-actions/") or t == ".tsov-state.json" for t in tracked)


def test_project_open_roundtrip(ws):
    p = Project.create("p5", _score([_note(0.0, 0.5, 60)]), parent=ws)
    p.apply_batch(EditBatch().add("transpose", value=5))
    p2 = Project.open(p.root)
    assert [n.pitch_midi for n in p2.score.tracks[0].notes] == [65]
    assert p2.name == "p5"


@pytest.mark.skipif(not _HAS_GIT, reason="git 不可用")
def test_project_score_at_returns_any_rev(ws):
    """score_at（议题 ④）：git show 任意版本，不改工作区/HEAD；坏版本返回 None。

    M-V7 D2：编辑不再 per-op commit——版本由收藏点产生（HEAD = 最近收藏）。
    """
    p = Project.create("p6", _score([_note(0.0, 0.5, 60)]), parent=ws)
    p.apply_batch(EditBatch(label="+2").add("transpose", value=2), commit_message="第一轮：+2")
    r = p.favorite("v1")                                    # 收藏 → 新 commit（[62] 状态）
    assert r["ok"]
    head = p.log()[0].split(" ")[0]
    # 当前 HEAD 版本（= 收藏点）
    s_now = p.score_at("HEAD")
    assert s_now and [n.pitch_midi for n in s_now.tracks[0].notes] == [62]
    # 初始版本（基线提交）
    s_init = p.score_at(head + "~1")
    assert s_init and [n.pitch_midi for n in s_init.tracks[0].notes] == [60]
    # 工作区未变
    assert [n.pitch_midi for n in p.score.tracks[0].notes] == [62]
    # 坏版本
    assert p.score_at("NO_SUCH_REV") is None
