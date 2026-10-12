"""ADTOF 鼓转录后端（xavriley/ADTOF-pytorch；批E 段2 E2-1/E2-3）。

- 输入：鼓声部音频（stems/drums.wav）；输出：打击乐 Note——GM 音高 5 类
  [35 kick, 38 snare, 47 tom, 42 hihat, 49 crash]，**同刻叠击合法**。
- 权重随 vendor 仓（`src/adtof_pytorch/data/*.pth`，~3.5MB）；**零 venv 依赖新增**
  （torch/librosa/pretty_midi/numpy 均既有）——subprocess 里挂 vendor src 直载，不装包。
- **不走 `_build_voice`**（那是单声部旋律的口径：重叠裁剪/乐句分割/IOI 估速都不适用于鼓）；
  自建 Voice：segments=[]、bpm=120.0 占位（打击乐无调性、无速度推断——速度以链首
  「定速与对齐」的 grid.json 为准）。
- 重依赖（torch / adtof_pytorch）全部函数内惰性导入：注册表导入要快，测试无 GPU 也可跑。
"""

from __future__ import annotations

import sys
from pathlib import Path

from ...core.notes import Note, Voice
from ...core.units import midi_to_hz
from . import TranscribeBackend

# 项目内 vendor 安装位置（vendor/ 不入库，换机需 fetch_engines 重装）
_ROOT = Path(__file__).resolve().parents[3]
_VENDOR_SRC = _ROOT / "vendor" / "ADTOF-pytorch" / "src"
_WEIGHTS_REL = Path("adtof_pytorch") / "data" / "adtof_frame_rnn_pytorch_weights.pth"

# 默认参数（代码内默认值 + **params 覆盖，ADR-0005 决策 7）
DEFAULTS = {
    "device": "auto",        # auto|cuda|cpu（auto=有 CUDA 就用 GPU）
    "thresholds": None,      # None → 随仓 FRAME_RNN_THRESHOLDS [0.22,0.24,0.32,0.22,0.30]
    "fps": 100,              # 模型激活帧率
    "weights": None,         # None → vendor 随仓权重；或显式 .pth 路径
    "velocity": 0.8,         # Note.velocity（0-1；ADTOF 无力度输出，固定值）
    "note_duration": 0.1,    # 秒（固定时值，后续由量化步对齐网格）
}


def asset_status() -> tuple[bool, str]:
    """vendor 资产就位检查（错误指路 / fetch_engines --check 共用）。"""
    if not _VENDOR_SRC.is_dir():
        return False, (f"ADTOF 未安装：{_VENDOR_SRC} 不存在"
                       "（fetch_engines --only adtof 或手动 clone ADTOF-pytorch 到 vendor/）")
    weights = _VENDOR_SRC / _WEIGHTS_REL
    if not weights.is_file():
        return False, f"ADTOF 权重缺失：{weights}（随仓件，检查 clone 完整性）"
    return True, ""


def peaks_to_notes(peaks: dict, *, velocity: float = 0.8,
                   note_duration: float = 0.1) -> list[Note]:
    """ADTOF 峰值字典 {gm_pitch: [t, ...]} → 打击乐 Note（同刻叠音保留；按 (start, pitch) 稳定排序）。

    纯函数（不碰 torch）——单测直击映射规则。
    """
    out: list[Note] = []
    for pitch, times in (peaks or {}).items():
        for t in times or []:
            t = float(t)
            out.append(Note(
                start=t, end=round(t + float(note_duration), 6),
                pitch_midi=int(pitch), pitch_hz=float(midi_to_hz(int(pitch))),
                velocity=float(velocity), confidence=1.0,
                deviation_cents=0.0, is_ornament=False,
            ))
    out.sort(key=lambda n: (n.start, n.pitch_midi))
    return out


class AdtofBackend(TranscribeBackend):
    name = "adtof"

    def transcribe(self, audio_path: str, **params) -> Voice:
        ok, why = asset_status()
        if not ok:
            raise RuntimeError(why)
        cfg = {**DEFAULTS, **params}
        audio_path = Path(audio_path)
        if not audio_path.is_file():
            raise FileNotFoundError(f"音频不存在：{audio_path}")

        # 惰性导入（重依赖；vendor src 挂 sys.path——不装包、零 venv 改动）
        import torch

        if str(_VENDOR_SRC) not in sys.path:
            sys.path.insert(0, str(_VENDOR_SRC))
        from adtof_pytorch import (  # type: ignore[import-not-found]
            FRAME_RNN_THRESHOLDS, LABELS_5, PeakPicker,
            calculate_n_bins, create_frame_rnn_model, get_default_weights_path,
            load_audio_for_model, load_pytorch_weights,
        )

        device = str(cfg.get("device") or "auto")
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"

        weights = Path(cfg["weights"]) if cfg.get("weights") else Path(
            get_default_weights_path() or (_VENDOR_SRC / _WEIGHTS_REL))
        if not weights.is_file():
            raise FileNotFoundError(f"ADTOF 权重不存在：{weights}")

        model = create_frame_rnn_model(calculate_n_bins())
        model.eval()
        model = load_pytorch_weights(model, str(weights), strict=False)
        model.to(device)

        x = load_audio_for_model(str(audio_path)).to(device)
        with torch.no_grad():
            pred = model(x).cpu().numpy()

        thresholds = cfg.get("thresholds") or FRAME_RNN_THRESHOLDS
        picker = PeakPicker(thresholds=thresholds, fps=int(cfg.get("fps") or 100))
        peaks = picker.pick(pred, labels=LABELS_5, label_offset=0)[0]
        notes = peaks_to_notes(peaks, velocity=float(cfg.get("velocity") or 0.8),
                               note_duration=float(cfg.get("note_duration") or 0.1))

        return Voice(notes=notes, bpm=120.0, bpm_confidence=0.0, segments=[],
                     source_audio=str(audio_path), backend="adtof")
