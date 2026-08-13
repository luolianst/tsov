"""tsov.dsp —— 音频预处理 + DSP 转录（音频 → 结构化声部对象）。

M1 已实现：preprocess（转码 16k wav + noisereduce 降噪）。
M2 填充：transcribe / quantize / segment（crepe_notes vs basic-pitch 双验证）。
M4 定案：转录层插件架构（backends/），game（GAME）为唯一后端；CREPE/basic-pitch 弃用。
"""

from .preprocess import (
    PreprocessResult,
    denoise_wav,
    preprocess_audio,
    preprocess_batch,
    transcode_to_wav,
)
from .backends import get_backend, list_backends
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
    "get_backend",
    "list_backends",
]
