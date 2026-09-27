"""工程辅助（F5：自 tsov/web.py 拆出；state schema docs/05 §五）。"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from ..core.score import Score
from ..host import Project


def project_state(proj: Project) -> dict:
    return {
        "project": proj.name,
        "score": proj.score.to_dict(),
        "selection": {"track": 0, "indices": []},  # 选择集是前端所有，后端只回默认值
        "history": {"can_undo": proj.can_undo, "can_redo": proj.can_redo},
        "git_log": proj.log(20),
        "summary": proj.summary(),
        # M-V8 E6：总时长（含音频 clip 尾 +1s 释放；前端 fit/时长显示用——音频轨一等公民）
        "duration": score_duration(proj.score, proj.root),
    }


def score_duration(score: Score, root=None) -> float:
    """工程时长（含 1s 释放尾）：音符尾 + 音频轨 clip 尾。

    音频部分读文件头（soundfile.info）；文件缺失/未给 root 时忽略（渲染/加载时会报错）。
    """
    ends = [n.end for t in score.tracks for n in t.notes]
    if root is not None and score.tracks:
        import soundfile as sf

        base = Path(root)
        for t in score.tracks:
            if str(getattr(t, "kind", "midi") or "midi") != "audio":
                continue
            for cl in t.audio_clips():  # E6：多 clip；src_len=null → 到文件尾
                rel = str(cl.get("file") or "")
                if not rel:
                    continue
                try:
                    info = sf.info(str(base / rel))
                    dur = float(info.frames) / float(info.samplerate)
                    span = dur - float(cl.get("src_offset") or 0.0)
                    if cl.get("src_len") is not None:
                        span = min(float(cl["src_len"]), span)
                    ends.append(
                        float(cl.get("start") or 0.0)
                        + max(0.0, span) * float(cl.get("stretch") or 1.0)
                    )
                except Exception:  # noqa: BLE001 —— 文件缺失/损坏：时长忽略（load 时报错）
                    continue
    return round(max(ends) + 1.0, 3) if ends else 1.0  # 含 1s 释放尾（与 mix_graph 一致）


def _resolve_project_audio(proj, rel: str) -> Path:
    """工程内音频相对路径解析（E2 护栏：须在 <工程根>/audio/ 内，防穿越）。"""
    if not rel:
        raise HTTPException(400, "缺 file 参数")
    p = Path(str(rel))
    if p.is_absolute() or ".." in p.parts:
        raise HTTPException(400, f"非法音频路径：{rel!r}")
    root = proj.root.resolve()
    full = (proj.root / p).resolve()
    audio_dir = (root / "audio").resolve()
    if full != audio_dir and audio_dir not in full.parents:
        raise HTTPException(400, f"音频文件必须在工程 audio/ 内：{rel!r}")
    if not full.is_file():
        raise HTTPException(404, f"音频文件不存在：{rel!r}")
    return full


def _score_from_dict(data: dict) -> Score:
    try:
        return Score.from_dict(data)
    except Exception as e:  # noqa: BLE001 schema 不合法 → 400
        raise HTTPException(400, f"Score JSON 不合法：{type(e).__name__}: {e}") from e
