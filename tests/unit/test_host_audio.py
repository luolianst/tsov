"""M-V8 E2 音频轨（第一刀）单测：数据模型 / AudioClipSource / session 分派 / 渲染混入 / 导入 / 命令层 op。

约定：不用 pytest tmp_path（安全软件锁，handoff 坑 81）——output/<uuid> + rmtree_force；低采样率加速。
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from tsov.core.score import Instrument, Score, Track
from tsov.host import EditBatch, HostSession, HostTrack
from tsov.host.audio_source import AudioClipSource, resample_to
from tsov.host.instrument import SoundSource
from tsov.host.mix import render_buses
from tsov.host.project import Project

from unit._cleanup import rmtree_force

SR = 8000  # 低采样率加速（同 test_host_mix）
_HAS_FFMPEG = shutil.which("ffmpeg") is not None


def _sine(seconds=0.5, sr=SR, freq=440.0, amp=0.5):
    t = np.arange(int(seconds * sr)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _write_sine(path, seconds=0.5, sr=SR, freq=440.0, amp=0.5):
    sig = _sine(seconds, sr, freq, amp)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), sig, sr, subtype="FLOAT")  # float32 无量化 → 样本级断言可逐位比
    return sig


class _ConstSource(SoundSource):
    """常数信号源（忽略 notes；手算用）。"""

    def __init__(self, value: float = 0.0):
        self.value = float(value)

    def render(self, notes, samplerate, n_frames):
        return np.full((n_frames,), self.value, dtype=np.float32)


@pytest.fixture()
def wd():
    d = Path("output") / f"audiotest-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        rmtree_force(d)


# ---------------------------------------------------------------------------
# A：数据模型（additive）
# ---------------------------------------------------------------------------


def test_track_audio_fields_roundtrip():
    tr = Track(name="原曲", kind="audio", audio={"file": "audio/x-1a2b3c4d.flac", "offset": 1.25})
    d = tr.to_dict()
    assert d["kind"] == "audio" and d["audio"]["offset"] == 1.25
    tr2 = Track.from_dict(d)
    assert tr2.kind == "audio" and tr2.audio["file"].endswith(".flac")

    # 旧数据（无 kind/audio 字段）向后兼容 → midi/空
    legacy = {
        "name": "m",
        "instrument": {"backend": "fluidsynth", "program": "piano", "volume": 1.0, "effects": []},
        "notes": [],
    }
    tr3 = Track.from_dict(legacy)
    assert tr3.kind == "midi" and tr3.audio == {}


# ---------------------------------------------------------------------------
# C：AudioClipSource
# ---------------------------------------------------------------------------


def test_clip_source_places_samples(wd):
    sig = _write_sine(wd / "a.wav", seconds=0.5)
    src = AudioClipSource(wd / "a.wav", offset=1.0, samplerate=SR)
    out = src.render([], SR, int(2.0 * SR))
    i0, n = SR, len(sig)
    assert np.allclose(out[:i0], 0.0, atol=1e-7)
    assert np.allclose(out[i0 : i0 + n], sig, atol=1e-6)
    assert np.allclose(out[i0 + n :], 0.0, atol=1e-7)
    assert src.clip_end == pytest.approx(1.5, abs=1e-9)


def test_clip_source_offset_beyond_buffer(wd):
    _write_sine(wd / "a.wav", seconds=0.2)
    src = AudioClipSource(wd / "a.wav", offset=5.0, samplerate=SR)
    out = src.render([], SR, int(1.0 * SR))
    assert np.allclose(out, 0.0)


def test_clip_source_resample_path(wd):
    """48k 文件在 8k 目标下走兜底重采样：时长正确、能量在、结尾不拖尾。"""
    _write_sine(wd / "a48.wav", seconds=0.5, sr=48000)
    src = AudioClipSource(wd / "a48.wav", offset=0.0, samplerate=SR)
    out = src.render([], SR, int(1.0 * SR))
    expect = int(0.5 * SR)
    assert float(np.abs(out[:expect]).max()) > 0.2
    assert np.allclose(out[expect:], 0.0, atol=1e-6)


def test_clip_source_missing_file(wd):
    with pytest.raises(FileNotFoundError):
        AudioClipSource(wd / "nope.wav")


def test_resample_to_identity_and_length():
    a = _sine(0.1)
    assert resample_to(a, SR, SR) is a
    b = resample_to(a, SR, SR * 2)
    assert abs(len(b) - len(a) * 2) <= 2


# ---------------------------------------------------------------------------
# session：分派 / 护栏 / 时长
# ---------------------------------------------------------------------------


def _audio_score(rel="audio/x.wav", offset=1.0, name="原曲"):
    return Score(title="t", tracks=[Track(name=name, kind="audio", audio={"file": rel, "offset": offset})])


def test_session_assigns_audio_source(wd):
    _write_sine(wd / "audio" / "x.wav", seconds=0.5)
    sess = HostSession.from_score(_audio_score(), soundfont=str(wd / "sf.fake"), samplerate=SR, base_dir=wd)
    src = sess.tracks[0].source
    assert isinstance(src, AudioClipSource) and src.offset == 1.0
    assert sess.duration == pytest.approx(1.5, abs=1e-6)  # 时长含音频尾


def test_session_audio_requires_base_dir(wd):
    _write_sine(wd / "audio" / "x.wav", seconds=0.5)
    with pytest.raises(ValueError, match="base_dir"):
        HostSession.from_score(_audio_score(), soundfont=str(wd / "sf.fake"), samplerate=SR)


def test_session_audio_path_guards(wd):
    with pytest.raises(ValueError, match="相对路径"):
        HostSession.from_score(_audio_score(rel="../x.flac"), soundfont=str(wd / "sf.fake"), samplerate=SR, base_dir=wd)
    with pytest.raises(ValueError, match="audio.file"):
        HostSession.from_score(_audio_score(rel=""), soundfont=str(wd / "sf.fake"), samplerate=SR, base_dir=wd)
    with pytest.raises(FileNotFoundError):
        HostSession.from_score(_audio_score(rel="audio/missing.flac"), soundfont=str(wd / "sf.fake"), samplerate=SR, base_dir=wd)


# ---------------------------------------------------------------------------
# 渲染混入（单一支点）
# ---------------------------------------------------------------------------


def _mix_session(wd):
    sig = _write_sine(wd / "audio" / "x.wav", seconds=0.5)
    tr_a = Track(name="原曲", kind="audio", audio={"file": "audio/x.wav", "offset": 1.0})
    tr_b = Track(name="pad", instrument=Instrument(program="piano", volume=1.0))
    tracks = [
        HostTrack(track=tr_b, source=_ConstSource(0.0)),
        HostTrack(track=tr_a, source=AudioClipSource(wd / "audio" / "x.wav", offset=1.0, samplerate=SR)),
    ]
    sess = HostSession(Score(title="t", tracks=[tr_b, tr_a]), tracks, samplerate=SR)
    return sess, sig


def test_render_buses_includes_audio(wd):
    sess, sig = _mix_session(wd)
    out = render_buses(sess, SR, auto_scale=False)
    assert out.ndim == 2 and out.shape[1] == 2
    assert out.shape[0] == int(2.5 * SR)  # max(音频尾 1.5) + 1s 释放尾
    i0, n = SR, len(sig)
    assert np.allclose(out[i0 : i0 + n, 0], sig, atol=1e-6)
    assert np.allclose(out[i0 : i0 + n, 1], sig, atol=1e-6)  # pan 中位双等
    assert float(np.abs(out[i0 + n :, :]).max()) < 1e-6
    assert float(np.abs(out[:i0, :]).max()) < 1e-6


def test_render_buses_audio_skips_stem_cache(wd):
    from tsov.host.cache import StemStore

    sess, sig = _mix_session(wd)
    store = StemStore(wd / "cachehome")
    stats: dict = {}
    out1 = render_buses(sess, SR, cache=store, auto_scale=False, stats=stats)
    seen = (stats.get("rendered") or []) + (stats.get("cached") or [])
    assert "原曲" not in seen           # 音频轨不走 stem 库
    assert "pad" in seen                # 普通轨照常入缓存
    out2 = render_buses(sess, SR, cache=store, auto_scale=False)
    assert np.allclose(out1, out2)
    i0, n = SR, len(sig)
    assert np.allclose(out2[i0 : i0 + n, 0], sig, atol=1e-6)


# ---------------------------------------------------------------------------
# B：import_audio（工程入库）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 ffmpeg")
def test_import_audio_basic_and_dedupe(wd):
    proj = Project.create("e2proj", Score(title="t"), parent=wd)
    src = wd / "src素材.wav"
    _write_sine(src, seconds=0.3, sr=48000)  # 48k 源 → 转 44.1k
    info = proj.import_audio(src, name="素材")
    assert info["file"].startswith("audio/") and info["file"].endswith(".flac")
    assert info["samplerate"] == 44100 and info["deduped"] is False
    assert abs(info["seconds"] - 0.3) < 0.01
    assert (proj.root / info["file"]).is_file()

    info2 = proj.import_audio(src, name="素材")
    assert info2["deduped"] is True and info2["file"] == info["file"]
    assert len(list((proj.root / "audio").glob("*.flac"))) == 1  # 同内容只留一份


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 ffmpeg")
def test_import_audio_guards(wd):
    proj = Project.create("e2g", Score(title="t"), parent=wd)
    with pytest.raises(ValueError, match="不存在"):
        proj.import_audio(wd / "nope.wav")
    bad = wd / "x.txt"
    bad.write_text("hi")
    with pytest.raises(ValueError, match="扩展名"):
        proj.import_audio(bad)
    empty = wd / "empty.wav"
    empty.write_bytes(b"")
    with pytest.raises(ValueError, match="为空"):
        proj.import_audio(empty)
    junk = wd / "junk.wav"
    junk.write_bytes(b"not a real wav at all" * 100)
    with pytest.raises(ValueError, match="转码失败"):
        proj.import_audio(junk)


# ---------------------------------------------------------------------------
# 命令层 op：add_audio_track / set_audio_track
# ---------------------------------------------------------------------------


def test_add_audio_track_op():
    score = Score(title="t", tracks=[Track(name="melody")])
    batch = EditBatch(label="导入音频").add(
        "add_audio_track", value={"file": "audio/原曲-1a2b3c4d.flac", "offset": 2.5}
    )
    new, res = batch.apply(score)
    assert res.applied == 1 and res.ok
    tr = new.tracks[-1]
    assert tr.kind == "audio" and tr.audio == {"file": "audio/原曲-1a2b3c4d.flac", "offset": 2.5}
    assert tr.name == "原曲"  # 缺省名去哈希后缀

    # 重名 → 自动加序号
    batch2 = EditBatch().add("add_audio_track", value={"file": "audio/原曲-deadbeef.flac"})
    new2, res2 = batch2.apply(new)
    assert res2.applied == 1 and new2.tracks[-1].name == "原曲 2"


@pytest.mark.parametrize(
    "value,pat",
    [
        ({"file": "../x.flac"}, "audio/ 内相对路径"),
        ({"file": "/abs/x.flac"}, "audio/ 内相对路径"),
        ({"file": "other/x.flac"}, "audio/ 内相对路径"),
        ({"file": ""}, "为空"),
        ({"file": "audio/x.flac", "offset": -1}, "不能为负"),
        ({"file": "audio/x.flac", "name": "长" * 65}, "过长"),
        ("not-a-dict", "value 需"),
    ],
)
def test_add_audio_track_guards(value, pat):
    score = Score(title="t", tracks=[])
    new, res = EditBatch().add("add_audio_track", value=value).apply(score)
    assert res.applied == 0
    assert any(pat in e for e in res.errors), res.errors
    assert len(new.tracks) == 0


def test_set_audio_track_op():
    score = Score(
        title="t",
        tracks=[
            Track(name="melody"),
            Track(name="原曲", kind="audio", audio={"file": "audio/x-1a2b3c4d.flac", "offset": 1.0}),
        ],
    )
    new, res = EditBatch().add("set_audio_track", track=1, value={"offset": 3.5}).apply(score)
    assert res.applied == 1 and new.tracks[1].audio["offset"] == 3.5

    _, res2 = EditBatch().add("set_audio_track", track=0, value={"offset": 1.0}).apply(score)
    assert res2.applied == 0 and "只作用于音频轨" in res2.errors[0]

    _, res3 = EditBatch().add("set_audio_track", track=1, value={"offset": -0.5}).apply(score)
    assert res3.applied == 0 and "不能为负" in res3.errors[0]

    _, res4 = EditBatch().add("set_audio_track", track=1, value={"file": "evil/x.flac"}).apply(score)
    assert res4.applied == 0 and "audio/ 内" in res4.errors[0]

    _, res5 = EditBatch().add("set_audio_track", track=1, value={}).apply(score)
    assert res5.applied == 0 and "无可改字段" in res5.errors[0]

    # 换素材（合法路径）
    new6, res6 = EditBatch().add("set_audio_track", track=1, value={"file": "audio/new-beefcafe.flac"}).apply(score)
    assert res6.applied == 1 and new6.tracks[1].audio["file"].endswith("new-beefcafe.flac")
    assert new6.tracks[1].audio["offset"] == 1.0  # 未动字段保留
