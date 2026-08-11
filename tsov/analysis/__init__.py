"""tsov.analysis —— LLM 分析层（调性候选 / 疑似错音 / 分段建议 / 回放音色）。

双向拟合（ADR-0004）：原始层 → 语义层（规则填充，确定性，无 LLM 参与）→ LLM 只读分析。
M3 填充实现。
"""

from __future__ import annotations

from ..core.notes import Voice
from .dataset import build_semantic_dataset
from .llm import AnalysisResult, call_llm
from .prompt import build_prompt

__all__ = ["build_semantic_dataset", "build_prompt", "call_llm", "AnalysisResult", "analyze"]


def analyze(voice: Voice, llm: bool = True, **params) -> AnalysisResult:
    """Voice → AnalysisResult：规则语义层 +（可选）LLM 分析。

    - llm=True：调 LLM（失败自动降级，不阻塞）；llm=False：只出规则部分
    - params 透传给 call_llm（api_key / endpoint / model / timeout 等）
    """
    semantic = build_semantic_dataset(voice)
    result = AnalysisResult(
        llm_used=False,
        semantic_dataset=semantic,
        prompt_used="",
    )
    if not llm:
        return result

    llm_out = call_llm(semantic, **params)
    result.llm_used = not llm_out.get("error")
    result.error = llm_out.get("error", "")
    result.key_candidates = llm_out.get("key_candidates", [])
    result.suspicious_notes = llm_out.get("suspicious_notes", [])
    result.phrase_suggestions = llm_out.get("phrase_suggestions", [])
    result.playback_notes = llm_out.get("playback_notes", "")
    result.prompt_used = build_prompt(semantic)
    return result
