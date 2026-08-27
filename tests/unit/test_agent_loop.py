"""agentloop 单元/端到端测试（ADR-0012）：工具循环、观测回喂、会话落盘、DoD 全链路。

DoD 验证：`tsov agent run "读 score → 改谱 → 宿主渲染"` 在不依赖 dsh 的前提下跑通——
LLM 调用被脚本化（fake chat）+ edit 的 LLM 被确定性替换（无 key 也能全链路验证）。
"""

import json
import shutil
import uuid
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from tsov.agent import AgentLoop
from tsov.core.notes import Note
from tsov.core.score import Instrument, Score, Track
from tsov.dsp.pitch import midi_to_hz
from tsov.render.fluidsynth_backend import default_soundfont

_SOUNDFONT = default_soundfont()


@pytest.fixture()
def ws():
    """工作区临时目录：手动 mkdir（绕开 pytest tmpdir 的 AV 锁 + 沙箱对 mkdtemp 目录的拒绝）。"""
    d = Path("output") / f"wstest-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _tool_resp(name, args, call_id="t1"):
    return {
        "content": "",
        "tool_calls": [{"id": call_id, "name": name, "arguments": args}],
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": call_id, "type": "function",
                 "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
            ],
        },
    }


def _answer_resp(text):
    return {"content": text, "tool_calls": [], "message": {"role": "assistant", "content": text}}


class _FakeChat:
    """脚本化 LLM：按顺序吐预设回复。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, messages, tools=None, **kw):
        self.calls.append((messages, tools))
        assert self.script, "fake chat script 用尽（loop 多调了一轮）"
        return self.script.pop(0)


def _tiny_score() -> Score:
    return Score(
        title="t",
        tempo=100.0,
        key_candidates=[],
        tracks=[
            Track(
                name="melody",
                instrument=Instrument(backend="fluidsynth", program="piano", volume=0.8),
                notes=[
                    Note(start=0.0, end=0.5, pitch_midi=60, pitch_hz=261.63),
                    Note(start=0.6, end=1.0, pitch_midi=62, pitch_hz=293.66),
                    Note(start=1.1, end=1.5, pitch_midi=64, pitch_hz=329.63),
                ],
            )
        ],
    )


def test_doD_agent_end_to_end_read_edit_render(ws, monkeypatch):
    """DoD 全链路：读 score → 改谱（整体 +2 半音）→ 宿主渲染 wav → 最终回答。"""
    score_path = ws / "stage-04-score.json"
    score_path.write_text(json.dumps(_tiny_score().to_dict()), encoding="utf-8")
    edited_path = ws / "agent-edited-score.json"
    wav_path = ws / "out.wav"

    # edit 的内部 LLM 替换为确定性动作（无 key 也能跑）
    def fake_edit_llm(notes, feedback, suspicious, **kw):
        return [replace(n, pitch_midi=n.pitch_midi + 2, pitch_hz=midi_to_hz(n.pitch_midi + 2)) for n in notes], ""

    monkeypatch.setattr("tsov.analysis.edit._call_edit_llm", fake_edit_llm)

    fake = _FakeChat([
        _tool_resp("load_score", {"path": str(score_path)}, "c1"),
        _tool_resp("edit_score", {"score_path": str(score_path), "feedback": "整体高 2 半音",
                                  "output": str(edited_path)}, "c2"),
        _tool_resp("render_wav", {"score_path": str(edited_path), "out_wav": str(wav_path)}, "c3"),
        _answer_resp("完成：改谱并渲染了。"),
    ])
    monkeypatch.setattr("tsov.agent.loop.chat", fake)

    result = AgentLoop(max_turns=8, session_dir=str(ws / "sessions")).run("改谱并渲染")
    assert result.answer == "完成：改谱并渲染了。"
    assert result.turns == 4 and result.tool_calls_made == 3
    assert Path(result.session_path).exists()
    jsonl = Path(result.session_path).read_text(encoding="utf-8").strip().splitlines()
    assert len(jsonl) == 1 + 1 + 3 * 2 + 1  # system + user + (assistant+tool)*3 + final assistant

    edited = Score.from_dict(json.loads(edited_path.read_text(encoding="utf-8")))
    assert [n.pitch_midi for n in edited.tracks[0].notes] == [62, 64, 66]

    if _SOUNDFONT:
        import soundfile as sf

        audio, sr = sf.read(str(wav_path), dtype="float32")
        assert len(audio) > 0 and float(np.abs(audio).max()) > 0.01


def test_tool_error_returned_as_observation(ws, monkeypatch):
    fake = _FakeChat([
        _tool_resp("load_score", {"path": str(ws / "不存在.json")}, "c1"),
        _answer_resp("文件不存在，请检查路径。"),
    ])
    monkeypatch.setattr("tsov.agent.loop.chat", fake)
    result = AgentLoop(max_turns=4, session_dir=str(ws / "s")).run("读文件")
    assert "文件不存在" in result.answer
    assert any("错误" in n for n in result.notes)  # 工具异常被捕获成观测回喂


def test_max_turns_bail_with_session(ws, monkeypatch):
    fake = _FakeChat([_tool_resp("list_dir", {"path": str(ws)}, f"c{i}") for i in range(6)])
    monkeypatch.setattr("tsov.agent.loop.chat", fake)
    result = AgentLoop(max_turns=3, session_dir=str(ws / "s")).run("循环任务")
    assert "已达最大轮次" in result.answer
    assert result.tool_calls_made == 3
    assert Path(result.session_path).exists()
