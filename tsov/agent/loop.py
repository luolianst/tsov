"""AgentLoop（ADR-0012）：工具调用循环（移植 pi 的 loop 结构 + dsh 工具注册表机制）。

流程：system+user → 模型调用（原生 function calling / JSON 决策兜底）→ 执行工具 → 观测回喂 → 续循环；
无工具调用即视为最终回答。全程只依赖 stdlib + requests + tsov 库（不依赖 dsh/opencode）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .llm import chat
from .prompt import build_system_prompt
from .registry import ToolRegistry
from .session import AgentSession
from .skills import SkillLibrary


@dataclass
class AgentResult:
    answer: str  # 最终回答（或终止原因）
    turns: int  # 执行的模型调用轮数
    tool_calls_made: int  # 实际执行的工具调用次数
    session_path: str  # 会话 JSONL
    notes: list[str] = field(default_factory=list)  # 关键观测（工具结果摘要，便于人工核对）


class AgentLoop:
    def __init__(
        self,
        registry: ToolRegistry | None = None,
        max_turns: int = 12,
        session_dir: str = "output/agent-sessions",
        model: str | None = None,
        skills: SkillLibrary | None = None,
        **llm_params,
    ):
        self.skills = skills or SkillLibrary()
        if registry is None:
            from .tools import build_default_registry

            registry = build_default_registry(skills=self.skills)
        self.registry = registry
        self.max_turns = int(max_turns)
        self.session_dir = session_dir
        self.llm_params: dict = {**llm_params}
        if model:
            self.llm_params["model"] = model

    def run(self, task: str, session_id: str | None = None) -> AgentResult:
        """执行一次 agent 任务（单会话闭环）。"""
        session = AgentSession(task=task, session_dir=self.session_dir, session_id=session_id)
        session.add("system", build_system_prompt(self.skills))
        session.add("user", task)

        tool_count = 0
        observations: list[str] = []
        for turn in range(1, self.max_turns + 1):
            out = chat(session.api_messages, tools=self.registry.openai_tools(), **self.llm_params)
            if not out["tool_calls"]:
                session.add("assistant", out["content"])
                return AgentResult(
                    answer=out["content"],
                    turns=turn,
                    tool_calls_made=tool_count,
                    session_path=str(session.path),
                    notes=observations,
                )
            session.add("assistant", out["content"], tool_calls=out["message"].get("tool_calls"))
            for tc in out["tool_calls"]:
                name, args = tc["name"], tc["arguments"]
                try:
                    spec = self.registry.get(name)
                    observation = spec.handler(args)
                except Exception as e:  # noqa: BLE001 工具出错也作为观测回喂（LLM 可纠错）
                    observation = f"工具 {name} 错误: {type(e).__name__}: {e}"
                tool_count += 1
                observations.append(f"[{name}] {observation[:200]}")
                session.add("tool", observation, tool_call_id=tc["id"])

        return AgentResult(
            answer=f"已达最大轮次（{self.max_turns}）未形成最终回答；会话已落盘。",
            turns=self.max_turns,
            tool_calls_made=tool_count,
            session_path=str(session.path),
            notes=observations,
        )
