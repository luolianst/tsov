"""MCP prompts（批F F3-4）：内置技能 → prompts（「宿主即知识源」路线）。

外部 agent 连上即有 tsov 领域知识（渐进披露同构：先见目录，需要时取全文）。
"""

from __future__ import annotations

import mcp_types as types


def _library():
    from ..agent.skills import SkillLibrary

    return SkillLibrary()


def list_prompts() -> list[types.Prompt]:
    lib = _library()
    out: list[types.Prompt] = []
    for nm in lib.names():
        sk = lib.get(nm)
        out.append(types.Prompt(name=nm, description=(sk.description or "")[:220]))
    return out


def get_prompt(name: str) -> types.GetPromptResult:
    lib = _library()
    sk = lib.get(str(name))
    if sk is None:
        raise ValueError(f"未知提示词：{name}（可选：{', '.join(lib.names())}）")
    return types.GetPromptResult(
        description=sk.description,
        messages=[
            types.PromptMessage(
                role="user",
                content=types.TextContent(type="text", text=sk.load()),
            )
        ],
    )
