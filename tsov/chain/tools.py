"""处理链工具注册表（M-V8 E3 段2）。

工具 = {id · 名称 · 档位 tier · 参数 schema · run 契约}；每个工具可单独调用，
「链」只是 presets/chains/*.json 里的工具序列 + 参数（没有魔法）。ADR-0017：
链、REST、agent 走同一实现。

档位（Q2 定）：
- fast：毫秒~百毫秒级（DSP）——改参自动顺跑（无「应用」按钮）
- slow：秒级（如转录）——改参标脏，「应用」按钮显式触发

run 契约：run(ctx: ChainContext) -> dict —— 读 ctx.prev（上一步产物）/ctx.source_audio，
写 ctx.artifact(...)，返回统计（并入状态）。取消检查点：ctx.check_cancel()（步边界）。
"""

from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path


class ChainCancelled(Exception):
    """链被用户取消（步边界检查点抛出）。"""


@dataclass
class ChainContext:
    """工具执行上下文（runner 构造；工具唯一入口）。"""

    project_root: Path
    run_dir: Path
    source_audio: Path
    score_tempo: float
    step_id: str
    params: dict
    prev: Path | None = None          # 上一步产物（链式输入）
    cancel: threading.Event | None = None

    def artifact(self, name: str) -> Path:
        """本步产物路径（<project>/chain/<run-ts>/<name>）。"""
        return self.run_dir / name

    def check_cancel(self) -> None:
        """取消检查点（各步开始时、耗时子过程前后调用）。"""
        if self.cancel is not None and self.cancel.is_set():
            raise ChainCancelled("已取消")

    def log(self, msg: str) -> None:
        """进度消息（runner 挂接；工具可自由调用）。"""
        hook = getattr(self, "on_log", None)
        if callable(hook):
            try:
                hook(str(msg))
            except Exception:  # noqa: BLE001 日志不该弄崩工具
                pass


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _ffmpeg(args: list[str]) -> None:
    """ffmpeg 调用（错误带 stderr；与 dsp/preprocess 同口径）。"""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise RuntimeError(
            f"ffmpeg 不存在（PATH 与程序目录都找不到）：{e}。"
            f"若是开箱包：把 ffmpeg.exe/ffprobe.exe 放进 tsov 包的 ffmpeg\\ 文件夹后重启 tsov；"
            f"若是源码安装：完整 ffmpeg 发行包进 PATH。") from e
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败：{' '.join(cmd)}\n{(proc.stderr or '').strip()[:400]}")


def _load_notes(path: Path) -> tuple[list, float | None]:
    """读音符：voice.json（{notes,bpm,...}）或 notes 数组 json →（Note 列表, bpm）。"""
    from ..core.notes import Note

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return [Note.from_dict(n) for n in data.get("notes") or []], (float(data["bpm"]) if data.get("bpm") else None)
    return [Note.from_dict(n) for n in data], None


def _dump_notes(path: Path, notes: list) -> Path:
    Path(path).write_text(
        json.dumps([n.to_dict() for n in notes], ensure_ascii=False, indent=2), encoding="utf-8")
    return Path(path)


# ---------------------------------------------------------------------------
# 首批 5 件工具
# ---------------------------------------------------------------------------


def _run_denoise(ctx: ChainContext) -> dict:
    """① 降噪：源音频 → 16k 单声道转码（00）+ noisereduce 谱门降噪（01）。"""
    from ..dsp.preprocess import denoise_wav, probe_duration, transcode_to_wav

    ctx.check_cancel()
    src = ctx.source_audio
    wav16 = ctx.artifact("00-source-16k.wav")
    transcode_to_wav(src, wav16, sr=16000)
    ctx.check_cancel()
    strength = float(ctx.params.get("strength", 0.8))
    out = ctx.artifact("01-denoised.wav")
    denoise_wav(wav16, out, strength=strength)
    return {"artifact": out.name, "duration_sec": round(probe_duration(out), 3),
            "sample_rate": 16000, "strength": strength, "reference": wav16.name}


