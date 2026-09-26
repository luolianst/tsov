"""M-V8 E4 段2 单测：粗频谱小工具 + 电平测量（纯数学口径）。

口径锚点（与 agent 工具 analyze_levels 同源）：
- 频段占比（6 段，和≈1）；谱心随频率单调；空/静音 → 全 0
- RMS / 峰值 dBFS 同式（0 → 地板 -120）；最响窗 = 非重叠窗取最大（附起点）
"""

from __future__ import annotations

import math

import numpy as np

from tsov.tune import measure, spectrum

from unit._tune_fixtures import SR, tone


# ---------------------------------------------------------------------------
# 频谱
# ---------------------------------------------------------------------------


def test_band_shares_low_tone_dominates_low_band():
    shares = spectrum.band_shares(tone(80), SR)
    assert shares[0] > 0.95
    assert abs(sum(shares) - 1.0) < 1e-6


def test_band_shares_high_tone_dominates_high_band():
    shares = spectrum.band_shares(tone(6000), SR)
    assert shares[4] > 0.9


def test_band_shares_mix_sums_one():
    mix = tone(200) + tone(2000) + tone(9000)
    shares = spectrum.band_shares(mix, SR)
    assert abs(sum(shares) - 1.0) < 0.01
    assert shares[1] > 0.2 and shares[3] > 0.2 and shares[4] > 0.2   # 中低/中高/高


def test_band_shares_empty_and_silence_and_tiny():
    assert spectrum.band_shares(np.zeros(0), SR) == [0.0] * 6
    assert spectrum.band_shares(np.zeros(10), SR) == [0.0] * 6
    assert spectrum.band_shares(np.zeros(SR), SR) == [0.0] * 6


def test_centroid_monotonic_and_silent():
    assert spectrum.spectral_centroid(tone(200), SR) < spectrum.spectral_centroid(tone(4000), SR)
    assert spectrum.spectral_centroid(np.zeros(SR), SR) == 0.0


def test_cosine_identical_orthogonal_and_shape_guard():
    a = spectrum.band_shares(tone(80), SR)
    b = spectrum.band_shares(tone(6000), SR)
    assert spectrum.cosine(a, a) == 1.0
    assert spectrum.cosine(a, b) < 0.1
    assert spectrum.cosine([], [1.0, 2.0]) == 0.0


def test_mono_stereo_average():
    st = np.stack([tone(300), tone(300)], axis=1)      # 帧×2
    m = spectrum.mono(st)
    assert m.ndim == 1
    assert abs(float(m[100]) - float(tone(300)[100])) < 1e-9


# ---------------------------------------------------------------------------
# 电平
# ---------------------------------------------------------------------------


def test_rms_and_peak_dbfs_formula():
    buf = np.full(1000, 0.5)
    want = 20 * math.log10(0.5)
    assert abs(measure.rms_dbfs(buf) - want) < 0.01
    assert abs(measure.peak_dbfs(buf) - want) < 0.01
    assert measure.rms_dbfs(np.zeros(0)) == measure.DB_FLOOR
    assert measure.peak_dbfs(np.array([])) == measure.DB_FLOOR


def test_loudest_window_nonoverlap_max_with_position():
    buf = np.concatenate([np.full(SR, 0.1), np.full(SR, 0.8)])   # 2s
    loud_db, at = measure.loudest_window(buf, SR, 0.5)
    assert at == 1.0
    assert loud_db > measure.rms_dbfs(np.full(SR, 0.1)) + 10


def test_section_rms_bounds_and_out_of_range():
    buf = np.concatenate([np.full(SR, 0.1), np.full(SR, 0.8)])
    lo = measure.section_rms_dbfs(buf, SR, 0.0, 1.0)
    hi = measure.section_rms_dbfs(buf, SR, 1.0, 2.0)
    assert hi > lo + 10
    assert measure.section_rms_dbfs(buf, SR, 5.0, 6.0) == measure.DB_FLOOR


def test_track_metrics_schema_and_silent_flag():
    m = measure.track_metrics(np.zeros(SR), SR)
    assert m["silent"] is True and m["rms_dbfs"] == measure.DB_FLOOR
    m2 = measure.track_metrics(tone(440, 1.0), SR)
    assert m2["silent"] is False
    assert m2["duration_sec"] == 1.0
    for k in ("rms_dbfs", "loudest_win_dbfs", "loudest_win_at", "peak_dbfs"):
        assert k in m2


def test_mix_peak_clipping_flag():
    _, clip = measure.mix_peak(np.full(100, 1.2))
    assert clip is True
    _, clip2 = measure.mix_peak(np.full(100, 0.5))
    assert clip2 is False
    assert measure.mix_peak(np.zeros(0)) == (measure.DB_FLOOR, False)
