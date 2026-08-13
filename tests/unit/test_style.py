"""style.py 单元测试（M6 参考曲风格改谱闭环）：feedback 构造 + MOSS 不可用报错 + 无 LLM 模式。

LLM/MOSS 外部调用 mock 掉（离线可跑精神）。
"""

import pytest

from tsov.analysis.style import build_style_feedback, style_transfer
from tsov.core.notes import Note
from tsov.core.score import Instrument, Score, Track


def _score():
    return Score(
        title="t", tempo=120.0,
        tracks=[Track(name="melody", instrument=Instrument(), notes=[
            Note(start=0.0, end=0.5, pitch_midi=50, pitch_hz=146.8, velocity=0.8, confidence=0.9),
            Note(start=0.6, end=1.1, pitch_midi=52, pitch_hz=164.8, velocity=0.8, confidence=0.9),
        ])],
    )


def test_build_style_feedback_contains_fields():
    f = build_style_feedback(
        {"description": "忧郁的民谣", "tags": ["民谣", "忧郁"], "lyrics": "都怪我该沉默时沉默"},
        extra_feedback="再加点停顿",
    )
    assert "忧郁的民谣" in f
    assert "民谣" in f and "忧郁" in f
    assert "都怪我该沉默时沉默" in f
    assert "再加点停顿" in f
    assert "保持旋律轮廓和节奏大致不变" in f


def test_build_style_feedback_empty_fields():
    f = build_style_feedback({"description": "", "tags": [], "lyrics": ""})
    assert "（无描述）" in f
    assert "（无标签）" in f
    assert "（无）" in f


def test_style_transfer_moss_unavailable(monkeypatch):
    def fake_understand(path, url, timeout):
        raise ConnectionError("connect failed")

    monkeypatch.setattr("tsov.analysis.style.moss_understand", fake_understand)
    with pytest.raises(RuntimeError) as ei:
        style_transfer(_score(), "ref.mp3", llm=True)
    assert "MOSS 服务不可用" in str(ei.value)


def test_style_transfer_moss_error_field(monkeypatch):
    def fake_understand(path, url, timeout):
        return {"description": "", "tags": [], "lyrics": "", "error": "模型加载中"}

    monkeypatch.setattr("tsov.analysis.style.moss_understand", fake_understand)
    with pytest.raises(RuntimeError) as ei:
        style_transfer(_score(), "ref.mp3", llm=True)
    assert "MOSS 理解失败" in str(ei.value)


def test_style_transfer_no_llm(monkeypatch):
    def fake_understand(path, url, timeout):
        return {"description": "民谣", "tags": ["民谣"], "lyrics": "歌词"}

    def fake_edit(score, feedback, llm, **params):
        from tsov.analysis.edit import EditResult

        return EditResult(score, ["~ idx0 保持原样"], "", False)

    monkeypatch.setattr("tsov.analysis.style.moss_understand", fake_understand)
    monkeypatch.setattr("tsov.analysis.style.edit_score", fake_edit)
    result, info = style_transfer(_score(), "ref.mp3", llm=False)
    assert info["understanding"]["description"] == "民谣"
    assert "民谣" in info["feedback"]
    assert result.llm_used is False
    assert result.diff_summary
