"""E4 段3：配器生成编排（generate/apply/store）单测——装配/落盘/命令层幂等。"""

import json
import shutil
import uuid
from pathlib import Path

from tsov.arrange import store
from tsov.arrange.apply import build_arrange_commands
from tsov.arrange.decide import default_plan
from tsov.arrange.expand import role_program, role_track_name
from tsov.arrange.generate import (assemble_tracks, candidate_score, generate_batch, tracks_from_specs)
from tsov.arrange.library import load_patterns
from tsov.core.notes import Note
from tsov.core.score import Bookmark, Instrument, KeyCandidate, Score, Track
from tsov.core.units import midi_to_hz
from tsov.host import EditBatch

PACK = "wotaiko-fast-6-8"


def _tmp_root() -> Path:
    return Path("output") / f"arrangetest-{uuid.uuid4().hex[:8]}"


def _facts_68():
    bar_sec, gpb = 1.5, 12
    grid_sec = bar_sec / gpb
    bars = [{"i": i + 1, "start": round(i * bar_sec, 9), "end": round((i + 1) * bar_sec, 9)} for i in range(4)]
    per_bar = [{"bar": b["i"], "root_pc": 2, "kind": "major", "tones": [2, 6, 9], "label": "D"} for b in bars]
    return {
        "project": "t", "pack": PACK, "strength": "standard", "key": "D major",
        "meter": {"time_signature": "6/8", "tempo": 120.0, "bar_sec": bar_sec, "gpb": gpb,
                  "grid_sec": grid_sec, "bars_total": 4},
        "bars": bars,
        "sections": [
            {"index": 0, "label": "前奏", "kind": "intro", "start": 0.0, "end": 3.0,
             "start_bar": 1, "end_bar": 2, "bars": 2, "energy": 0.5, "synthetic": False},
            {"index": 1, "label": "主歌", "kind": "verse", "start": 3.0, "end": 6.0,
             "start_bar": 3, "end_bar": 4, "bars": 2, "energy": 1.0, "synthetic": False},
        ],
        "chords": {"per_bar": per_bar},
        "melody": {"track": 0, "name": "melody", "notes": 2, "register": [64, 67], "mean_midi": 65.5},
        "roles": {}, "fills": {},
    }


def _score_68():
    notes = [Note(start=0.2 + i * 0.5, end=0.2 + i * 0.5 + 0.4, pitch_midi=62 + (i % 8),
                  pitch_hz=midi_to_hz(62 + (i % 8)), velocity=0.7, confidence=0.9) for i in range(9)]
    return Score(
        title="t68", tempo=120.0, time_signature="6/8",
        key_candidates=[KeyCandidate(key="D major", confidence=1.0)],
        tracks=[Track(name="melody", instrument=Instrument(backend="fluidsynth", program="piano"), notes=notes)],
        bookmarks=[Bookmark(scope="project", kind="section", start=0.0, end=3.0, label="前奏"),
                   Bookmark(scope="project", kind="section", start=3.0, end=4.6, label="主歌")],
    )


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


def test_assemble_tracks_default_plan():
    lib = load_patterns(PACK)
    facts = _facts_68()
    plan = default_plan(lib, facts)
    specs = assemble_tracks(lib, facts, plan, melody_notes=[])
    roles = {s["role"] for s in specs}
    # synth_lead 两段默认都是 off → 不出轨
    assert roles == {"piano", "synth_bass", "synth_pad", "e_drums", "e_guitar"}
    ed = next(s for s in specs if s["role"] == "e_drums")
    # 前奏 sparse 9×2 + 主歌 base 11×2 + 段头 crash 1
    assert ed["n_notes"] == 9 * 2 + 11 * 2 + 1
    assert ed["sections"]["0"]["variant"] == "sparse"
    assert ed["sections"]["1"]["crash"] is True
    # 序列化形状
    assert all(isinstance(n, dict) and {"start", "end", "pitch_midi"} <= set(n) for n in ed["notes"])
    # program 名
    assert ed["program"] == "drums"
    bass = next(s for s in specs if s["role"] == "synth_bass")
    assert bass["name"] == "合成贝斯" and bass["program"] == "synth_bass"


