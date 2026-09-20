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


def test_action_journal_shim_reexport():
    """M-V7 D2：ActionJournal 已下沉 host/journal.py；web_actions 保留旧导入路径（兼容层）。"""
    from tsov.host.journal import ActionJournal as HostJournal

    from tsov.web_actions import ActionJournal as ShimJournal

    assert ShimJournal is HostJournal
