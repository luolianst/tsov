"""tsov.agent —— agentloop（ADR-0012）：独立最小闭环，不依赖 dsh/opencode。

- registry：工具注册表（JSON schema + 观测回喂，dsh 机制）
- loop：工具调用循环（pi 结构：上下文组装 → 模型调用 → 执行 → 观测 → 续循环）
- session：append-only JSONL 会话
- tools：tsov 领域工具链（load_score/edit_score/render_wav/play_score/transcribe/understand/list_dir/use_skill）
- prompt：系统提示（角色 + 工具协议 + JSON 兜底说明 + 技能目录）
- skills：skill 机制（SKILL.md 模式，渐进披露：目录常驻 / 正文按需加载）

入口：`tsov agent run "<任务>"`；LLM 端点/模型/key 走环境变量（TSOV_LLM_ENDPOINT/TSOV_LLM_MODEL/OPENCODE_GO_API_KEY）或 WebUI ⚙ 设置（设置档层）。
"""

from .llm import chat
from .loop import AgentLoop, AgentResult
from .registry import ToolRegistry, ToolSpec
from .session import AgentSession
from .skills import SkillLibrary
from .tools import build_default_registry

__all__ = [
    "AgentLoop",
    "AgentResult",
    "AgentSession",
    "SkillLibrary",
    "ToolRegistry",
    "ToolSpec",
    "build_default_registry",
    "chat",
]
