"""agent LLM 调用（ADR-0012）：OpenAI 兼容 chat + 工具调用。

- 端点/模型/key：`tsov.llm_client`（F4 收口，2026-09-22；原经 analysis.llm 借用）
- **原生 function calling 优先**；内容 JSON 决策兜底（`{"tool","arguments"}` / `{"answer"}`），
  兼容网关不支持 tools 参数/返回纯文本的场景
- 失败抛 RuntimeError（loop 层终止并给出可读错误，不静默）
"""

from __future__ import annotations

import json

from ..llm_client import (
    LLM_TIMEOUT_SEC,
    LlmRequestError,
    chat_post_json,
    extract_json_object,
    resolve_api_key,
    resolve_endpoint,
    resolve_model,
)


def _parse_args(raw: str) -> dict:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _json_decision(content: str) -> dict:
    """内容里若有 JSON 决策体 {"tool","arguments"} / {"answer"} → 返回；否则返回 {}。"""
    data = extract_json_object(content)
    if "tool" in data or "answer" in data:
        return data
    return {}


def chat(messages: list[dict], tools: list[dict] | None = None, **params) -> dict:
    """一次 LLM 调用 → {"content", "tool_calls", "message"}。

    - tool_calls: [{"id","name","arguments":dict}]（已解析参数；可能为空 = 最终回答）
    - message: 原始助手消息（wire 格式，可原样存入会话）
    """
    api_key = resolve_api_key(**params)
    if not api_key:
        raise RuntimeError("缺少 LLM key（TSOV_LLM_API_KEY / DEEPSEEK_API_KEY / OPENCODE_GO_API_KEY，agent LLM 跳过）")
    endpoint = resolve_endpoint(**params)
    model = resolve_model(**params)
    timeout = float(params.get("timeout", LLM_TIMEOUT_SEC))

    payload: dict = {"model": model, "messages": messages, "temperature": 0.2}
    if tools:
        payload["tools"] = tools

    try:
        body = chat_post_json(payload, api_key=api_key, endpoint=endpoint, timeout=timeout)
        message = body["choices"][0]["message"]
    except LlmRequestError as e:  # noqa: BLE001 网络/协议失败 → 明确报错
        raise RuntimeError(f"agent LLM 调用失败：{e}") from e
    except Exception as e:  # noqa: BLE001 响应结构异常（如缺 choices）→ 明确报错
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
