"""agent 工具：set_tempo/拍号（M-V6 时间参数）+ Score.time_signature 兼容性 + MIDI 拍号导出。

约定：不用 pytest tmp_path（已知坑 81）——手动 output/<uuid> 目录 + teardown（带重试清理）。
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path

import pytest

from tsov.agent.tools import build_default_registry, tool_load_score, tool_set_tempo
from tsov.core.score import Score, parse_time_signature
from tsov.midi.export import score_to_midi


def _ws() -> Path:
    d = Path("output") / f"wstool-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cleanup(d: Path) -> None:
    for _ in range(6):   # 安全软件瞬时锁 → 重试清理（防残物堆积）
        shutil.rmtree(d, ignore_errors=True)
        if not d.exists():
            return
        time.sleep(0.5)


def _score_payload(title="t", tempo=120.0, ts=None):
    payload = {
        "title": title, "tempo": tempo, "key_candidates": [],
        "tracks": [{
            "name": "melody",
            "instrument": {"backend": "fluidsynth", "program": "", "volume": 1.0, "effects": []},
            "notes": [{
                "start": 0.0, "end": 0.3, "pitch_midi": 64, "pitch_hz": 329.6,
                "velocity": 0.8, "confidence": 0.9, "deviation_cents": 0.0, "is_ornament": False,
            }],
        }],
        "meta": {},
    }
    if ts is not None:
        payload["time_signature"] = ts
    return payload


def test_parse_time_signature():
    assert parse_time_signature("6/8") == (6, 8)
    assert parse_time_signature(" 3/4 ") == (3, 4)
    assert parse_time_signature(None) == (4, 4)
    assert parse_time_signature("") == (4, 4)
    assert parse_time_signature("bad") == (4, 4)
    assert parse_time_signature("6/7") == (4, 4)      # 分母不在白名单 → 缺省
    assert parse_time_signature("99/4") == (4, 4)     # 分子越界 → 缺省


def test_score_time_signature_backcompat_and_roundtrip():
    """旧谱（无字段）→ 4/4；带字段 → 保留；to_dict 落盘带字段。"""
    s = Score.from_dict(_score_payload())
    assert s.time_signature == "4/4"
    assert s.to_dict()["time_signature"] == "4/4"
    s2 = Score.from_dict(_score_payload(ts="6/8"))
    assert s2.time_signature == "6/8"


def test_set_tempo_tool_writes_and_reports():
    d = _ws()
    try:
        src = d / "score.json"
        src.write_text(json.dumps(_score_payload()), encoding="utf-8")
        out = tool_set_tempo({"score_path": str(src), "tempo": 200, "time_signature": "6/8"})
        assert "已写回" in out and "tempo=200.0" in out and "time_signature=6/8" in out
        assert "每小节≈0.900s" in out          # 200BPM 的 6/8 → 每小节 3 个四分拍 = 0.9s
        edited = json.loads((d / "agent-edited-score.json").read_text(encoding="utf-8"))
        assert edited["tempo"] == 200.0 and edited["time_signature"] == "6/8"
        # 原文件不动（同 edit_score 约定：写 agent-edited-score.json）
        assert json.loads(src.read_text(encoding="utf-8"))["tempo"] == 120.0
    finally:
        _cleanup(d)


def test_set_tempo_tool_rejects_bad_values():
    d = _ws()
    try:
        src = d / "score.json"
        src.write_text(json.dumps(_score_payload()), encoding="utf-8")
        for bad in ({"tempo": 10}, {"tempo": 1000}, {"time_signature": "6/7"},
                    {"time_signature": "x"}, {"time_signature": "4/4/4"}):
            with pytest.raises(ValueError):
                tool_set_tempo({"score_path": str(src), **bad})
        with pytest.raises(ValueError):
            tool_set_tempo({"score_path": str(src)})   # 两个都没给 → 报错
    finally:
        _cleanup(d)


def test_set_tempo_tool_remaps_notes_by_default():
    """Q47：工具默认 remap=True → 改 BPM 同步缩放音符（跟速重排）；写 agent-edited-score.json。"""
    d = _ws()
    try:
        src = d / "score.json"
        src.write_text(json.dumps(_score_payload()), encoding="utf-8")   # 1 音 0.0–0.3 @120
        out = tool_set_tempo({"score_path": str(src), "tempo": 240})
        assert "跟速重排" in out
        edited = json.loads((d / "agent-edited-score.json").read_text(encoding="utf-8"))
        assert edited["tempo"] == 240.0
        n = edited["tracks"][0]["notes"][0]
        assert n["start"] == 0.0 and abs(n["end"] - 0.15) < 1e-6         # factor=120/240=0.5
        assert json.loads(src.read_text(encoding="utf-8"))["tracks"][0]["notes"][0]["end"] == 0.3  # 原文件不动
    finally:
        _cleanup(d)


def test_set_tempo_tool_remap_false_keeps_notes():
    d = _ws()
    try:
        src = d / "score.json"
        src.write_text(json.dumps(_score_payload()), encoding="utf-8")
        out = tool_set_tempo({"score_path": str(src), "tempo": 240, "remap": False})
        assert "跟速重排" not in out
        edited = json.loads((d / "agent-edited-score.json").read_text(encoding="utf-8"))
        assert edited["tempo"] == 240.0
        assert edited["tracks"][0]["notes"][0]["end"] == 0.3             # 音符不动
    finally:
        _cleanup(d)


def test_load_score_accepts_both_keys():
    """键名修正：score_path（主）与 path（兼容别名）都能读；都不给 → 明确报错（G5 遗留；原 KeyError: 'path'）。"""
    d = _ws()
    try:
        src = d / "score.json"
        src.write_text(json.dumps(_score_payload()), encoding="utf-8")
        a = tool_load_score({"score_path": str(src)})
        b = tool_load_score({"path": str(src)})
        assert "melody" in a and a == b
        with pytest.raises(ValueError):
            tool_load_score({})
    finally:
        _cleanup(d)


def test_registry_includes_set_tempo():
    reg = build_default_registry()
    names = [s.name for s in reg.specs()]
    assert "set_tempo" in names


def test_midi_export_writes_time_signature():
    """拍号写进 MIDI（pretty_midi time_signature_changes）；缺省 4/4。"""
    import pretty_midi

    d = _ws()
    try:
        out = d / "s68.mid"
        score_to_midi(Score.from_dict(_score_payload(ts="6/8")), str(out))
        pm = pretty_midi.PrettyMIDI(str(out))
        ts = pm.time_signature_changes
        assert ts and ts[0].numerator == 6 and ts[0].denominator == 8

        out2 = d / "s44.mid"
        score_to_midi(Score.from_dict(_score_payload()), str(out2))
        pm2 = pretty_midi.PrettyMIDI(str(out2))
        assert pm2.time_signature_changes[0].numerator == 4
        assert pm2.time_signature_changes[0].denominator == 4
    finally:
        _cleanup(d)
