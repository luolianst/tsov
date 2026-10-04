"""处理链包（M-V8 E3 段2 · ADR-0017）。

- tools.py：工具注册表（5 件：denoise / loudnorm / transcribe / quantize / snap_scale）
  —— 每件可单独调用；链 = 预设里的工具序列 + 参数（presets/chains/*.json）
- runner.py：链运行器（步骤状态机 / 后台线程 / 取消检查点 / 产物落盘 / chain.json 持久化）
- apply.py：链产物 → 工程装配（段3「双轨进工程」；只产命令 dict，执行交宿主层）

设计要点见 开发任务书 §11.1 A；运行模型见 E3 设计讨论 Q1/Q2/Q7。
"""

from .apply import build_apply_commands, load_run_notes
from .runner import (ChainRunner, apply_config, auto_run_plan,
                     load_chain_json, load_preset, save_chain_json)
from .tools import ChainCancelled, ChainContext, TOOLS, get_tool, list_tools

__all__ = [
    "ChainCancelled",
    "ChainContext",
    "ChainRunner",
    "TOOLS",
    "apply_config",
    "auto_run_plan",
    "build_apply_commands",
    "get_tool",
    "list_tools",
    "load_chain_json",
    "load_preset",
    "load_run_notes",
    "save_chain_json",
]
