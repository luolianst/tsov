"""tsov.dsp.preprocess 单元测试（M1）：转码/降噪的纯逻辑部分（时长探测、批处理 manifest）。"""

import json
import subprocess

import pytest


def test_probe_duration_returns_float(sample_wav_16000):
    from tsov.dsp.preprocess import probe_duration

    dur = probe_duration(sample_wav_16000)
    assert isinstance(dur, float)
    assert dur > 0


@pytest.fixture
def sample_wav_16000(tmp_path):
    """生成 1s 440Hz 16k 单声道 wav（纯 python 生成，不依赖 ffmpeg）。"""
    import numpy as np
    import soundfile as sf

    sr = 16000
    t = np.arange(int(sr * 1.0)) / sr
    audio = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    path = tmp_path / "tone.wav"
    sf.write(str(path), audio, sr, subtype="PCM_16")
    return path


def test_preprocess_batch_manifest(tmp_path, sample_wav_16000):
    from tsov.dsp import preprocess_batch

    results = preprocess_batch([sample_wav_16000], tmp_path / "out")
    assert len(results) == 1
    manifest_path = tmp_path / "out" / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["sr"] == 16000
    assert len(manifest["files"]) == 1
