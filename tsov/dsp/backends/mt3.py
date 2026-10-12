"""MT3 多器乐转录后端（mt3-infer · YourMT3 首选；批E 段2 E2-2/E2-3）。

- 输入：声部音频（stems/bass.wav、stems/other.wav…）；输出：该声部 Note 列表。
- 模型：YourMT3（8-stem 多任务，~15× 实时，536MB；mr_mt3 / mt3_pytorch 可换）。
  16k numpy 进 → mido MIDI 出 → pretty_midi 读回 → Note 口径（**允许复音/同刻叠音**）。
- 权重：vendor/mt3-checkpoints（`MT3_CHECKPOINT_DIR` 注入；预置走 fetch_engines --only mt3）；
  auto_download=True 且权重缺失时由 mt3-infer 自行下载（首次运行慢，链外可见进度由父进程心跳兜）。
- **不走 _build_voice**（那是单声部旋律口径；复音/多器乐需保留叠音）；重依赖全惰性导入。
"""

from __future__ import annotations

import os
from pathlib import Path

from ...core.notes import Note, Voice
from ...core.units import midi_to_hz
from . import TranscribeBackend

# 项目内权重目录（vendor/ 不入库，换机需 fetch_engines 重装）
_ROOT = Path(__file__).resolve().parents[3]
_CHECKPOINT_DIR = _ROOT / "vendor" / "mt3-checkpoints"
# yourmt3 权重相对路径（registry 路径去 `.mt3_checkpoints/` 前缀）
_YOURMT3_CKPT_REL = (Path("yourmt3")
                     / "mc13_256_g4_all_v7_mt3f_sqr_rms_moe_wf4_n8k2_silu_rope_rp_b36_nops"
                     / "last.ckpt")

# 默认参数（代码内默认值 + **params 覆盖，ADR-0005 决策 7）
DEFAULTS = {
    "model": "yourmt3",      # yourmt3（默认，8-stem）| mr_mt3 | mt3_pytorch
    "device": "auto",        # auto|cuda|cpu
    "min_note_ms": 40.0,     # 短于此的碎音剔除（0=不过滤）
    "keep_programs": None,   # None=全保留；或 GM program 号列表（如贝斯族 range(32,40)）
    "auto_download": True,   # 缺权重时由 mt3-infer 自动下载
}


def checkpoint_path(model: str = "yourmt3") -> Path | None:
    """本仓权重路径（yourmt3；其他模型 → None，交给 mt3-infer 自理）。"""
    if str(model) == "yourmt3":
        return _CHECKPOINT_DIR / _YOURMT3_CKPT_REL
    return None


def asset_status(model: str = "yourmt3") -> tuple[bool, str]:
    """环境就位检查（fetch_engines --check / 错误指路共用）。"""
    try:
        import mt3_infer  # noqa: F401
    except ImportError as e:
        return False, f"mt3-infer 未安装：{e}（uv pip install --no-deps mt3-infer + 手补依赖）"
    ckpt = checkpoint_path(model)
    if ckpt is not None and not ckpt.is_file():
        return False, (f"MT3 权重缺失：{ckpt}"
                       "（fetch_engines --only mt3 预置，或 auto_download=True 首次运行自动下载）")
    return True, ""


def midi_to_notes(midi, *, min_note_ms: float = 40.0, keep_programs=None) -> list[Note]:
    """mido.MidiFile → Note 列表（pretty_midi 读回；复音/同刻叠音保留；按 (start, pitch) 排序）。

    纯转换（除 pretty_midi 外无重依赖）——单测直击规则。
    """
    import io

    import pretty_midi

    buf = io.BytesIO()
    midi.save(file=buf)
    buf.seek(0)
    pm = pretty_midi.PrettyMIDI(buf)
    keep = None if keep_programs is None else {int(p) for p in keep_programs}
    out: list[Note] = []
    for inst in pm.instruments:
        if keep is not None and int(inst.program) not in keep:
            continue
        for n in inst.notes:
            if min_note_ms and (float(n.end) - float(n.start)) * 1000.0 < float(min_note_ms):
                continue
            out.append(Note(
                start=round(float(n.start), 6), end=round(float(n.end), 6),
                pitch_midi=int(n.pitch), pitch_hz=float(midi_to_hz(int(n.pitch))),
                velocity=max(0.0, min(1.0, float(n.velocity) / 127.0)),
                confidence=1.0, deviation_cents=0.0, is_ornament=False,
            ))
    out.sort(key=lambda x: (x.start, x.pitch_midi))
    return out


class Mt3Backend(TranscribeBackend):
    name = "mt3"

    def transcribe(self, audio_path: str, **params) -> Voice:
        cfg = {**DEFAULTS, **params}
        model = str(cfg.get("model") or DEFAULTS["model"])
        ok, why = asset_status(model)
        if not ok:
            if not cfg.get("auto_download", True):
                raise RuntimeError(why)
            if "权重缺失" not in why:  # 缺包/环境硬伤——即便 auto_download 也不放行
                raise RuntimeError(why)
        audio_path = Path(audio_path)
        if not audio_path.is_file():
            raise FileNotFoundError(f"音频不存在：{audio_path}")

        os.environ.setdefault("MT3_CHECKPOINT_DIR", str(_CHECKPOINT_DIR))

        import librosa
        import torch

        from mt3_infer import transcribe as mt3_transcribe

        device = str(cfg.get("device") or "auto")
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"

        y, _sr = librosa.load(str(audio_path), sr=16000, mono=True)
        midi = mt3_transcribe(y, model=model, sr=16000, device=device,
                              auto_download=bool(cfg.get("auto_download", True)))
        notes = midi_to_notes(midi, min_note_ms=float(cfg.get("min_note_ms") or 0.0),
                              keep_programs=cfg.get("keep_programs"))

        return Voice(notes=notes, bpm=120.0, bpm_confidence=0.0, segments=[],
                     source_audio=str(audio_path), backend="mt3")
