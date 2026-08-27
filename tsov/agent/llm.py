"""agent LLM 调用（ADR-0012）：OpenAI 兼容 chat + 工具调用。

- 端点/模型/key：沿用 `TSOV_LLM_ENDPOINT` / `TSOV_LLM_MODEL` / `OPENCODE_GO_API_KEY`（与 analysis/llm.py 同款）
- **原生 function calling 优先**；内容 JSON 决策兜底（`{"tool","arguments"}` / `{"answer"}`），
  兼容网关不支持 tools 参数/返回纯文本的场景
- 失败抛 RuntimeError（loop 层终止并给出可读错误，不静默）
"""

from __future__ import annotations

import json
import os
import re

import requests

LLM_ENDPOINT = os.environ.get("TSOV_LLM_ENDPOINT", "https://opencode.ai/zen/go/v1/chat/completions")
LLM_MODEL = os.environ.get("TSOV_LLM_MODEL", "deepseek-v4-flash")
LLM_TIMEOUT_SEC = 240.0


def _parse_args(raw: str) -> dict:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _json_decision(content: str) -> dict:
    """内容里若有 JSON 决策体 {"tool","arguments"} / {"answer"} → 返回；否则返回 {}。"""
    text = (content or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return {}
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {}
    if isinstance(data, dict) and ("tool" in data or "answer" in data):
        return data
    return {}


def chat(messages: list[dict], tools: list[dict] | None = None, **params) -> dict:
    """一次 LLM 调用 → {"content", "tool_calls", "message"}。

    - tool_calls: [{"id","name","arguments":dict}]（已解析参数；可能为空 = 最终回答）
    - message: 原始助手消息（wire 格式，可原样存入会话）
    """
    api_key = params.get("api_key") or os.environ.get("OPENCODE_GO_API_KEY", "")
    if not api_key:
        raise RuntimeError("缺少 OPENCODE_GO_API_KEY（agent LLM 跳过）")
    endpoint = params.get("endpoint") or LLM_ENDPOINT
    model = params.get("model") or LLM_MODEL
    timeout = float(params.get("timeout", LLM_TIMEOUT_SEC))

    payload: dict = {"model": model, "messages": messages, "temperature": 0.2}
    if tools:
        payload["tools"] = tools
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    try:
        resp = requests.post(endpoint, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
        message = body["choices"][0]["message"]
    except Exception as e:  # noqa: BLE001 网络/协议失败 → 明确报错
        raise RuntimeError(f"agent LLM 调用失败：{type(e).__name__}: {e}") from e

    content = message.get("content") or message.get("reasoning_content") or ""
    tool_calls: list[dict] = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        tool_calls.append(
            {
                "id": tc.get("id") or f"call-{len(tool_calls) + 1}",
                "name": fn.get("name", ""),
                "arguments": _parse_args(fn.get("arguments") or "{}"),
            }
        )
    raw = dict(message)

    if not tool_calls:
        decision = _json_decision(content)
        if decision.get("tool"):
            name = str(decision["tool"])
            args = decision.get("arguments") if isinstance(decision.get("arguments"), dict) else {}
            tool_calls.append({"id": "json-1", "name": name, "arguments": args})
            raw = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "json-1",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
                    }
                ],
            }
        elif decision.get("answer") is not None:
            content = str(decision["answer"])
            raw = {"role": "assistant", "content": content}

    return {"content": content, "tool_calls": tool_calls, "message": raw}
