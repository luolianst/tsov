"""批B（工具即控件 · 动作卡）呈现层单测：标签覆盖 / 参数摘要 / 影响范围。

覆盖 ADR-0017 的「工具 UI 呈现覆盖率」硬指标（21/21 工具在标签表里）与摘要/影响的纯函数行为。
"""

from __future__ import annotations

from tsov.agent.tools import build_default_registry
from tsov.core.notes import Note
from tsov.core.score import Instrument, Score, Track
from tsov.web_actions import (READ_TOOLS, SCORE_WRITING_TOOLS, TOOL_LABELS, impact_of,
                             summarize_args, tool_label)


def _note(start: float, end: float, pitch: int) -> Note:
    return Note(start=start, end=end, pitch_midi=pitch, pitch_hz=440.0, velocity=0.8)


def _score(volume: float = 0.8, notes: list[Note] | None = None, tempo: float = 200.0) -> Score:
    return Score(
        title="t",
        tempo=tempo,
        time_signature="6/8",
        tracks=[Track(name="melody", instrument=Instrument(backend="fluidsynth", program="piano", volume=volume),
                      notes=list(notes or []))],
    )


def test_labels_cover_all_registered_tools():
    """21/21：每个注册工具都有中文标签；只读/写集都是注册工具的子集。"""
    reg = build_default_registry()
    names = {s.name for s in reg.specs()}
    assert len(names) == 21
    assert names - set(TOOL_LABELS) == set(), f"缺标签：{sorted(names - set(TOOL_LABELS))}"
    assert READ_TOOLS <= names and SCORE_WRITING_TOOLS <= names
    assert not (READ_TOOLS & SCORE_WRITING_TOOLS)
    # 每个工具都能取到标签与摘要（不炸）
    for n in names:
        assert tool_label(n)
        assert isinstance(summarize_args(n, {}), str)


def test_summarize_args_smoke():
    assert summarize_args("set_tempo", {"tempo": 180, "time_signature": "6/8"}) == "♩=180 · 6/8"
    assert "3" in summarize_args("write_notes", {"notes": [1, 2, 3], "track": 1, "mode": "replace"})
    assert summarize_args("duplicate_bars", {"src_start_bar": 1, "src_end_bar": 8, "dest_start_bar": 9}).startswith("小节 1-8 → 9")
    assert "piano-pop-reverb" in summarize_args("apply_effect", {"track": 2, "preset": "piano-pop-reverb"})
    assert summarize_args("未知工具", {"x": 1}) == ""
    assert summarize_args("write_notes", None) == "轨 0 · ? 个音（append）"


def test_impact_of_notes_mix_and_noop():
    n1, n2 = _note(0.0, 0.5, 60), _note(0.5, 1.0, 62)
    a = _score(0.8, [n1])
    b = _score(0.6, [n1, n2])
    imp = impact_of(a.to_dict(), b.to_dict())
    assert "melody" in imp["text"] and "+1 音" in imp["text"] and "音量" in imp["text"]
    assert imp["tracks"][0]["added"] == 1 and imp["tracks"][0]["removed"] == 0
    # 速度/拍号变化进 text
    c = _score(0.8, [n1], tempo=180.0)
    assert "♩200→180" in impact_of(a.to_dict(), c.to_dict())["text"]
    # 一致 → 空影响；None 输入安全
    assert impact_of(a.to_dict(), a.to_dict())["text"] == ""
    assert impact_of(None, b.to_dict())["text"] == ""
    assert impact_of(a.to_dict(), None)["text"] == ""


def test_action_journal_roundtrip_and_gc():
    """批B B1-2：快照日志——内容寻址/持久/失效标记/窗口淘汰+GC。"""
    import uuid
    from pathlib import Path

    from tsov.web_actions import ActionJournal

    from unit._cleanup import rmtree_force

    d = Path("output") / f"acttest-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        n1 = _note(0.0, 0.5, 60)
        s1 = _score(0.8, [n1]).to_dict()
        s2 = _score(0.65, [n1]).to_dict()
        j = ActionJournal(d)
        h1, h2 = j.snapshot(s1), j.snapshot(s2)
        assert h1 and h2 and h1 != h2
        assert (d / ".agent-actions" / f"{h1}.json").is_file()
        e = j.append(session_id="s", turn=1, tool="set_track_mix", args="轨 melody · 音量 0.65",
                     pre=h1, post=h2, impact={"text": "melody：音量 0.80→0.65"})
        assert e["seq"] == 1 and e["label"] == "调音量/声像"

        # 重新打开 → 条目与快照可读（持久化）
        j2 = ActionJournal(d)
        got = j2.get(1)
        assert got and got["tool"] == "set_track_mix"
        assert j2.load(got["pre"])["tracks"][0]["instrument"]["volume"] == 0.8
        assert j2.load(got["post"])["tracks"][0]["instrument"]["volume"] == 0.65

        # 失效标记持久
        j2.mark_stale_from(1)
        assert ActionJournal(d).get(1)["stale"] is True

        # 窗口淘汰：超 MAX_ENTRIES → 只留最近 N 条
        for _ in range(ActionJournal.MAX_ENTRIES + 3):
            j2.append(session_id="s", turn=2, tool="set_tempo", args="♩=180",
                      pre=h1, post=h2, impact={"text": "♩200→180"})
        j3 = ActionJournal(d)
        assert len(j3.entries) == ActionJournal.MAX_ENTRIES
        assert j3.entries[0]["seq"] > 1
    finally:
        rmtree_force(d)