def test_tracks_from_specs_roundtrip_and_candidate():
    lib = load_patterns(PACK)
    facts = _facts_68()
    plan = default_plan(lib, facts)
    specs = assemble_tracks(lib, facts, plan, melody_notes=[])
    tracks = tracks_from_specs(specs)
    assert len(tracks) == len(specs)
    assert sum(len(t.notes) for t in tracks) == sum(s["n_notes"] for s in specs)
    score = _score_68()
    cand = candidate_score(score, specs)
    assert len(cand.tracks) == len(score.tracks) + len(specs)
    assert len(score.tracks) == 1  # 原分数未动


# ---------------------------------------------------------------------------
# 批次落盘
# ---------------------------------------------------------------------------


def test_generate_batch_default_path_writes_store():
    root = _tmp_root()
    try:
        score = _score_68()
        batch = generate_batch(root, score, pack=PACK, key="", name="proj")  # key="" → 全默认（不碰网络）
        assert batch["stats"]["source"] == "default"
        assert batch["stats"]["tracks"] >= 4 and batch["stats"]["notes"] > 0
        # 批次文件
        got = store.load_batch(root, batch["batch_ts"])
        assert got["pack"] == PACK and got["state"] == "pending"
        assert len(got["tracks"]) == batch["stats"]["tracks"]
        # 事件
        events = store.load_events(root, type_="generate")
        assert len(events) == 1 and events[0]["batch_ts"] == batch["batch_ts"]
        # 摘要列表
        lst = store.list_batches(root)
        assert lst and lst[0]["batch_ts"] == batch["batch_ts"] and lst[0]["n_notes"] == batch["stats"]["notes"]
        # facts 快照在（含段表）
        assert got["facts"]["sections"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_generate_batch_no_melody_raises():
    root = _tmp_root()
    try:
        score = Score(title="t", tempo=120.0, tracks=[Track(name="melody")])
        try:
            generate_batch(root, score, pack=PACK, key="")
            raised = False
        except ValueError:
            raised = True
        assert raised
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 采纳命令（真执行：EditBatch）
# ---------------------------------------------------------------------------


def test_build_arrange_commands_executes_on_editbatch():
    score = _score_68()
    old_bass = Track(name="合成贝斯", instrument=Instrument(backend="fluidsynth", program="synth_bass"),
                     notes=[Note(start=0.0, end=0.5, pitch_midi=40, pitch_hz=midi_to_hz(40), velocity=0.5)])
    score.tracks.append(old_bass)  # 旧产物轨（重跑场景）
    lib = load_patterns(PACK)
    facts = _facts_68()
    plan = default_plan(lib, facts)
    specs = assemble_tracks(lib, facts, plan, melody_notes=[])
    plan_cmds = build_arrange_commands(list(score.tracks), specs=specs, anchor_track=0)
    # 旧「合成贝斯」被列入删除
    assert 1 in plan_cmds["meta"]["removed"]

    eb = EditBatch(label="配器进工程")
    for c in plan_cmds["commands"]:
        eb.add(str(c["op"]), track=int(c.get("track", 0)), value=c.get("value"))
    new_score, result = eb.apply(score)
    assert result.ok, result.errors
    names = [t.name for t in new_score.tracks]
    assert names[0] == "melody"
    # 旧轨删除、新轨恰一条同名
    assert names.count("合成贝斯") == 1
    # 新轨在 melody 正下方起
    assert names[1:1 + len(specs)] == [s["name"] for s in specs]
    # program / notes / pan 落地
    bass = new_score.tracks[names.index("合成贝斯")]
    assert bass.instrument.program == "synth_bass"
    assert len(bass.notes) == next(s["n_notes"] for s in specs if s["role"] == "synth_bass")
    drums = new_score.tracks[names.index("电子鼓组")]
    assert drums.instrument.program == "drums"
    assert drums.notes and all(n.velocity <= 1.0 for n in drums.notes)
    guitar = new_score.tracks[names.index("电吉他")]
    assert guitar.pan < 0  # e_guitar pan=-0.35 落轨


def test_build_arrange_commands_rejects_dup_names():
    score = _score_68()
    specs = [{"role": "a", "name": "X", "notes": []}, {"role": "b", "name": "X", "notes": []}]
    try:
        build_arrange_commands(list(score.tracks), specs=specs, anchor_track=0)
        raised = False
    except ValueError:
        raised = True
    assert raised
