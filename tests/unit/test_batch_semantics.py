"""P62b-6（C4+W9）批语义锁：「实质变更门」与「拒绝语义契约」。

- C4：副作用型 op（open_instrument_editor）成功但谱面无变化 → 不落盘 / 不记快照；
  正常编辑 op → 照旧落盘 + 记快照（负控）。
  判别性：修复前 journal 会多记一条「无改动」快照（entries+1 / index.json 落盘）且响应无 changed 键。
- W9：/batch 拒绝语义 = HTTP 200 + applied:0 + errors（不做 4xx reject）；
  副作用型 op 经路由 → applied>0 且 changed:false，且不广播（saved_at 不塞）。
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tsov.core.notes import Note
from tsov.core.score import Instrument, Score, Track
from tsov.host import EditBatch, Project, vst_worker
from tsov.web import create_app

from unit._cleanup import rmtree_force

VST3 = "vst3:C:/fake/MT-PowerDrumKit.vst3"


@pytest.fixture()
def ws():
    d = Path("output") / f"p62b6-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    rmtree_force(d)


def _note():
    return Note(start=0.0, end=0.5, pitch_midi=36, pitch_hz=73.42, velocity=0.9)


def _score(program: str = VST3) -> Score:
    return Score(tempo=120.0,
                 tracks=[Track(name="mt", instrument=Instrument(program=program), notes=[_note()])])


def _app(env_dir: Path) -> TestClient:
    return TestClient(create_app(output_dir=env_dir))


def _mk_project(root: Path, name: str, score: dict) -> Path:
    pdir = root / name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "score.json").write_text(json.dumps(score, ensure_ascii=False), encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=pdir, check=True)
    subprocess.run(["git", "config", "user.email", "p62b6@local"], cwd=pdir, check=True)
    subprocess.run(["git", "config", "user.name", "p62b6"], cwd=pdir, check=True)
    return pdir


# ---------------------------------------------------------------------------
# C4：实质变更门（Project 层）
# ---------------------------------------------------------------------------


def test_c4_side_effect_op_skips_persist(ws, monkeypatch):
    """副作用型 op：applied>0 但谱面无变化 → score.json / journal 零写入。"""
    monkeypatch.setattr(vst_worker, "open_editor", lambda p, sr=44100: {"opened": True, "path": p})
    p = Project.create("c4a", _score(), parent=ws)
    sp = p.root / "score.json"
    before, mt0 = sp.read_bytes(), sp.stat().st_mtime_ns
    j0 = len(p.journal.entries)
    out = p.apply_batch(EditBatch(label="打开插件界面").add("open_instrument_editor", track=0))
    assert out["ok"] and out["applied"] == 1 and out.get("changed") is False
    assert out["seq"] is None
    assert sp.read_bytes() == before, "副作用型 op 改写了 score.json 内容"
    assert sp.stat().st_mtime_ns == mt0, "副作用型 op 触发了 score.json 重写（mtime 变化）"
    assert len(p.journal.entries) == j0, "副作用型 op 记了无改动快照（journal 条目 +1）"


def test_c4_real_edit_still_persists(ws):
    """负控：正常编辑 op（谱面变化）→ 照旧落盘 + 记快照 + changed=True（不误伤）。"""
    p = Project.create("c4b", _score("piano"), parent=ws)
    j0 = len(p.journal.entries)
    out = p.apply_batch(EditBatch(label="+2").add("transpose", value=2))
    assert out["ok"] and out["applied"] == 1 and out.get("changed") is True
    assert out["seq"] and len(p.journal.entries) == j0 + 1
    assert p.score.tracks[0].notes[0].pitch_midi == 38
    assert (p.root / "score.json").exists()


def test_c4_reject_path_changed_false(ws):
    """全拒路径同样带 changed:false（拒绝 ≠ 变更）。"""
    p = Project.create("c4c", _score("piano"), parent=ws)
    out = p.apply_batch(EditBatch(label="x").add("set_pitch", index=9, value=60))
    assert out["applied"] == 0 and out.get("changed") is False and out["errors"]


# ---------------------------------------------------------------------------
# W9：路由层契约锁（200 + applied:0；副作用 op changed:false 不广播）
# ---------------------------------------------------------------------------


def test_w9_batch_reject_semantics_200_applied0(ws):
    """全拒 batch：HTTP 200 + applied:0 + errors 非空 + 工程文件零变化（契约锁——不做 4xx）。"""
    _mk_project(ws, "w9a", _score("piano").to_dict())
    client = _app(ws)
    sp = ws / "w9a" / "score.json"
    before, mt0 = sp.read_bytes(), sp.stat().st_mtime_ns
    r = client.post("/api/projects/w9a/batch",
                    json={"label": "坏批", "commands": [{"op": "set_pitch", "index": 99, "value": 64}]})
    assert r.status_code == 200, r.text          # 拒绝不做 4xx（消费方按 applied 判定）
    body = r.json()
    assert body["applied"] == 0 and body["errors"] and body["ok"] is False
    assert sp.read_bytes() == before and sp.stat().st_mtime_ns == mt0


def test_w9_side_effect_op_route_changed_false_no_publish(ws, monkeypatch):
    """副作用型 op 经路由：applied>0 + changed:false + 零落盘 / 零快照 / 不广播（无 saved_at）。"""
    monkeypatch.setattr(vst_worker, "open_editor", lambda p, sr=44100: {"opened": True, "path": p})
    _mk_project(ws, "w9b", _score().to_dict())
    client = _app(ws)
    sp = ws / "w9b" / "score.json"
    before, mt0 = sp.read_bytes(), sp.stat().st_mtime_ns
    r = client.post("/api/projects/w9b/batch",
                    json={"label": "打开插件界面", "commands": [{"op": "open_instrument_editor", "track": 0}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] == 1 and body.get("changed") is False
    assert "saved_at" not in body                 # 不广播路径：saved_at 不塞（前端徽章不抖）
    assert sp.read_bytes() == before and sp.stat().st_mtime_ns == mt0
    assert not (ws / "w9b" / ".agent-actions" / "index.json").exists(), "副作用型 op 记了无改动快照"
