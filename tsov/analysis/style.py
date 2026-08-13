"""参考曲风格改谱闭环（M6）：MOSS 理解 → LLM 改谱。

"我想要 XXX 那种感觉" 场景的第一步闭环：
reference_audio → moss.understand(description/tags/lyrics) → 构造 style feedback → edit_score 改谱 → MIDI+回放。

MOSS 服务不可用 → 明确报错（"MOSS 服务未启动，先跑 serve_moss.py"），不静默。
"""

from __future__ import annotations

from typing import Any

from ..core.score import Score
from .edit import EditResult, edit_score
from .moss import DEFAULT_URL, understand as moss_understand


def build_style_feedback(understanding: dict, extra_feedback: str = "") -> str:
    """把 MOSS 理解输出构造成 edit 的 feedback 模板。

    - understanding: {description, tags, lyrics}
    - 返回完整 feedback 文本（含"保持轮廓节奏、调整音高/调式/情绪色彩"约束）
    """
    description = (understanding.get("description") or "").strip() or "（无描述）"
    tags = understanding.get("tags") or []
    tags_txt = "、".join(str(t) for t in tags) if tags else "（无标签）"
    lyrics = (understanding.get("lyrics") or "").strip()
    lyrics_txt = (lyrics[:100] + ("…" if len(lyrics) > 100 else "")) if lyrics else "（无）"
    extra = (extra_feedback or "").strip()
    parts = [
        f"把这段旋律改出参考曲的风格。参考曲理解：{description}",
        f"风格标签：{tags_txt}。",
        f"歌词片段（参考情绪）：{lyrics_txt}。",
        f"{extra}。" if extra else "",
        "保持旋律轮廓和节奏大致不变，主要调整音高/调式/情绪色彩。",
    ]
    return "\n".join(p for p in parts if p)


def style_transfer(
    score: Score,
    reference_audio: str,
    moss_url: str = DEFAULT_URL,
    extra_feedback: str = "",
    llm: bool = True,
    timeout: int = 320,
    **params,
) -> tuple[EditResult, dict]:
    """MOSS 理解参考曲 → 构造 style feedback → edit_score 改谱。

    返回 (EditResult, request_info)；request_info = {understanding, feedback}（供落盘 style-request.json）。
    MOSS 服务不可用（连接失败）→ 抛 RuntimeError（明确报错，不静默）。
    """
    try:
        understanding = moss_understand(reference_audio, url=moss_url, timeout=timeout)
    except Exception as e:  # noqa: BLE001 连接失败 → 明确报错
        raise RuntimeError(
            f"MOSS 服务不可用（{type(e).__name__}: {e}）。先启动："
            'cmd /c "set PYTHONUTF8=1&& D:\\tools\\MOSS-Music\\.venv\\Scripts\\python.exe '
            'D:\\tools\\MOSS-Music\\serve_moss.py > D:\\tools\\MOSS-Music\\serve_moss.log 2>&1"'
        ) from e
    if understanding.get("error"):
        raise RuntimeError(f"MOSS 理解失败：{understanding['error']}")

    feedback = build_style_feedback(understanding, extra_feedback)
    result = edit_score(score, feedback=feedback, llm=llm, **params)
    request_info = {"understanding": understanding, "feedback": feedback}
    return result, request_info
