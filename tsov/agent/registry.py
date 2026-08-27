"""工具注册表（ADR-0012）：JSON schema 工具 + 处理器 → OpenAI function calling 格式。

形态照搬 dsh 工具注册表（schema 化 + 观测回喂），执行层是 tsov 自己的领域工具（tools.py）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class ToolSpec:
    """一个 agent 可以调用的工具。handler 返回观测文本（回喂给 LLM）。"""

    name: str
    description: str
    parameters: dict  # JSON schema：{"type":"object","properties":{...},"required":[...]}
    handler: Callable[[dict], str]


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"工具已注册：{spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        if name not in self._tools:
            raise KeyError(f"未知工具：{name!r}（可选 {sorted(self._tools)}）")
        return self._tools[name]

    def specs(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def openai_tools(self) -> list[dict]:
        """OpenAI 兼容工具定义（供 chat.completions 的 tools 参数）。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": s.name,
                    "description": s.description,
                    "parameters": s.parameters,
                },
            }
            for s in self.specs()
        ]