def _run_loudnorm(ctx: ChainContext) -> dict:
    """② 响度：ffmpeg loudnorm（EBU R128；目标 -16 LUFS / 峰值 -1 dB）。"""
    from ..dsp.preprocess import probe_duration

    ctx.check_cancel()
    src = ctx.prev or ctx.artifact("01-denoised.wav")
    target_lufs = float(ctx.params.get("target_lufs", -16.0))
    true_peak = float(ctx.params.get("true_peak_db", -1.0))
    out = ctx.artifact("02-loudnorm.wav")
    _ffmpeg(["-i", str(src),
             "-af", f"loudnorm=I={target_lufs}:TP={true_peak}:LRA=11",
             "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(out)])
    return {"artifact": out.name, "duration_sec": round(probe_duration(out), 3),
            "target_lufs": target_lufs, "true_peak_db": true_peak}


def _run_transcribe(ctx: ChainContext) -> dict:
    """③ 转录：wav → Voice（默认 RMVPE；jump/min_note/conf 可调）→ 03-voice.json。"""
    from ..dsp.transcribe import transcribe

    ctx.check_cancel()
    src = ctx.prev or ctx.artifact("02-loudnorm.wav")
    backend = str(ctx.params.get("backend", "rmvpe") or "rmvpe")
    kw: dict = {}
    if backend == "rmvpe":
        kw = {
            "confidence_threshold": float(ctx.params.get("confidence_threshold", 0.4)),
            "min_note_ms": float(ctx.params.get("min_note_ms", 60.0)),
            "pitch_jump_semitones": float(ctx.params.get("pitch_jump_semitones", 0.5)),
        }
    ctx.log(f"转录中（{backend}）——秒级步骤，请稍候")
    voice = transcribe(str(src), backend=backend, **kw)
    ctx.check_cancel()
    out = ctx.artifact("03-voice.json")
    out.write_text(json.dumps(voice.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return {"artifact": out.name, "notes": len(voice.notes), "bpm": float(voice.bpm),
            "backend": backend, "params": kw}


def _run_quantize(ctx: ChainContext) -> dict:
    """④ 量化：音符对齐网格（网格跟随工程/转录 BPM；strength=100% 全吸 / 50% 半拉）。"""
    from dataclasses import replace

    from ..dsp.quantize import quantize_notes

    ctx.check_cancel()
    src = ctx.prev or ctx.artifact("03-voice.json")
    notes, bpm = _load_notes(src)
    grid = int(ctx.params.get("grid", 16))
    strength = max(0.0, min(1.0, float(ctx.params.get("strength", 1.0))))
    use_bpm = float(bpm or ctx.score_tempo or 120.0)
    q = quantize_notes(notes, bpm=use_bpm, grid=grid)
    if strength < 1.0:  # 半拉：在原位置与量化点之间插值（保时值轮廓）
        q = [replace(a, start=round(a0.start + (a.start - a0.start) * strength, 6),
                     end=round(a0.end + (a.end - a0.end) * strength, 6))
             for a0, a in zip(notes, q)]
    out = _dump_notes(ctx.artifact("04-notes-quantized.json"), q)
    return {"artifact": out.name, "notes": len(q), "grid": grid, "strength": strength, "bpm": use_bpm}


def _run_snap_scale(ctx: ChainContext) -> dict:
    """⑤ 调内吸附：调外且 |dev|≥ 阈值的音吸到最近调内音 → 05-notes-snapped.json。"""
    from ..core.snap import snap_out_of_key

    ctx.check_cancel()
    src = ctx.prev or ctx.artifact("04-notes-quantized.json")
    notes, _bpm = _load_notes(src)
    raw_key = str(ctx.params.get("key", "auto") or "auto")
    key = None if raw_key.lower() in ("auto", "", "none") else raw_key
    threshold = float(ctx.params.get("threshold_cents", 42.0))
    snapped, stats = snap_out_of_key(notes, key=key, threshold_cents=threshold)
    out = _dump_notes(ctx.artifact("05-notes-snapped.json"), snapped)
    return {"artifact": out.name, "notes": len(snapped), "snapped": stats["snapped"],
            "key": stats["key"], "threshold_cents": threshold}


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

TOOLS: dict[str, dict] = {
    "denoise": {
        "id": "denoise", "tier": "fast",
        "name": {"zh": "降噪", "en": "Denoise"},
        "desc": {"zh": "noisereduce 谱门降噪（含 16k 转码；强度越大降得越狠）",
                 "en": "noisereduce spectral gating (with 16k transcode)"},
        "params": {
            "strength": {"type": "number", "default": 0.8, "min": 0.0, "max": 1.0,
                         "step": 0.05, "label": {"zh": "强度", "en": "Strength"}},
        },
        "run": _run_denoise,
    },
    "loudnorm": {
        "id": "loudnorm", "tier": "fast",
        "name": {"zh": "响度", "en": "Loudness"},
        "desc": {"zh": "ffmpeg loudnorm（EBU R128 响度归一）",
                 "en": "ffmpeg loudnorm (EBU R128)"},
        "params": {
            "target_lufs": {"type": "number", "default": -16.0, "min": -30.0, "max": -8.0,
                            "step": 0.5, "label": {"zh": "目标响度 LUFS", "en": "Target LUFS"}},
            "true_peak_db": {"type": "number", "default": -1.0, "min": -6.0, "max": 0.0,
                             "step": 0.5, "label": {"zh": "峰值上限 dB", "en": "True peak dB"}},
        },
        "run": _run_loudnorm,
    },
    "transcribe": {
        "id": "transcribe", "tier": "slow",
        "name": {"zh": "转录", "en": "Transcribe"},
        "desc": {"zh": "音频 → 音符（默认 RMVPE；秒级步骤）",
                 "en": "Audio to notes (RMVPE default)"},
        "params": {
            "backend": {"type": "select", "default": "rmvpe", "options": ["rmvpe", "game"],
                        "label": {"zh": "后端", "en": "Backend"}},
            "pitch_jump_semitones": {"type": "number", "default": 0.5, "min": 0.2, "max": 1.5,
                                     "step": 0.1, "label": {"zh": "音高跳变阈值", "en": "Pitch jump"}},
            "min_note_ms": {"type": "number", "default": 60.0, "min": 30.0, "max": 300.0,
                            "step": 10.0, "label": {"zh": "最短音 ms", "en": "Min note ms"}},
            "confidence_threshold": {"type": "number", "default": 0.4, "min": 0.1, "max": 0.9,
                                     "step": 0.05, "label": {"zh": "置信度阈值", "en": "Confidence"}},
        },
        "run": _run_transcribe,
    },
    "quantize": {
        "id": "quantize", "tier": "fast",
        "name": {"zh": "量化", "en": "Quantize"},
        "desc": {"zh": "音符对齐节拍网格（网格跟随 BPM；强度=全吸/半拉）",
                 "en": "Quantize notes to beat grid"},
        "params": {
            "grid": {"type": "select", "default": 16,
                     "options": [4, 8, 16, 32,
                                 {"value": 3, "label": {"zh": "1/8 三连", "en": "1/8 triplet"}},
                                 {"value": 6, "label": {"zh": "1/16 三连", "en": "1/16 triplet"}}],
                     "label": {"zh": "网格", "en": "Grid"}},
            "strength": {"type": "number", "default": 1.0, "min": 0.0, "max": 1.0,
                         "step": 0.05, "label": {"zh": "强度", "en": "Strength"}},
        },
        "run": _run_quantize,
    },
    "snap_scale": {
        "id": "snap_scale", "tier": "fast",
        "name": {"zh": "调内吸附", "en": "Scale snap"},
        "desc": {"zh": "调外音吸附回调内（自动检测调性；微偏差才吸）",
                 "en": "Snap out-of-key notes back into scale"},
        "params": {
            "key": {"type": "text", "default": "auto",
                    "label": {"zh": "调性（auto=自动检测）", "en": "Key (auto)"}},
            "threshold_cents": {"type": "number", "default": 42.0, "min": 10.0, "max": 80.0,
                                "step": 1.0, "label": {"zh": "吸附阈值音分", "en": "Threshold cents"}},
        },
        "run": _run_snap_scale,
    },
}


def get_tool(tool_id: str) -> dict:
    """按 id 取工具定义；未知 id → KeyError。"""
    if tool_id not in TOOLS:
        raise KeyError(f"未知链工具：{tool_id!r}（可选：{', '.join(TOOLS)}）")
    return TOOLS[tool_id]


def list_tools() -> list[dict]:
    """工具清单（不含 run 函数；供 REST/前端渲染参数表单）。"""
    return [{k: v for k, v in t.items() if k != "run"} for t in TOOLS.values()]
