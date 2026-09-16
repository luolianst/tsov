"""宿主框架单元测试（ADR-0013）：HostEngine 渲染 / mix_graph 音量 / 音源桩。"""

import json
import shutil
import time
import uuid
from pathlib import Path

import numpy as np
import pytest

from tsov.core.notes import Note
from tsov.core.score import Instrument, Score, Track
from tsov.host import HostEngine, HostSession, HostTrack, SoundSource
from tsov.host.device import mix_graph
from tsov.host.instrument import SFZSource, VST3Source
from tsov.render.fluidsynth_backend import default_soundfont

from unit._cleanup import rmtree_force

_SOUNDFONT = default_soundfont()

pytestmark = pytest.mark.skipif(not _SOUNDFONT, reason="vendor 无 SoundFont（FluidR3_GM.sf2 未下载，宿主渲染跳过）")


@pytest.fixture()
def ws():
    """工作区临时目录：手动 mkdir（绕开 pytest tmpdir 的 AV 锁 + 沙箱对 mkdtemp 目录的拒绝）。"""
    d = Path("output") / f"wstest-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    rmtree_force(d)   # 清理：.git/objects 只读属性 → chmod 强删（见 _cleanup.py）


def _tiny_score() -> Score:
    return Score(
        title="t",
        tempo=100.0,
        tracks=[
            Track(
                name="melody",
                instrument=Instrument(backend="fluidsynth", program="piano", volume=0.8),
                notes=[
                    Note(start=0.0, end=0.5, pitch_midi=60, pitch_hz=261.63),
                    Note(start=0.6, end=1.0, pitch_midi=62, pitch_hz=293.66),
                    Note(start=1.1, end=1.5, pitch_midi=64, pitch_hz=329.63),
                ],
            )
        ],
    )


def test_host_engine_render_score_produces_audio(ws):
    score = _tiny_score()
    in_path = ws / "in.json"
    out_wav = ws / "out.wav"
    in_path.write_text(json.dumps(score.to_dict()), encoding="utf-8")

    engine = HostEngine()
    wav = engine.render_score(str(in_path), str(out_wav))

    assert str(out_wav) == wav and out_wav.exists()
    import soundfile as sf

    audio, sr = sf.read(str(out_wav), dtype="float32")
    assert sr == engine.samplerate
    assert len(audio) > 0
    assert float(np.abs(audio).max()) > 0.01, "渲染结果不应静音"


def test_mix_graph_applies_instrument_volume():
    class OnesSource(SoundSource):
        name = "ones"

        def render(self, notes, samplerate, n_frames):
            return np.ones((n_frames,), dtype=np.float32)

    track = Track(
        name="m",
        instrument=Instrument(backend="fluidsynth", program="piano", volume=0.5),
        notes=[Note(start=0.0, end=1.0, pitch_midi=60, pitch_hz=261.63)],
    )
    session = HostSession(
        score=Score(title="x", tracks=[track]),
        tracks=[HostTrack(track=track, source=OnesSource())],
        samplerate=44100,
    )
    mix = mix_graph(session)
    assert abs(float(mix.max()) - 0.5) < 1e-5
    assert abs(float(mix.min()) - 0.5) < 1e-5


def test_instrument_sources_missing_paths_raise():
    """里程碑二后 VST3/SFZ 不再桩（见 test_host_sources.py）；但路径非法仍须明确报错，不静默。"""
    with pytest.raises(ImportError):        # pedalboard 加载失败
        VST3Source("some-vst3.dll")
    with pytest.raises(RuntimeError):       # 文件不存在
        SFZSource("some.sfz")
