"""agent LLM 调用单元测试（ADR-0012）：原生 tool_calls 解析 + JSON 决策兜底。"""

import tsov.agent.llm as al
import tsov.llm_client as lc


class _FakeResp:
    def __init__(self, message):
        self._message = message

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": self._message}]}


def _patch_post(monkeypatch, message):
    # F4：HTTP POST 已收口 llm_client——打桩目标随之迁移（语义等价）
    monkeypatch.setattr(lc.requests, "post", lambda *a, **k: _FakeResp(message))


def test_native_tool_calls_parsed(monkeypatch):
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "k")
    _patch_post(
        monkeypatch,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "a1", "type": "function",
                 "function": {"name": "load_score", "arguments": '{"path": "x.json"}'}}
            ],
        },
    )
    out = al.chat([{"role": "user", "content": "x"}], tools=[{"type": "function", "function": {}}])
    assert len(out["tool_calls"]) == 1
    assert out["tool_calls"][0]["name"] == "load_score"
    assert out["tool_calls"][0]["arguments"] == {"path": "x.json"}
    assert out["content"] == ""


def test_json_decision_fallback_synthesizes_tool(monkeypatch):
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "k")
    _patch_post(
        monkeypatch,
        {"role": "assistant", "content":
            '{"tool": "list_dir", "arguments": {"path": "output"}}'},
    )
    out = al.chat([{"role": "user", "content": "x"}])
    assert len(out["tool_calls"]) == 1
    assert out["tool_calls"][0]["name"] == "list_dir"
    assert out["tool_calls"][0]["arguments"] == {"path": "output"}
    # 合成 message 供会话存档（wire 格式，arguments 为 JSON 字符串）
    assert out["message"]["tool_calls"][0]["function"]["arguments"] == '{"path": "output"}'


def test_json_answer_wrapper_unwrapped(monkeypatch):
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "k")
    _patch_post(monkeypatch, {"role": "assistant", "content": '{"answer": "已完成"}'})
    out = al.chat([{"role": "user", "content": "x"}])
    assert out["tool_calls"] == []
    assert out["content"] == "已完成"


def test_plain_text_is_final_answer(monkeypatch):
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "k")
    _patch_post(monkeypatch, {"role": "assistant", "content": "好的，直接作答。"})
    out = al.chat([{"role": "user", "content": "x"}])
    assert out["tool_calls"] == []
    assert out["content"] == "好的，直接作答。"
