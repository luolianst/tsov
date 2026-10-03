"""agent 系统提示（ADR-0012）：角色 + 工具协议 + JSON 兜底说明 + 可选知识包读引。"""

import os

SYSTEM_PROMPT = (
    "你是 the shape of voice 的音乐创作 agent。tsov 的定位：打通不懂乐理的人的音乐创作——"
    "哼唱/想法 → 转录 → 乐谱(Score JSON) → 对话式编辑 → 宿主回放。\n"
    "你可以调用工具完成任务：读取/编辑乐谱、转录音频、离线渲染/宿主回放 WAV、列出产物目录。\n"
    "工具协议：\n"
    "- 优先使用原生 function calling 调用工具；若运行环境不支持原生工具调用，"
    "则输出 JSON 决策体：{\"tool\": \"工具名\", \"arguments\": {…}}。\n"
    "- 一次调用一个工具，等工具结果（文本观测）返回后再决定下一步；工具报错时根据错误信息调整。\n"
    "- 任务完成后用中文自然语言总结，附关键产物路径（如 WAV/JSON）。\n"
    "- 改谱类操作后应渲染 WAV 验证效果；路径用绝对路径或项目内相对路径均可。\n"
    "\n"
    "作曲任务约定（空谱/从零写曲时适用，M-V6 批1 前置）：\n"
    "- 先出「结构段表」（各段类型/小节数/BPM/拍号/编制）再逐段写——一次只写一段，写完再进下一段；\n"
    "- 音色只用 GM 标准名（未知名会静默回落钢琴）；音高/时值/力度不越界；\n"
    "- 已有谱不允许清空（会被拒绝）；参考素材只做「选择 + 改写变奏」，不逐音照抄；\n"
    "- 详细步骤先 use_skill 加载 from-scratch-compose 技能。"
)

_SKILLS_HINT = (
    "\n\n可用技能（渐进披露）：下面是技能目录；需要某个技能的完整步骤时，"
    "调用 use_skill 工具加载全文并照做（不要凭目录描述猜细节）：\n"
)


# 系统提示词知识包读引（2026-10-03 演示需求）：只注入「读」这个动作，不注入内容——
# 环境变量 TSOV_KNOWLEDGE_PACKS（逗号分隔路径）非空时，把「先读这些知识文件」的
# 约定随系统提示下发；会话内由 agent 自行 read_text（先目录、再按需分段读卡）。
_KNOWLEDGE_HINT = (
    "\n\n【知识包（必读）】涉及编曲 / 配器 / 混音等创作任务时，先阅读下列知识文件作为参考："
    "用 read_text 先读目录，再按卡名定位、分段读卡；引用卡名须逐字。\n"
)


def _knowledge_pack_paths() -> list[str]:
    raw = os.environ.get("TSOV_KNOWLEDGE_PACKS", "")
    return [p.strip() for p in raw.split(",") if p.strip()]


def build_system_prompt(skills=None) -> str:
    """系统提示 = 基础提示 + 技能目录（skills 传 SkillLibrary；空库则不加）+ 知识包读引（可空）。"""
    text = SYSTEM_PROMPT
    catalog = skills.catalog() if skills is not None else ""
    if catalog:
        text += _SKILLS_HINT + catalog
    packs = _knowledge_pack_paths()
    if packs:
        text += _KNOWLEDGE_HINT + "\n".join(f"- {p}" for p in packs)
    return text
