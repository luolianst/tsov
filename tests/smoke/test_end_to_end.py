"""M4 端到端冒烟测试（ADR-0005 测试第 3 层 smoke）：合成小星星 → 全管线 → 产物断言。

- 转录后端 = game（openvpi/GAME，M4 唯一底座）：合成音频需带谐波（纯正弦 GAME 不认作人声，
  实测 2/3 次谐波 → 14/14 音全对）
- 离线可跑：LLM 分析 mock（不依赖外部 API）
- 若 GAME 模型 / SoundFont 缺失（vendor 未下载），跳过并提示（不误伤 CI）
"""

import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from tsov.analysis import analyze, build_semantic_dataset, build_prompt
from tsov.core.notes import Note, Voice
from tsov.midi.export import parse_key_signature, score_to_midi
from tsov.pipeline import build_score, run_closed_loop
from tsov.render.fluidsynth_backend import default_soundfont

# 小星星：C 大调，C4 起
TWINKLE = [60, 60, 67, 67, 69, 69, 67, 65, 65, 64, 64, 62, 62, 60]
NOTE_DUR = 0.30
GAP = 0.20
SR = 16000


def game_model_available() -> bool:
    from tsov.dsp.backends.game import _default_model

    try:
        _default_model()
        return True
    except RuntimeError:
        return False


def synth_twinkle(path, sr=SR):
    """纯 python 合成"歌声化"小星星：基波 + 2/3 次谐波 + 指数衰减包络，16k 单声道 PCM16 wav。

    纯正弦 GAME 不识别（无谐波不似人声），加谐波后 GAME 端到端转录 14/14 音全对。
    """
    samples = []
    for midi in TWINKLE:
        hz = 440.0 * 2.0 ** ((midi - 69) / 12.0)
        n_on = int(NOTE_DUR * sr)
        n_gap = int(GAP * sr)
        tt = np.arange(n_on) / sr
        env = np.exp(-tt * 3.0)
        sig = (
            np.sin(2 * np.pi * hz * tt)
            + 0.5 * np.sin(2 * np.pi * 2 * hz * tt)
            + 0.25 * np.sin(2 * np.pi * 3 * hz * tt)
        )
        samples.append(sig * env)
        samples.append(np.zeros(n_gap))
    audio = np.concatenate(samples).astype(np.float32)
    audio /= np.max(np.abs(audio)) + 1e-9
    sf.write(str(path), audio, sr, subtype="PCM_16")
    return path


@pytest.fixture(scope="module")
def twinkle_wav(tmp_path_factory):
    return synth_twinkle(tmp_path_factory.mktemp("twinkle") / "twinkle.wav")


@pytest.mark.skipif(not game_model_available(), reason="GAME 模型未下载（vendor/GAME/pretrained/*.pt），跳过 game 端到端")
def test_synthetic_twinkle_pipeline_end_to_end(twinkle_wav, tmp_path):
    """合成小星星 → 全管线（转录/语义层/Score/MIDI/回放）→ 产物存在且非空。"""
    if default_soundfont() is None:
        pytest.skip("SoundFont 未下载（vendor/soundfonts/FluidR3_GM.sf2），跳过回放断言")
    out_dir = tmp_path / "run"
    summary = run_closed_loop(twinkle_wav, out_dir=out_dir, backend="game", llm=False)

    for key in ("voice", "semantic", "score", "midi", "wav"):
        p = Path(summary["artifacts"][key])
        assert p.is_file() and p.stat().st_size > 0

    # stage JSON 存在且非空
    for name in ("stage-01-voice.json", "stage-02-semantic.json", "stage-04-score.json"):
        assert (out_dir / name).stat().st_size > 0

    # MIDI 存在、音符数 > 0（pretty_midi 读回）
    import pretty_midi

    midi = pretty_midi.PrettyMIDI(str(out_dir / "song.mid"))
    total_notes = sum(len(i.notes) for i in midi.instruments)
    assert total_notes > 0

    # 回放 wav 时长 > 0
    wav_path = out_dir / "song.wav"
    info = sf.info(str(wav_path))
    assert info.frames > 0 and info.duration > 0


def test_analyze_llm_mocked(monkeypatch):
    """LLM 分析接口：mock call_llm，验证 analyze 正常合并 + llm=False 只出规则。"""
    voice = Voice(
        notes=[Note(start=0.0, end=0.3, pitch_midi=60, pitch_hz=261.6, confidence=0.9)],
        bpm=120.0,
        segments=[],
        source_audio="mock.wav",
        backend="game",
    )
    fake = {
        "key_candidates": [{"key": "C major", "confidence": 0.8}],
        "suspicious_notes": [],
        "phrase_suggestions": ["保持平稳"],
        "playback_notes": "钢琴",
    }

    def fake_call(semantic, **params):
        assert semantic["notes"]
        return dict(fake)

    monkeypatch.setattr("tsov.analysis.call_llm", fake_call)
    r = analyze(voice, llm=True)
    assert r.key_candidates == fake["key_candidates"]
    assert r.phrase_suggestions == fake["phrase_suggestions"]
    assert r.llm_used and not r.error

    r2 = analyze(voice, llm=False)
    assert r2.key_candidates == []
    assert not r2.llm_used


def test_semantic_dataset_rules():
    """语义层规则：音名/置信度标签/乐句分组/DSP 特征确定性。"""
    voice = Voice(
        notes=[
            Note(start=0.0, end=0.5, pitch_midi=60, pitch_hz=261.6, confidence=0.9),
            Note(start=0.7, end=1.0, pitch_midi=62, pitch_hz=293.7, confidence=0.3),
        ],
        bpm=120.0,
        segments=[],
        source_audio="mock.wav",
        backend="game",
    )
    s = build_semantic_dataset(voice)
    assert s["notes"][0]["note_name"] == "C4"
    assert s["notes"][0]["confidence_label"] == "high"
    assert s["notes"][1]["confidence_label"] == "low"
    assert s["dsp_features"]["note_count"] == 2
    assert len(build_prompt(s)) > 0


def test_score_to_midi_writes_key_signature(tmp_path):
    """score_to_midi：tempo/key signature/音符直写。"""
    score = build_score(
        Voice(notes=[Note(start=0.0, end=0.5, pitch_midi=60, pitch_hz=261.6)], bpm=100.0),
        analyze(
            Voice(notes=[Note(start=0.0, end=0.5, pitch_midi=60, pitch_hz=261.6)], bpm=100.0),
            llm=False,
        ),
        title="t",
    )
    from tsov.core.score import KeyCandidate

    score.key_candidates.append(KeyCandidate(key="C major", confidence=0.9))
    path = tmp_path / "t.mid"
    score_to_midi(score, str(path))
    import pretty_midi

    midi = pretty_midi.PrettyMIDI(str(path))
    assert midi.key_signature_changes[0].key_number == parse_key_signature("C major")
    assert abs(midi.get_tempo_changes()[1][0] - 100.0) < 1e-6
    assert sum(len(i.notes) for i in midi.instruments) == 1
