"""音频预处理：ffmpeg 转 16k 单声道 wav + noisereduce 降噪（FR-08 / ADR-0004）。

- 转码：ffmpeg → 16kHz 单声道 PCM16 wav（m4a/mp3 等 ffmpeg 可解码格式均可）
- 降噪：noisereduce spectral gating，批量自动跑，不需手动标噪音段
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np


@dataclass
class PreprocessResult:
    source: str  # 输入文件绝对/相对路径
    wav_path: str  # 转码后 16k wav
    denoised_path: str  # 降噪后 wav
    duration_sec: float  # 音频时长
    sample_rate: int = 16000
    noise_reduce_strength: float = 0.8
    elapsed_sec: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def _run_ffmpeg(args: list[str], retries: int = 3) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args]
    proc = _spawn(cmd, "ffmpeg", retries=retries)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败：{' '.join(cmd)}\n{proc.stderr}")


def _spawn(cmd: list[str], name: str, retries: int = 4):
    """subprocess 启动并重试（Windows 安全软件偶发锁进程：WinError 5 拒绝访问，等 30-60s 重试）。

    退避 10/20/30s 递增，总窗口 ~60s；仍失败才抛 RuntimeError。
    """
    for attempt in range(retries):
        try:
            return subprocess.run(cmd, capture_output=True, text=True)
        except PermissionError as e:
            if attempt < retries - 1:
                time.sleep(10 * (attempt + 1))
                continue
            raise RuntimeError(f"{name} 进程启动被拒（重试 {retries} 次后放弃）：{e}") from e
    raise RuntimeError(f"{name} 进程启动失败")  # pragma: no cover


def probe_duration(audio_path: str | Path) -> float:
    """用 ffprobe 取音频时长（秒）。"""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    proc = _spawn(cmd, "ffprobe")
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe 失败：{audio_path}\n{proc.stderr}")
    return float(proc.stdout.strip())


def transcode_to_wav(input_path: str | Path, output_path: str | Path, sr: int = 16000) -> Path:
    """m4a/mp3/任意 ffmpeg 可解码格式 → sr Hz 单声道 PCM16 wav。"""
    input_path = Path(input_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(["-i", str(input_path), "-ar", str(sr), "-ac", "1", "-c:a", "pcm_s16le", str(output_path)])
    return output_path


def denoise_wav(wav_path: str | Path, output_path: str | Path, strength: float = 0.8) -> Path:
    """noisereduce spectral gating 降噪。

    strength（prop_decrease）0-1：越大降得越狠；0.8 为保守默认，避免吃掉弱音。
    """
    import noisereduce as nr
    import soundfile as sf

    audio, sr = sf.read(str(wav_path), dtype="float32")
    if audio.ndim > 1:  # 防御：理论上已是单声道
        audio = audio.mean(axis=1)
    reduced = nr.reduce_noise(y=audio, sr=sr, stationary=True, prop_decrease=float(strength))
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(output_path), reduced, sr, subtype="PCM_16")
    return output_path


def preprocess_audio(
    input_path: str | Path,
    output_dir: str | Path,
    sr: int = 16000,
    noise_reduce_strength: float = 0.8,
) -> PreprocessResult:
    """单文件全流程：转码 → 降噪，产物写 output_dir/{16k,denoised}/。

    命名：保留输入文件名 stem；不同子目录避免互相覆盖。
    """
    t0 = time.time()
    input_path = Path(input_path)
    output_dir = Path(output_dir)
    stem = input_path.stem
    wav_path = output_dir / "16k" / f"{stem}.wav"
    denoised_path = output_dir / "denoised" / f"{stem}.wav"

    wav_path = transcode_to_wav(input_path, wav_path, sr=sr)
    duration_sec = probe_duration(input_path)
    denoised_path = denoise_wav(wav_path, denoised_path, strength=noise_reduce_strength)

    return PreprocessResult(
        source=str(input_path),
        wav_path=str(wav_path),
        denoised_path=str(denoised_path),
        duration_sec=round(duration_sec, 3),
        sample_rate=sr,
        noise_reduce_strength=noise_reduce_strength,
        elapsed_sec=round(time.time() - t0, 3),
    )


def preprocess_batch(
    input_paths: list[str | Path],
    output_dir: str | Path,
    sr: int = 16000,
    noise_reduce_strength: float = 0.8,
) -> list[PreprocessResult]:
    """批量全流程；返回结果列表并写 output_dir/manifest.json（复现元信息）。"""
    results = [preprocess_audio(p, output_dir, sr=sr, noise_reduce_strength=noise_reduce_strength) for p in input_paths]
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run": "m1-preprocess",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sr": sr,
        "noise_reduce_strength": noise_reduce_strength,
        "files": [r.to_dict() for r in results],
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return results
