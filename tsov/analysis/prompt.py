"""分析提示词模板（ADR-0005 决策 5）。

固定提示词模板 + 语义层数据填充 + DSP 特征/局限说明段。M3 填充实现。
确定性模板：LLM 只读数据，不做加工。
"""

from __future__ import annotations

import json
from typing import Any

# 系统提示：限定角色与输出契约（只输出 JSON）
SYSTEM_PROMPT = (
    "你是 the shape of voice 项目的音乐分析助手。输入是一段哼唱经 DSP 转录后得到的"
    "语义层数据（音符序列、乐句分组、DSP 特征）。你的任务："
    "① 推断调性候选（key candidates，给出琴键写法如 'C major' / 'a minor' 及置信度）；"
    "② 标出疑似错音（索引 + 原因，依据音高突变/装饰音/偏离中心调式等）；"
    "③ 给出乐句/段落重组建议；"
    "④ 给出一句回放提示（音色/情绪/处理建议）。"
    "你只能输出一个 JSON 对象，不要输出任何解释文字、Markdown 代码块或前后缀。"
)

# DSP 局限说明段：教 LLM 理解原始层到语义层的降级信息
DSP_LIMITATIONS = """## DSP 局限说明（重要，分析时必须考虑）

- **置信度含义**：confidence 是帧级音高估计的平均置信度（CREPE 输出），不是"音符写对了"的概率。
  低置信度只说明这段音高估计不稳定（气音/低语/噪音），不代表真实旋律就错。
- **哼唱天然抖动**：真实哼唱的音分偏移（deviation_cents）普遍存在 ±20~50 音分，是表现力不是错误；
  只有显著偏离（>80 音分）或与上下文调式冲突才值得怀疑。
- **装饰音**：is_ornament=True 表示短音（可能为倚音/滑音尾），判定错音时不应把装饰音当主旋律错音。
- **多声部素材**：本管线锁定主声部，若素材含背景人声/乐器，音符序列可能混入非主旋律片段，请优先看
  置信度高、成乐句的片段。
- **BPM**：当前 bpm 默认 120，bpm_confidence=0 表示尚未做节拍估计，相对时值仅供参考节奏轮廓，
  不要据此刻板判定"节奏错误"。
- **乐句分组**：phrase_grouping 由规则（停顿/音高突变）生成，边界可能不精确，你可给出重组建议。
"""

# 输出 JSON schema 说明：喂给 LLM 的结构契约
OUTPUT_SCHEMA = """## 输出 JSON 结构（必须遵守）

{
  "key_candidates": [{"key": "C major", "confidence": 0.8}],
  "suspicious_notes": [{"index": 3, "reason": "..."}],
  "phrase_suggestions": ["..."],
  "playback_notes": "一句话回放建议"
}

要求：
- key_candidates 最多 3 个，按置信度降序；key 用琴键写法（大调大写首字母如 'C major'，小调小写如 'a minor'）。
- suspicious_notes 的 index 必须来自下方 notes 数组的 index 字段。
- phrase_suggestions 为字符串列表，每项描述一个乐句的调整建议。
- playback_notes 为单条字符串。
"""


def build_prompt(semantic_dataset: dict[str, Any], **params) -> str:
    """由语义层中间数据集生成完整用户提示词（含 DSP 特征与局限说明）。

    返回 user 消息正文；SYSTEM_PROMPT 单独作为 system 消息。
    """
    if semantic_dataset is None or not isinstance(semantic_dataset, dict):
        raise TypeError("semantic_dataset 必须是 dict（build_semantic_dataset 的输出）")
    data_json = json.dumps(semantic_dataset, ensure_ascii=False, indent=2)
    return (
        "以下是哼唱的语义层数据（JSON）：\n\n```json\n"
        + data_json
        + "\n```\n\n"
        + DSP_LIMITATIONS
        + "\n"
        + OUTPUT_SCHEMA
    )
