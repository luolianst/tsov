"""tools_compose（M-V6 双任务工具链）测试：grid 换算 / 轨管理 / 段落复制 / 混音参数 / 效果 / 鼓型 / 测量 / 导出。

不需要音频设备；渲染类用例缺 SoundFont / ffmpeg 时自动 skip（沿用 test_web 的守卫风格）。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tsov.agent import tools_compose as tc
from tsov.agent.tools import build_default_registry
from tsov.core.score import Instrument, Score, Track

ROOT = Path(__file__).resolve().parents[2]
SF2 = ROOT / "vendor" / "soundfonts" / "FluidR3_GM.sf2"

NEW_TOOLS = {
    "voice_to_score", "detect_key", "create_track", "write_notes", "duplicate_bars",
    "set_track_mix", "remove_track", "rename_track", "apply_effect", "apply_pattern",
    "analyze_levels", "export_audio", "export_midi",
    "read_text",
}


def _melody() -> Track:
    return Track(name="melody", instrument=Instrument(backend="fluidsynth", program="piano", volume=0.8))


def _drums() -> Track:
    return Track(name="drums", instrument=Instrument(backend="fluidsynth", program="drums", volume=0.9))


def _base(tracks: list[Track]) -> Score:
    return Score(title="t", tempo=200.0, time_signature="6/8", tracks=tracks)


def _save_score(path: Path, score: Score) -> Path:
    path.write_text(json.dumps(score.to_dict(), ensure_ascii=False), encoding="utf-8")
    return path


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------


def test_note_name_to_midi():
    assert tc.note_name_to_midi("C4") == 60
    assert tc.note_name_to_midi("E4") == 64
    assert tc.note_name_to_midi("f#3") == 54
    assert tc.note_name_to_midi("Bb2") == 46
    assert tc.note_name_to_midi("H4") is None


def test_voice_to_score(tmp_path):
    voice = {
        "notes": [
            {"start": 0.0, "end": 0.3, "pitch_midi": 64, "pitch_hz": 329.63, "velocity": 0.8},
            {"start": 0.3, "end": 0.6, "pitch_midi": 67, "pitch_hz": 392.0, "velocity": 0.75},
        ],
        "bpm": 200.0, "bpm_confidence": 0.8, "segments": [], "source_audio": "x.mp3", "backend": "game",
    }
    vp = tmp_path / "voice.json"
    vp.write_text(json.dumps(voice), encoding="utf-8")
    out = tmp_path / "proj" / "score.json"
    msg = tc.tool_voice_to_score({"voice_path": str(vp), "output": str(out)})
    assert out.is_file() and "piano" in msg
    s = _load(out)
    assert s["tempo"] == 200.0
    assert len(s["tracks"]) == 1 and s["tracks"][0]["instrument"]["program"] == "piano"
    assert len(s["tracks"][0]["notes"]) == 2


def test_create_track_validation(tmp_path):
    sp = _save_score(tmp_path / "score.json", _base([_melody()]))
    msg = tc.tool_create_track({"score_path": str(sp), "output": str(sp),
                                "program": "synth_lead", "name": "lead"})
    assert "track[1]" in msg
    s = _load(sp)
    assert s["tracks"][1]["instrument"]["program"] == "synth_lead"
    assert s["tracks"][1]["name"] == "lead"
    with pytest.raises(ValueError):
        tc.tool_create_track({"score_path": str(sp), "output": str(sp), "program": "theremin"})


def test_write_notes_grid_math(tmp_path):
    sp = _save_score(tmp_path / "score.json", _base([_melody()]))
    # 6/8 @200BPM：小节 0.9s，16 分格 0.075s
    msg = tc.tool_write_notes({"score_path": str(sp), "output": str(sp), "track": 0, "notes": [
        {"bar": 1, "grid": 1, "len": 2, "note": "E4", "velocity": 0.9},
        {"bar": 2, "grid": 7, "len": 1, "pitch_midi": 60},
    ]})
    assert "覆盖小节 1-2" in msg
    notes = _load(sp)["tracks"][0]["notes"]
    assert notes[0]["pitch_midi"] == 64
    assert notes[0]["start"] == pytest.approx(0.0) and notes[0]["end"] == pytest.approx(0.15)
    assert notes[1]["start"] == pytest.approx(1.35) and notes[1]["end"] == pytest.approx(1.425)

    # 越界校验：grid 超每小节格数 / len 跨小节
    with pytest.raises(ValueError):
        tc.tool_write_notes({"score_path": str(sp), "output": str(sp),
                             "notes": [{"bar": 1, "grid": 13, "len": 1, "pitch_midi": 60}]})
    with pytest.raises(ValueError):
        tc.tool_write_notes({"score_path": str(sp), "output": str(sp),
                             "notes": [{"bar": 1, "grid": 12, "len": 2, "pitch_midi": 60}]})

    # replace 覆盖语义（批A P31）：只重写本次涉及的小节范围，范围外保留
    msg = tc.tool_write_notes({"score_path": str(sp), "output": str(sp), "mode": "replace",
                               "notes": [{"bar": 1, "grid": 1, "len": 12, "pitch_midi": 72}]})
    assert "范围外保留" in msg and "替换原 1 音" in msg
    notes = _load(sp)["tracks"][0]["notes"]
    assert len(notes) == 2                                # bar2 的原音保留
    assert {n["pitch_midi"] for n in notes} == {60, 72}


def test_write_notes_replace_scope_keeps_outside(tmp_path):
    """批A P31 实录回归：16 小节谱只替换第 1 小节 → 第 10 小节原有音不受影响。"""
    sp = _save_score(tmp_path / "score.json", _base([_melody()]))
    tc.tool_write_notes({"score_path": str(sp), "output": str(sp), "notes": [
        {"bar": 10, "grid": 1, "len": 2, "pitch_midi": 60},
    ]})
    msg = tc.tool_write_notes({"score_path": str(sp), "output": str(sp), "mode": "replace",
                               "notes": [{"bar": 1, "grid": 1, "len": 2, "pitch_midi": 72}]})
    notes = _load(sp)["tracks"][0]["notes"]
    assert len(notes) == 2 and {n["pitch_midi"] for n in notes} == {60, 72}
    assert "范围外保留" in msg


def test_duplicate_bars_all_tracks(tmp_path):
    sp = _save_score(tmp_path / "score.json", _base([_melody(), _drums()]))
    tc.tool_write_notes({"score_path": str(sp), "output": str(sp), "track": 0, "notes": [
        {"bar": 1, "grid": 1, "len": 2, "pitch_midi": 64},
        {"bar": 2, "grid": 1, "len": 2, "pitch_midi": 67},
    ]})
    tc.tool_write_notes({"score_path": str(sp), "output": str(sp), "track": 1, "notes": [
        {"bar": 1, "grid": 1, "len": 2, "pitch_midi": 36},
    ]})
    msg = tc.tool_duplicate_bars({"score_path": str(sp), "output": str(sp),
                                  "src_start_bar": 1, "src_end_bar": 2, "dest_start_bar": 3})
    assert "全轨复制" in msg and "追加" in msg and "原有音符保留" in msg
    s = _load(sp)
    assert len(s["tracks"][0]["notes"]) == 4 and len(s["tracks"][1]["notes"]) == 2
    starts = sorted(n["start"] for n in s["tracks"][0]["notes"])
    assert starts[0] == pytest.approx(0.0) and starts[-1] == pytest.approx(2.7)  # 0.9+1.8
    drum_starts = sorted(n["start"] for n in s["tracks"][1]["notes"])
    assert drum_starts[-1] == pytest.approx(1.8)


def test_set_track_mix_and_effect(tmp_path):
    sp = _save_score(tmp_path / "score.json", _base([_melody()]))
    tc.tool_set_track_mix({"score_path": str(sp), "output": str(sp), "track": 0, "volume": 0.5, "pan": -0.3})
    s = _load(sp)
    assert s["tracks"][0]["instrument"]["volume"] == pytest.approx(0.5)
    assert s["tracks"][0]["pan"] == pytest.approx(-0.3)
    with pytest.raises(ValueError):
        tc.tool_set_track_mix({"score_path": str(sp), "output": str(sp), "volume": 3.0})

    tc.tool_apply_effect({"score_path": str(sp), "output": str(sp), "track": 0, "preset": "piano-pop-reverb"})
    effects = _load(sp)["tracks"][0]["instrument"]["effects"]
    assert effects and "reverb" in {e["type"] for e in effects}
    with pytest.raises(ValueError):
        tc.tool_apply_effect({"score_path": str(sp), "output": str(sp), "track": 0, "preset": "no-such-preset"})


def test_remove_rename_track_tools(tmp_path):
    sp = _save_score(tmp_path / "score.json", _base([_melody(), _drums()]))
    # 重命名（轨名寻址）
    msg = tc.tool_rename_track({"score_path": str(sp), "output": str(sp), "track": "drums", "name": "drums2"})
    assert "drums2" in msg
    assert [t["name"] for t in _load(sp)["tracks"]] == ["melody", "drums2"]
    with pytest.raises(ValueError):
        tc.tool_rename_track({"score_path": str(sp), "output": str(sp), "track": "drums2", "name": "melody"})  # 重名
    with pytest.raises(ValueError):
        tc.tool_rename_track({"score_path": str(sp), "output": str(sp), "name": "x"})  # track 必填
    # 删除（轨名寻址）
    msg2 = tc.tool_remove_track({"score_path": str(sp), "output": str(sp), "track": "drums2"})
    assert "已删除" in msg2 and "2 轨 → 1 轨" in msg2
    assert [t["name"] for t in _load(sp)["tracks"]] == ["melody"]
    with pytest.raises(ValueError):
        tc.tool_remove_track({"score_path": str(sp), "output": str(sp)})  # track 必填
    with pytest.raises(ValueError):
        tc.tool_remove_track({"score_path": str(sp), "output": str(sp), "track": "nope"})  # 未知轨名


def test_apply_pattern(tmp_path):
    sp = _save_score(tmp_path / "score.json", _base([_drums()]))
    tc.tool_apply_pattern({"score_path": str(sp), "output": str(sp),
                           "pattern": "wotaiko_drums_base", "start_bar": 1, "bars": 4})
    notes = _load(sp)["tracks"][0]["notes"]
    assert len(notes) == 11 * 4
    pitches = {n["pitch_midi"] for n in notes}
    assert {36, 38, 42, 46} <= pitches

    tc.tool_apply_pattern({"score_path": str(sp), "output": str(sp),
                           "pattern": "wotaiko_drums_energy", "start_bar": 5, "bars": 1, "crash": True})
    notes = _load(sp)["tracks"][0]["notes"]
    assert len(notes) == 11 * 4 + 15  # energy 14 + crash 1
    assert 49 in {n["pitch_midi"] for n in notes}


def test_apply_pattern_wrong_meter(tmp_path):
    score = Score(title="t", tempo=200.0, time_signature="4/4", tracks=[_drums()])
    sp = _save_score(tmp_path / "score.json", score)
    with pytest.raises(ValueError):
        tc.tool_apply_pattern({"score_path": str(sp), "output": str(sp),
                               "pattern": "wotaiko_drums_base", "start_bar": 1, "bars": 1})


def test_detect_key(tmp_path):
    sp = _save_score(tmp_path / "score.json", _base([_melody()]))
    tc.tool_write_notes({"score_path": str(sp), "output": str(sp), "notes": [
        {"bar": 1, "grid": 1, "len": 4, "note": "C4"}, {"bar": 1, "grid": 5, "len": 4, "note": "D4"},
        {"bar": 1, "grid": 9, "len": 4, "note": "E4"}, {"bar": 2, "grid": 1, "len": 4, "note": "G4"},
        {"bar": 2, "grid": 5, "len": 4, "note": "C5"}, {"bar": 2, "grid": 9, "len": 4, "note": "E4"},
    ]})
    msg = tc.tool_detect_key({"score_path": str(sp)})
    assert "track[0]" in msg and "(" in msg


@pytest.mark.skipif(not SF2.exists(), reason="缺 vendor/soundfonts/FluidR3_GM.sf2")
def test_analyze_levels(tmp_path):
    sp = _save_score(tmp_path / "score.json", _base([_melody(), _drums()]))
    tc.tool_write_notes({"score_path": str(sp), "output": str(sp), "track": 0, "notes": [
        {"bar": 1, "grid": 1, "len": 12, "note": "C5"},
    ]})
    tc.tool_apply_pattern({"score_path": str(sp), "output": str(sp),
                           "track": 1, "pattern": "wotaiko_drums_base", "start_bar": 1, "bars": 1})
    msg = tc.tool_analyze_levels({"score_path": str(sp)})
    assert "电平报告" in msg and "混音峰值" in msg and "track[0]" in msg


@pytest.mark.skipif(not SF2.exists(), reason="缺 vendor/soundfonts/FluidR3_GM.sf2")
def test_analyze_levels_exclusion_messages(tmp_path):
    """批A P25b：被 solo/mute 排除 ≠ 无音符——报告须分列原因并标注 solo 门控。"""
    sp = _save_score(tmp_path / "score.json", _base([_melody(), _drums()]))
    tc.tool_write_notes({"score_path": str(sp), "output": str(sp), "track": 0, "notes": [
        {"bar": 1, "grid": 1, "len": 12, "pitch_midi": 72},
    ]})
    tc.tool_apply_pattern({"score_path": str(sp), "output": str(sp),
                           "track": 1, "pattern": "wotaiko_drums_base", "start_bar": 1, "bars": 1})
    # 轨 1 solo → 轨 0（有音）被门控排除
    tc.tool_set_track_mix({"score_path": str(sp), "output": str(sp), "track": 1, "solo": True})
    msg = tc.tool_analyze_levels({"score_path": str(sp)})
    line0 = next(l for l in msg.splitlines() if l.startswith("track[0]"))
    assert "solo 门控排除" in line0 and "无音符" not in line0
    assert "⚠ solo 门控生效中" in msg
    # 轨 0 mute → 原因转 mute
    tc.tool_set_track_mix({"score_path": str(sp), "output": str(sp), "track": 0, "mute": True})
    msg2 = tc.tool_analyze_levels({"score_path": str(sp)})
    line0b = next(l for l in msg2.splitlines() if l.startswith("track[0]"))
    assert "mute 静音" in line0b


@pytest.mark.skipif(not SF2.exists() or not shutil.which("ffmpeg"), reason="缺 SoundFont 或 ffmpeg")
def test_export_audio_mp3(tmp_path):
    sp = _save_score(tmp_path / "score.json", _base([_melody()]))
    tc.tool_write_notes({"score_path": str(sp), "output": str(sp), "notes": [
        {"bar": 1, "grid": 1, "len": 6, "note": "E4"}, {"bar": 1, "grid": 7, "len": 6, "note": "G4"},
    ]})
    out = tmp_path / "demo.mp3"
    msg = tc.tool_export_audio({"score_path": str(sp), "out": str(out), "format": "mp3"})
    assert out.is_file() and out.stat().st_size > 1000 and "MP3" in msg
    assert out.with_suffix(".wav").is_file()


def test_export_midi(tmp_path):
    sp = _save_score(tmp_path / "score.json", _base([_melody(), _drums()]))
    tc.tool_write_notes({"score_path": str(sp), "output": str(sp), "track": 0, "notes": [
        {"bar": 1, "grid": 1, "len": 6, "note": "E4"}, {"bar": 1, "grid": 7, "len": 6, "note": "G4"},
    ]})
    tc.tool_apply_pattern({"score_path": str(sp), "output": str(sp), "track": 1,
                           "pattern": "wotaiko_drums_base", "start_bar": 1, "bars": 1})
    out = tmp_path / "out.mid"
    msg = tc.tool_export_midi({"score_path": str(sp), "out": str(out)})
    assert out.is_file() and out.stat().st_size > 50 and "MIDI" in msg


def test_resolve_track_numeric_string(tmp_path):
    """LLM 常把 track 传成字符串 "0"——必须按索引解析（任务1 实测坑）。"""
    sp = _save_score(tmp_path / "score.json", _base([_melody()]))
    msg = tc.tool_set_track_mix({"score_path": str(sp), "output": str(sp), "track": "0", "volume": 0.6})
    assert "track[0]" in msg
    with pytest.raises(ValueError):
        tc.tool_set_track_mix({"score_path": str(sp), "output": str(sp), "track": "9", "volume": 0.6})


def test_apply_effect_requires_track(tmp_path):
    """任务2 实测坑：apply_effect 缺 track 会静默落到 track[0]（master-limiter 覆盖旋律链）→ 现要求必填。"""
    sp = _save_score(tmp_path / "score.json", _base([_melody()]))
    with pytest.raises(ValueError, match="track 必填"):
        tc.tool_apply_effect({"score_path": str(sp), "output": str(sp), "preset": "piano-pop-reverb"})


def test_read_text_tool():
    msg = tc.tool_read_text({"path": "presets/arrangements/wotaiko-fast-6-8.json"})
    assert "wotaiko-fast-6-8" in msg and "instruments" in msg
    with pytest.raises(ValueError):
        tc.tool_read_text({"path": "C:/Windows/win.ini"})  # 仓库外拒读


def test_read_text_match_and_offset():
    """大文件导航：match 定位行号 → offset/limit 按行读段（知识卡包检索用，2026-10-02 增）。"""
    path = "presets/arrangements/wotaiko-fast-6-8.json"
    m = tc.tool_read_text({"path": path, "match": "instruments"})
    assert "命中" in m and "instruments" in m and "L" in m
    seg = tc.tool_read_text({"path": path, "offset": 1, "limit": 3})
    first = seg.splitlines()[0]
    assert "显示 L1-L" in first
    assert seg.splitlines()[1].startswith("L1: ")
    none = tc.tool_read_text({"path": path, "match": "zzz-not-there-zzz"})
    assert "未找到" in none
    over = tc.tool_read_text({"path": path, "offset": 99999})
    assert "超出" in over


def test_load_score_rejects_non_score():
    """任务1/2 实测坑：load_score 读预设 json → KeyError: 'tempo' 天书报错 → 现给指引。"""
    from tsov.agent.tools import tool_load_score

    with pytest.raises(ValueError, match="read_text"):
        tool_load_score({"path": "presets/arrangements/wotaiko-fast-6-8.json"})


def test_registry_has_compose_tools():
    reg = build_default_registry()
    names = {spec.name for spec in reg.specs()}
    assert NEW_TOOLS <= names
