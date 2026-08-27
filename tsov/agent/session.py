"""AgentSession：append-only JSONL 会话（ADR-0012）。

- 会话消息存 `output/agent-sessions/<session_id>.jsonl`（与 ADR-0005 落盘镜像同一哲学，可复现）
- 消息按 OpenAI wire 格式记录（role/content/tool_calls/tool_call_id），`api_messages` 供直接回喂 LLM
"""

from __future__ import annotations

import datetime
import json
import uuid
from pathlib import Path

_WIRE_KEYS = ("role", "content", "tool_calls", "tool_call_id")


class AgentSession:
    def __init__(
        self,
        task: str = "",
        session_dir: str | Path = "output/agent-sessions",
        session_id: str | None = None,
    ):
        if session_id is None:
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            session_id = f"{stamp}-{uuid.uuid4().hex[:8]}"
        self.session_id = session_id
        self.dir = Path(session_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{session_id}.jsonl"
        self.task = task
        self.messages: list[dict] = []

    def add(self, role: str, content: str = "", tool_calls=None, tool_call_id: str | None = None) -> dict:
        """追加一条消息（同时写 JSONL）。"""
        entry: dict = {"role": role, "content": content}
        if tool_calls is not None:
            entry["tool_calls"] = tool_calls
        if tool_call_id is not None:
            entry["tool_call_id"] = tool_call_id
        entry["ts"] = datetime.datetime.now().isoformat(timespec="seconds")
        self.messages.append(entry)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    @property
    def api_messages(self) -> list[dict]:
        """给 LLM 的会话（剥掉记录字段 ts）。"""
        return [{k: m[k] for k in _WIRE_KEYS if k in m} for m in self.messages]
