"""M-V8 E4 段1 单测：暂存区——探针 / 处置记录 / REST（list/adopt/discard/artifact）。

- 核心（tsov.staging）：探针形态（就绪/缺件/空目录跳过）；处置记录（.tsov-state.json 保留他键）；
  排序（pending 前→已处置后）；get_item
- REST：ready 校验 409；采纳走命令层（轨数 +2 / 幂等 already）；丢弃零残留（score.json sha 不变、
  run 目录保留）；artifact 守卫（200/400 穿越/404）
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tsov import staging
from tsov.web import create_app

from unit._cleanup import rmtree_force


def _notes(n: int) -> list[dict]:
    return [{"start": i * 0.5, "end": i * 0.5 + 0.4, "pitch_midi": 60 + i} for i in range(n)]


def _write_run(root: Path, run_ts: str = "20260101-000000", *, raw: int | None = 2,
               proc: int | None = 2, wavs: bool = True) -> Path:
    d = root / "chain" / run_ts
    d.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        (d / "03-voice.json").write_text(json.dumps({"notes": _notes(raw)}), encoding="utf-8")
    if proc is not None:
        (d / "05-notes-snapped.json").write_text(json.dumps({"notes": _notes(proc)}),
                                                 encoding="utf-8")
    if wavs:
        (d / "01-denoised.wav").write_bytes(b"RIFFx")
        (d / "02-loudnorm.wav").write_bytes(b"RIFFx")
    return d


# ---------------------------------------------------------------------------
# 核心：探针 / 记录 / 排序
# ---------------------------------------------------------------------------


def test_empty_project(tmp_path):
    r = staging.list_items(tmp_path)
    assert r["items"] == [] and r["counts"] == {"pending": 0, "adopted": 0, "discarded": 0}


def test_chain_item_shape(tmp_path):
    _write_run(tmp_path, "20260101-000000", raw=3, proc=4)
    _write_run(tmp_path, "empty-dir", raw=None, proc=None, wavs=False)  # 空 run 不列
    r = staging.list_items(tmp_path)
    assert len(r["items"]) == 1
    it = r["items"][0]
    assert it["id"] == "chain:20260101-000000" and it["producer"] == "chain"
    assert it["kind"] == "notes" and it["state"] == "pending" and it["ready"] is True
    assert it["refs"] == {"run_ts": "20260101-000000"}
    assert it["meta"]["notes_raw"] == 3 and it["meta"]["notes_processed"] == 4
    assert it["meta"]["audio"] == ["01-denoised.wav", "02-loudnorm.wav"]
    assert it["meta"]["missing"] == [] and it["created_at"] > 0


def test_chain_item_not_ready(tmp_path):
    _write_run(tmp_path, "20260102-000000", raw=2, proc=None, wavs=False)
    it = staging.list_items(tmp_path)["items"][0]
    assert it["ready"] is False and it["meta"]["missing"] == ["05-notes-snapped.json"]
    assert it["meta"]["notes_raw"] == 2 and it["meta"]["notes_processed"] is None


def test_records_persist_and_preserve_keys(tmp_path):
    (tmp_path / ".tsov-state.json").write_text(
        json.dumps({"last_commit": "abc123", "iter_counter": 7}), encoding="utf-8")
    _write_run(tmp_path, "20260103-000000")
    rec = staging.set_state(tmp_path, "chain:20260103-000000", "discarded", note="不合口味")
    assert rec["state"] == "discarded" and rec["note"] == "不合口味"
    data = json.loads((tmp_path / ".tsov-state.json").read_text(encoding="utf-8"))
    assert data["last_commit"] == "abc123" and data["iter_counter"] == 7
    assert data["staging"]["records"]["chain:20260103-000000"]["state"] == "discarded"
    # 列表反映处置态
    r = staging.list_items(tmp_path)
    assert r["counts"] == {"pending": 0, "adopted": 0, "discarded": 1}
    assert r["items"][0]["state"] == "discarded" and r["items"][0]["handled_at"] > 0


def test_sort_pending_first(tmp_path):
    _write_run(tmp_path, "20260101-000000")
    _write_run(tmp_path, "20260102-000000")
    staging.set_state(tmp_path, "chain:20260101-000000", "discarded")
    items = staging.list_items(tmp_path)["items"]
    assert [i["id"] for i in items] == ["chain:20260102-000000", "chain:20260101-000000"]
    assert items[0]["state"] == "pending" and items[1]["state"] == "discarded"


def test_get_item(tmp_path):
    _write_run(tmp_path, "20260101-000000")
    it = staging.get_item(tmp_path, "chain:20260101-000000")
    assert it is not None and it["ready"] is True
    assert staging.get_item(tmp_path, "chain:nope") is None
    assert staging.get_item(tmp_path, "tune:x") is None


def test_set_state_rejects_bad_value(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        staging.set_state(tmp_path, "chain:x", "pending")


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _make_project(env, name="p1"):
    r = env["client"].post("/api/projects", json={
        "name": name,
        "score": {
            "title": "t", "tempo": 120.0,
            "key_candidates": [{"key": "C major", "confidence": 0.7}],
            "tracks": [{"name": "melody",
                        "instrument": {"backend": "fluidsynth", "program": "piano",
                                       "volume": 0.8, "effects": []},
                        "notes": []}],
            "meta": {},
        }})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text


def _chain_json(root: Path, run_ts: str):
    (root / "chain.json").write_text(json.dumps(
        {"source_track": 0, "run_ts": run_ts, "preset": "humming-quicklane", "steps": []}),
        encoding="utf-8")


@pytest.fixture()
def env(monkeypatch):
    d = Path("output") / f"stagingtest-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("tsov.webapp.config.AGENT_SESSION_DIR", str(d / "agent-sessions"))
    app = create_app(output_dir=d)
    client = TestClient(app)
    try:
        yield {"dir": d, "app": app, "client": client}
    finally:
        rmtree_force(d)


# ---------------------------------------------------------------------------
# REST 用例
# ---------------------------------------------------------------------------


def test_rest_list_empty(env):
    _make_project(env)
    r = env["client"].get("/api/projects/p1/staging")
    assert r.status_code == 200
    body = r.json()
    assert body["project"] == "p1" and body["items"] == []
    assert body["counts"] == {"pending": 0, "adopted": 0, "discarded": 0}


def test_rest_adopt_flow_dual_tracks(env):
    """采纳 = 命令层事务（与 /chain/apply 同径）：轨数 +2 / 幂等 already / 记录 adopted。"""
    _make_project(env)
    root = env["dir"] / "p1"
    _write_run(root, "20990101-000000", raw=2, proc=2)
    _chain_json(root, "20990101-000000")
    client = env["client"]

    lst = client.get("/api/projects/p1/staging").json()
    assert lst["counts"]["pending"] == 1 and lst["items"][0]["ready"] is True
    item_id = lst["items"][0]["id"]

    a1 = client.post(f"/api/projects/p1/staging/{item_id}/adopt", json={})
    assert a1.status_code == 200, a1.text
    body = a1.json()
    assert body["ok"] is True and body["tracks"] == 3 and body["record"]["state"] == "adopted"
    proj = env["app"].state.tsov.get_project("p1")
    assert [t.name for t in proj.score.tracks] == ["melody", "melody", "melody · 处理"]
    assert [n.pitch_midi for n in proj.score.tracks[1].notes] == [60, 61]
    assert [n.pitch_midi for n in proj.score.tracks[2].notes] == [60, 61]

    lst2 = client.get("/api/projects/p1/staging").json()
    assert lst2["counts"] == {"pending": 0, "adopted": 1, "discarded": 0}
    assert lst2["items"][0]["state"] == "adopted"

    # 已采纳条目再点 → 幂等 already（不重跑装配）
    a2 = client.post(f"/api/projects/p1/staging/{item_id}/adopt", json={})
    assert a2.status_code == 200 and a2.json()["already"] is True


def test_rest_adopt_not_ready_409(env):
    _make_project(env)
    root = env["dir"] / "p1"
    _write_run(root, "20990101-000000", raw=2, proc=None, wavs=False)
    item_id = env["client"].get("/api/projects/p1/staging").json()["items"][0]["id"]
    r = env["client"].post(f"/api/projects/p1/staging/{item_id}/adopt", json={})
    assert r.status_code == 409 and "未跑完" in r.json()["error"]


def test_rest_discard_zero_residue(env):
    """丢弃 = 仅记处置：score.json sha256 不变、run 目录保留。"""
    _make_project(env)
    root = env["dir"] / "p1"
    run_dir = _write_run(root, "20990101-000000")
    _chain_json(root, "20990101-000000")
    client = env["client"]
    before = _sha(root / "score.json")

    item_id = client.get("/api/projects/p1/staging").json()["items"][0]["id"]
    d = client.post(f"/api/projects/p1/staging/{item_id}/discard")
    assert d.status_code == 200 and d.json()["record"]["state"] == "discarded"
    assert _sha(root / "score.json") == before
    assert (run_dir / "03-voice.json").is_file()
    lst = client.get("/api/projects/p1/staging").json()
    assert lst["counts"] == {"pending": 0, "adopted": 0, "discarded": 1}


def test_rest_item_404(env):
    _make_project(env)
    client = env["client"]
    assert client.post("/api/projects/p1/staging/chain:nope/adopt").status_code == 404
    assert client.post("/api/projects/p1/staging/chain:nope/discard").status_code == 404
    assert client.get("/api/projects/p1/staging/chain:nope/artifact?file=x.wav").status_code == 404


def test_rest_artifact_guards(env):
    _make_project(env)
    root = env["dir"] / "p1"
    _write_run(root, "20990101-000000")
    client = env["client"]
    item_id = "chain:20990101-000000"

    ok = client.get(f"/api/projects/p1/staging/{item_id}/artifact?file=02-loudnorm.wav")
    assert ok.status_code == 200 and ok.headers["content-type"].startswith("audio/wav")
    assert ok.content == b"RIFFx"

    trav = client.get(f"/api/projects/p1/staging/{item_id}/artifact?file=../chain.json")
    assert trav.status_code == 400
    miss = client.get(f"/api/projects/p1/staging/{item_id}/artifact?file=99-none.wav")
    assert miss.status_code == 404
