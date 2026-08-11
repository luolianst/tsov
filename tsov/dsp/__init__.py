"""tsov.dsp —— 音频预处理 + DSP 转录（哼唱 → 结构化声部对象）。

M1 已实现：preprocess（转码 16k wav + noisereduce 降噪）。
M2 填充：transcribe / quantize / segment（crepe_notes vs basic-pitch 双验证）。
"""

from .preprocess import (
    PreprocessResult,
    denoise_wav,
    preprocess_audio,
    preprocess_batch,
    transcode_to_wav,
)
from .quantize import quantize_notes
from .segment import split_into_segments
from .transcribe import BACKENDS, transcribe

__all__ = [
    "PreprocessResult",
    "transcode_to_wav",
    "denoise_wav",
    "preprocess_audio",
    "preprocess_batch",
    "transcribe",
    "quantize_notes",
    "split_into_segments",
    "BACKENDS",
]
