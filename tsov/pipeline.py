"""M3 闭环管线：哼唱 wav → 转录 → 语义层 → LLM 分析 → Score → MIDI → 回放 WAV。

进程内对象流 + 每阶段独立落盘镜像（ADR-0005 决策 2/19）：
stage-01-voice.json → stage-02-semantic.json → stage-03-analysis.json →
stage-04-score.json → song.mid → song.wav → run-summary.json
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .analysis import AnalysisResult, analyze, build_semantic_dataset
from .core.notes import Voice
from .core.score import Instrument, KeyCandidate, Score, Track
from .dsp import transcribe
from .midi.export import score_to_midi
from .render import render_midi


def build_score(voice: Voice, analysis: AnalysisResult, title: str = "", meta: dict | None = None) -> Score:
    """Voice + AnalysisResult → Score（单轨旋律，回放音色中性钢琴）。

    调性候选取 LLM 分析前 3 个（空则空表，不阻塞导出）。
    """
    key_candidates = []
    for k in analysis.key_candidates[:3]:
        if isinstance(k, dict) and k.get("key"):
            key_candidates.append(KeyCandidate(key=str(k["key"]), confidence=float(k.get("confidence", 0.5))))
    track = Track(
        name="melody",
        instrument=Instrument(backend="fluidsynth", program="piano", volume=0.8),
        notes=voice.notes,
    )
    return Score(
        title=title,
        tempo=voice.bpm,
        key_candidates=key_candidates,
        tracks=[track],
        meta=meta or {},
    )


def _write_json(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run_closed_loop(
    audio_path: str | Path,
    out_dir: str | Path,
    backend: str = "crepe_notes",
    soundfont: str | None = None,
    llm: bool = True,
    title: str = "",
    **params,
) -> dict:
    """一键跑完整 M3 闭环管线，返回 run-summary dict（含各阶段耗时与产物路径）。

    - llm=False：跳过 LLM 分析（离线可跑，smoke 测试用）
    - 每阶段产物独立落盘；任何阶段失败都落盘错误记录并抛出（不静默）
    """
    t_start = time.time()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stages: dict[str, dict] = {}

    def stage(name: str, fn):
        t0 = time.time()
        result = fn()
        stages[name] = {"elapsed_sec": round(time.time() - t0, 3), "status": "ok"}
        return result

    # 1. 转录
    voice = stage("transcribe", lambda: transcribe(str(audio_path), backend=backend))
    voice_path = _write_json(out_dir / "stage-01-voice.json", voice.to_dict())
    stages["transcribe"]["artifact"] = str(voice_path)

    # 2. 语义层（纯规则）
    semantic = stage("semantic", lambda: build_semantic_dataset(voice))
    semantic_path = _write_json(out_dir / "stage-02-semantic.json", semantic)
    stages["semantic"]["artifact"] = str(semantic_path)

    # 3. LLM 分析（失败自动降级，不阻塞）
    analysis = stage("analysis", lambda: analyze(voice, llm=llm, **params.get("llm", {})))
    analysis_dict = analysis.to_dict()
    if analysis.error:
        stages["analysis"]["status"] = "degraded"
        stages["analysis"]["error"] = analysis.error
    analysis_path = _write_json(out_dir / "stage-03-analysis.json", analysis_dict)
    stages["analysis"]["artifact"] = str(analysis_path)

    # 4. Score（构建 + 落盘镜像）
    score = stage(
        "score",
        lambda: build_score(
            voice, analysis, title=title or Path(audio_path).stem,
            meta={"source_audio": str(audio_path), "backend": backend},
        ),
    )
    score_path = _write_json(out_dir / "stage-04-score.json", score.to_dict())
    stages["score"]["artifact"] = str(score_path)

    # 5. MIDI 导出
    midi_path = out_dir / "song.mid"
    stage("midi", lambda: score_to_midi(score, str(midi_path)))
    stages["midi"]["artifact"] = str(midi_path)

    # 6. 渲染回放（fluidsynth → WAV）
    wav_path = out_dir / "song.wav"
    stage("render", lambda: render_midi(midi_path, wav_path, soundfont=soundfont))
    stages["render"]["artifact"] = str(wav_path)

    summary = {
        "run": "m3-closed-loop",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "input_audio": str(audio_path),
        "backend": backend,
        "llm_used": analysis.llm_used,
        "note_count": len(voice.notes),
        "analysis_error": analysis.error,
        "artifacts": {
            "voice": str(voice_path),
            "semantic": str(semantic_path),
            "analysis": str(analysis_path),
            "score": str(score_path),
            "midi": str(midi_path),
            "wav": str(wav_path),
        },
        "stages": stages,
        "total_elapsed_sec": round(time.time() - t_start, 3),
    }
    summary_path = _write_json(out_dir / "run-summary.json", summary)
    summary["artifacts"]["summary"] = str(summary_path)
    _write_json(out_dir / "run-summary.json", summary)
    return summary
