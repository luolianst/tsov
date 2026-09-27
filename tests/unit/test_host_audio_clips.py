"""E6 段1 音频编辑刀单测——clip 模型迁移 / 命令层（set_audio_clips·split_audio_clip）/
渲染（切片·修剪·淡化·伸缩·交叉融合）。"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from tsov.core.score import Score, Track, normalize_audio_clips
from tsov.host.audio_source import AudioClipSource
from tsov.host.command import EditBatch
from tsov.host.mix import render_buses
from tsov.host.session import HostSession

SR = 8000


def _sig(seconds=0.5, sr=SR, freq=220.0):
    t = np.arange(int(round(seconds * sr))) / sr
    return (0.5 * np.sin(2 * math.pi * freq * t)).astype(np.float32)


def _write_wav(path: Path, sig, sr=SR):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), sig, sr, subtype="FLOAT")  # float32 无量化 → 样本级断言可逐位比


def _audio_track(clips=None, rel="audio/take.wav", offset=0.0, name="素材"):
    t = Track(name=name)
    t.kind = "audio"
    t.audio = {"clips": clips} if clips is not None else {"file": rel, "offset": offset}
    return t


def _clip(file="audio/take.wav", start=0.0, src_offset=0.0, src_len=None, stretch=1.0,
          fade_in=0.0, fade_out=0.0, clip_id=""):
    return {
        "clip_id": clip_id, "file": file, "start": start, "src_offset": src_offset,
        "src_len": src_len, "stretch": stretch, "fade_in": fade_in, "fade_out": fade_out,
    }


def _src_item(path: Path, start=0.0, src_offset=0.0, src_len=None, stretch=1.0,
              fade_in=0.0, fade_out=0.0):
    return {"path": path, "start": start, "src_offset": src_offset, "src_len": src_len,
            "stretch": stretch, "fade_in": fade_in, "fade_out": fade_out}


# ---------------------------------------------------------------------------
# clip 模型（core/score.py）
# ---------------------------------------------------------------------------


def test_normalize_legacy_migrates_to_single_clip():
    clips = normalize_audio_clips({"file": "audio/a.wav", "offset": 2.0})
    assert len(clips) == 1
    c = clips[0]
    assert c["file"] == "audio/a.wav" and c["start"] == 2.0
    assert c["src_offset"] == 0.0 and c["src_len"] is None and c["stretch"] == 1.0
    assert c["fade_in"] == 0.0 and c["fade_out"] == 0.0 and c["clip_id"]
    # 确定性 id：同输入 → 同 id（跨加载稳定）
    again = normalize_audio_clips({"file": "audio/a.wav", "offset": 2.0})[0]
    assert again["clip_id"] == c["clip_id"]


def test_normalize_clips_fills_defaults_and_skips_junk():
    clips = normalize_audio_clips(
        {"clips": [{"file": "audio/b.wav", "start": 1.0}, "junk", {"file": "audio/c.wav", "src_len": 2.5}]}
    )
    assert len(clips) == 2
    assert clips[0]["clip_id"] and clips[0]["stretch"] == 1.0 and clips[0]["src_len"] is None
    assert clips[1]["src_len"] == 2.5 and clips[1]["clip_id"]
    assert clips[0]["clip_id"] != clips[1]["clip_id"]


def test_normalize_empty_forms():
    assert normalize_audio_clips(None) == []
    assert normalize_audio_clips({}) == []
    assert normalize_audio_clips({"clips": []}) == []
    assert _audio_track(clips=[]).audio_clips() == []
    # Track 方法走同一迁移
    tr = _audio_track(offset=0.75)
    assert tr.audio_clips()[0]["start"] == 0.75


# ---------------------------------------------------------------------------
# 命令层：set_audio_clips
# ---------------------------------------------------------------------------


def _clip_score():
    return Score(title="t", tracks=[Track(name="melody"), _audio_track()])


def test_set_audio_clips_replaces_and_normalizes():
    new, res = EditBatch().add(
        "set_audio_clips", track=1,
        value={"clips": [
            _clip(start=1.0, src_offset=0.25, src_len=0.5, stretch=1.5, fade_in=0.05, fade_out=0.1),
            _clip(start=2.0),
        ]},
    ).apply(_clip_score())
    assert res.ok and res.applied == 1
    audio = new.tracks[1].audio
    assert set(audio) == {"clips"} and len(audio["clips"]) == 2
    c0, c1 = audio["clips"]
    assert c0["start"] == 1.0 and c0["src_offset"] == 0.25 and c0["src_len"] == 0.5
    assert c0["stretch"] == 1.5 and c0["fade_in"] == 0.05 and c0["fade_out"] == 0.1
    assert c0["clip_id"] and c1["clip_id"] and c0["clip_id"] != c1["clip_id"]


def test_set_audio_clips_clears_to_silent():
    new, res = EditBatch().add("set_audio_clips", track=1, value={"clips": []}).apply(_clip_score())
    assert res.ok and new.tracks[1].audio == {"clips": []}


@pytest.mark.parametrize(
    "value,pat",
    [
        ({"clips": [dict(_clip(), file="evil/x.wav")]}, "audio/ 内相对路径"),
        ({"clips": [dict(_clip(), file="")]}, "为空"),
        ({"clips": [dict(_clip(), start=-1)]}, "越界"),
        ({"clips": [dict(_clip(), stretch=2.5)]}, "越界"),
        ({"clips": [dict(_clip(), stretch=0.2)]}, "越界"),
        ({"clips": [dict(_clip(), src_len=0)]}, "越界"),
        ({"clips": [dict(_clip(), fade_in=-0.1)]}, "越界"),
        ({"clips": [dict(_clip(), clip_id="y" * 65)]}, "过长"),
        ({"clips": [dict(_clip(clip_id="x")), dict(_clip(clip_id="x"))]}, "重复"),
        ({"clips": ["nope"]}, "需为对象"),
        ({"clips": "nope"}, "value 需"),
        ("nope", "value 需"),
    ],
)
def test_set_audio_clips_guards(value, pat):
    _, res = EditBatch().add("set_audio_clips", track=1, value=value).apply(_clip_score())
    assert not res.ok and any(pat in e for e in res.errors), res.errors


def test_set_audio_clips_rejects_midi_track():
    _, res = EditBatch().add("set_audio_clips", track=0, value={"clips": []}).apply(_clip_score())
    assert not res.ok and any("只作用于音频轨" in e for e in res.errors)


def test_set_audio_track_on_clips_form_single():
    score = Score(title="t", tracks=[_audio_track(clips=[_clip(clip_id="k1", start=0.5, src_offset=0.1, src_len=0.4)])])
    new, res = EditBatch().add("set_audio_track", track=0, value={"offset": 1.2}).apply(score)
    assert res.ok
    c = new.tracks[0].audio["clips"][0]
    assert c["start"] == 1.2 and c["clip_id"] == "k1" and c["src_offset"] == 0.1 and c["src_len"] == 0.4


def test_set_audio_track_on_clips_form_multi_rejected():
    score = Score(title="t", tracks=[_audio_track(clips=[_clip(clip_id="a"), _clip(clip_id="b", start=1.0)])])
    _, res = EditBatch().add("set_audio_track", track=0, value={"offset": 1.0}).apply(score)
    assert not res.ok and any("多 clip" in e for e in res.errors)


# ---------------------------------------------------------------------------
# 命令层：split_audio_clip
# ---------------------------------------------------------------------------


def test_split_math_basic():
    score = Score(title="t", tracks=[_audio_track(clips=[_clip(clip_id="k1", src_offset=0.1)])])
    new, res = EditBatch().add(
        "split_audio_clip", track=0, value={"clip_id": "k1", "at": 0.3}
    ).apply(score)
    assert res.ok
    a, b = new.tracks[0].audio["clips"]
    assert a["clip_id"] == "k1" and a["src_len"] == pytest.approx(0.3) and a["fade_out"] == 0.0
    assert b["clip_id"] != "k1" and b["start"] == 0.3
    assert b["src_offset"] == pytest.approx(0.4) and b["src_len"] is None  # 到文件尾
    assert b["fade_in"] == 0.0


def test_split_with_stretch_and_fade_inheritance():
    clip = _clip(clip_id="k2", start=1.0, src_offset=0.0, src_len=0.4, stretch=2.0,
                 fade_in=0.05, fade_out=0.08)
    score = Score(title="t", tracks=[_audio_track(clips=[clip])])
    new, res = EditBatch().add(
        "split_audio_clip", track=0, value={"clip_id": "k2", "at": 1.5}
    ).apply(score)
    assert res.ok
    a, b = new.tracks[0].audio["clips"]
    assert a["src_len"] == pytest.approx(0.25) and a["fade_in"] == 0.05 and a["fade_out"] == 0.0
    assert b["start"] == 1.5 and b["src_offset"] == pytest.approx(0.25)
    assert b["src_len"] == pytest.approx(0.15)  # 剩余源区间收缩（0.4 − 0.25）
    assert b["fade_out"] == 0.08 and b["fade_in"] == 0.0 and b["stretch"] == 2.0


@pytest.mark.parametrize(
    "clips,value,pat",
    [
        ([_clip(clip_id="k1")], {"clip_id": "nope", "at": 0.1}, "未找到"),
        ([_clip(clip_id="k1", start=0.5)], {"clip_id": "k1", "at": 0.5}, "起点之后"),
        ([_clip(clip_id="k1", start=0.5, src_len=0.2)], {"clip_id": "k1", "at": 0.7}, "终点之前"),
        ([_clip(clip_id="k1")], {"clip_id": "k1", "at": "x"}, "非法"),
        ([_clip(clip_id="k1")], "nope", "value 需"),
    ],
)
def test_split_guards(clips, value, pat):
    score = Score(title="t", tracks=[_audio_track(clips=clips)])
    _, res = EditBatch().add("split_audio_clip", track=0, value=value).apply(score)
    assert not res.ok and any(pat in e for e in res.errors), res.errors


def test_split_rejects_midi_track():
    score = Score(title="t", tracks=[Track(name="melody")])
    _, res = EditBatch().add("split_audio_clip", track=0, value={"clip_id": "k", "at": 1.0}).apply(score)
    assert not res.ok and any("只作用于音频轨" in e for e in res.errors)


def test_split_empty_id_single_clip_fallback():
    # 单 clip（clips 形态）→ 空 id 指代唯一 clip（UI 便捷通道）
    score = Score(title="t", tracks=[_audio_track(clips=[_clip(clip_id="", src_len=0.4)])])
    new, res = EditBatch().add("split_audio_clip", track=0, value={"clip_id": "", "at": 0.2}).apply(score)
    assert res.ok
    a, b = new.tracks[0].audio["clips"]
    assert a["src_len"] == pytest.approx(0.2) and b["src_offset"] == pytest.approx(0.2)


def test_split_legacy_track_empty_id_migrates():
    # 旧形态 {file, offset}：空 id 切分 → 读时迁移 + 两段 clips 落盘
    score = Score(title="t", tracks=[_audio_track()])
    new, res = EditBatch().add("split_audio_clip", track=0, value={"clip_id": "", "at": 0.2}).apply(score)
    assert res.ok
    audio = new.tracks[0].audio
    assert "clips" in audio and len(audio["clips"]) == 2
    assert audio["clips"][0]["clip_id"] and audio["clips"][1]["start"] == 0.2


def test_split_empty_id_multi_clip_rejected():
    score = Score(title="t", tracks=[_audio_track(clips=[_clip(clip_id="a"), _clip(clip_id="b", start=1.0)])])
    _, res = EditBatch().add("split_audio_clip", track=0, value={"clip_id": "", "at": 0.1}).apply(score)
    assert not res.ok and any("须指定 clip_id" in e for e in res.errors)


# ---------------------------------------------------------------------------
# 渲染（AudioClipSource 多 clip）
# ---------------------------------------------------------------------------


def test_render_multi_clip_placement_and_trim(tmp_path):
    sig = _sig(0.5)
    wav = tmp_path / "audio" / "take.wav"
    _write_wav(wav, sig)
    src = AudioClipSource(clips=[
        _src_item(wav, start=0.1, src_len=0.2),
        _src_item(wav, start=0.5, src_offset=0.3),
    ], samplerate=SR)
    out = src.render([], SR, int(0.9 * SR))
    sig2 = np.stack([sig, sig], axis=1)   # mono 素材 → 双声道复制
    i0 = int(round(0.1 * SR))
    n = int(round(0.2 * SR))
    assert np.allclose(out[i0:i0 + n, :], sig2[:n], atol=1e-6)
    j0 = int(round(0.5 * SR))
    m = len(sig) - int(round(0.3 * SR))
    assert np.allclose(out[j0:j0 + m, :], sig2[-m:], atol=1e-6)
    assert float(np.abs(out[:i0]).max()) == 0.0 and float(np.abs(out[j0 + m:]).max()) == 0.0
    assert src.clip_end == pytest.approx(0.7)


def test_render_fades_linear(tmp_path):
    sig = _sig(0.4)
    wav = tmp_path / "audio" / "take.wav"
    _write_wav(wav, sig)
    src = AudioClipSource(clips=[_src_item(wav, fade_in=0.1, fade_out=0.1)], samplerate=SR)
    out = src.render([], SR, int(0.4 * SR))
    sig2 = np.stack([sig, sig], axis=1)
    n_in = int(round(0.1 * SR))
    ramp = np.linspace(0.0, 1.0, n_in, dtype=np.float32)
    assert np.allclose(out[:n_in], sig2[:n_in] * ramp[:, None], atol=1e-5)
    assert np.allclose(out[-n_in:], sig2[-n_in:] * ramp[::-1, None], atol=1e-5)
    assert np.allclose(out[n_in:-n_in], sig2[n_in:-n_in], atol=1e-6)
    assert abs(float(out[0, 0])) < 1e-6 and abs(float(out[-1, 0])) < 1e-6


def test_render_stretch_preserves_length_and_pitch(tmp_path):
    sig = _sig(0.5, freq=440.0)
    wav = tmp_path / "audio" / "take.wav"
    _write_wav(wav, sig)
    src = AudioClipSource(clips=[_src_item(wav, src_len=0.4, stretch=2.0)], samplerate=SR)
    out = src.render([], SR, int(1.2 * SR))
    expect = int(round(0.4 * SR * 2.0))  # 6400
    nz = np.nonzero(np.abs(out) > 1e-6)[0]
    assert nz.size > 0 and nz[0] == 0
    assert abs(int(nz[-1]) + 1 - expect) < 800  # 拉长约一倍（容 librosa 帧尾误差）
    seg = out[500:expect - 500, 0]
    spec = np.abs(np.fft.rfft(seg))
    freq = np.fft.rfftfreq(len(seg), 1.0 / SR)
    dom = float(freq[int(np.argmax(spec))])
    assert abs(dom - 440.0) < 25  # 保音高（线性兜底会掉到 ~220）


def test_render_overlap_crossfade_complementary(tmp_path):
    ones = np.ones(int(0.3 * SR), dtype=np.float32)
    wav = tmp_path / "audio" / "dc.wav"
    _write_wav(wav, ones)
    src = AudioClipSource(clips=[
        _src_item(wav, start=0.0),
        _src_item(wav, start=0.2),
    ], samplerate=SR)
    out = src.render([], SR, int(0.6 * SR))
    # 互补交叉 → 重叠区（0.2–0.3s）合计仍为 1.0；无交叉会是 2.0
    assert float(np.abs(out).max()) == pytest.approx(1.0, abs=1e-5)
    i0, i1 = int(round(0.2 * SR)), int(round(0.3 * SR))
    assert np.allclose(out[i0:i1], 1.0, atol=1e-5)
    assert np.allclose(out[:i0], 1.0, atol=1e-5)
    assert np.allclose(out[i1:int(round(0.5 * SR))], 1.0, atol=1e-5)


def test_render_empty_clips_silent(tmp_path):
    src = AudioClipSource(clips=[], samplerate=SR)
    out = src.render([], SR, 1000)
    assert out.shape == (1000, 2) and not out.any()
    assert src.clip_end == 0.0


def test_render_stereo_fidelity_preserved(tmp_path):
    """E6：立体声素材 L/R 各自保留（谱峰 220/330）——不经 mono 折叠。"""
    t = np.arange(int(0.5 * SR)) / SR
    sig = np.stack([0.5 * np.sin(2 * math.pi * 220.0 * t),
                    0.5 * np.sin(2 * math.pi * 330.0 * t)], axis=1).astype(np.float32)
    wav = tmp_path / "audio" / "st.wav"
    _write_wav(wav, sig)
    src = AudioClipSource(clips=[_src_item(wav)], samplerate=SR)
    out = src.render([], SR, int(0.5 * SR))
    assert out.shape == (int(0.5 * SR), 2) and not np.allclose(out[:, 0], out[:, 1])
    for ch, f0 in ((0, 220.0), (1, 330.0)):
        seg = out[100:-100, ch]
        dom = float(np.fft.rfftfreq(len(seg), 1.0 / SR)[int(np.argmax(np.abs(np.fft.rfft(seg))))])
        assert abs(dom - f0) < 15


def test_render_buses_keeps_stereo_image(tmp_path):
    """E6：经 mix 层（render_buses）音频轨 L/R 不折叠（谱峰 220/330 分列）。"""
    t = np.arange(int(0.5 * SR)) / SR
    sig = np.stack([0.5 * np.sin(2 * math.pi * 220.0 * t),
                    0.5 * np.sin(2 * math.pi * 330.0 * t)], axis=1).astype(np.float32)
    _write_wav(tmp_path / "audio" / "take.wav", sig)
    score = Score(title="t", tracks=[_audio_track(clips=[_clip()])])
    sess = HostSession.from_score(score, soundfont=str(tmp_path / "sf.fake"), samplerate=SR, base_dir=tmp_path)
    out = render_buses(sess, SR, auto_scale=False)
    assert out.ndim == 2 and out.shape[1] == 2 and not np.allclose(out[: len(sig), 0], out[: len(sig), 1])
    for ch, f0 in ((0, 220.0), (1, 330.0)):
        seg = out[100:len(sig) - 100, ch]
        dom = float(np.fft.rfftfreq(len(seg), 1.0 / SR)[int(np.argmax(np.abs(np.fft.rfft(seg))))])
        assert abs(dom - f0) < 15


# ---------------------------------------------------------------------------
# 会话集成
# ---------------------------------------------------------------------------


def test_session_clips_duration_and_legacy_equivalence(tmp_path):
    sig = _sig(0.4)
    _write_wav(tmp_path / "audio" / "take.wav", sig)

    legacy = Score(title="t", tracks=[_audio_track(offset=0.25)])
    sess = HostSession.from_score(
        legacy, soundfont=str(tmp_path / "sf.fake"), samplerate=SR, base_dir=tmp_path
    )
    src = sess.tracks[0].source
    assert isinstance(src, AudioClipSource) and src.offset == 0.25
    assert sess.duration == pytest.approx(0.65)

    clips = Score(title="t", tracks=[_audio_track(clips=[_clip(start=0.4, src_offset=0.1, src_len=0.3, stretch=2.0)])])
    sess2 = HostSession.from_score(
        clips, soundfont=str(tmp_path / "sf.fake"), samplerate=SR, base_dir=tmp_path
    )
    assert sess2.duration == pytest.approx(1.0)  # 0.4 + 0.3×2


def test_session_silent_empty_clips_ok(tmp_path):
    score = Score(title="t", tracks=[_audio_track(clips=[])])
    sess = HostSession.from_score(
        score, soundfont=str(tmp_path / "sf.fake"), samplerate=SR, base_dir=tmp_path
    )
    assert sess.tracks[0].source.render([], SR, 100).shape == (100, 2)
