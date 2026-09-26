"""M-V8 E4 段3 单测：配器域 REST（generate / packs / list / get / preview / apply / discard / file / staging）。

重活打桩与否分线：
- generate：真跑（仅打断 decide.resolve_api_key → 离线全默认计划），验证批次真值落盘
- preview：打桩 _render_mix（不真渲染），验证候选 Score 与工程零改动
- apply：真跑命令层事务（EditBatch），验证 score.json 变化 + 幂等 + 记账 + 暂存处置
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import tsov.arrange.decide as decide_mod
import tsov.webapp.routes.arrange as arrange_routes
from tsov.arrange import store
from tsov.web import create_app

from unit._cleanup import rmtree_force


@pytest.fixture()
def env(monkeypatch):
    d = Path("output") / f"arrangeweb-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(decide_mod, "resolve_api_key", lambda *a, **k: "")  # 离线：全默认计划
    app = create_app(output_dir=d)
    client = TestClient(app)
    try:
        yield {"dir": d, "app": app, "client": client}
    finally:
        rmtree_force(d)


def _score_payload():
    notes = [{"start": round(0.2 + i * 0.5, 3), "end": round(0.6 + i * 0.5, 3),
              "pitch_midi": 62 + (i % 8), "pitch_hz": 300.0, "velocity": 0.7,
              "confidence": 0.9, "deviation_cents": 0.0, "is_ornament": False} for i in range(9)]
    return {"title": "t68", "tempo": 120.0, "time_signature": "6/8",
            "key_candidates": [{"key": "D major", "confidence": 0.8}],
            "tracks": [{"name": "melody", "instrument": {"backend": "fluidsynth", "program": "piano",
                                                         "volume": 0.8, "effects": []},
                        "notes": notes}],
            "meta": {}}


def _make_project(env, name="p1"):
    r = env["client"].post("/api/projects", json={"name": name, "score": _score_payload()})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    return name


def _fake_mix(log=None):
    def fake_mix(engine, proj_, score, out, cache=None):
        Path(out).write_bytes(b"RIFF0000")
        if log is not None:
            log["tracks"] = len(score.tracks)
    return fake_mix


# ---------------------------------------------------------------------------
# generate / packs / list / get
# ---------------------------------------------------------------------------


def test_generate_packs_list_get(env):
    name = _make_project(env)
    # packs 目录
    packs = env["client"].get(f"/api/projects/{name}/arrange/packs").json()
    assert [p["pack"] for p in packs["packs"]] == ["edm-electro-4-4", "pop-band-standard", "wotaiko-fast-6-8"]
    # generate（真跑，离线默认计划）
    r = env["client"].post(f"/api/projects/{name}/arrange/generate",
                           json={"pack": "wotaiko-fast-6-8"})
    assert r.status_code == 200, r.text
    batch = r.json()["batch"]
    assert batch["stats"]["source"] == "default"
    assert batch["stats"]["tracks"] >= 3, batch["stats"]
    ts = batch["batch_ts"]
    # 合成段（无书签 → 整曲单一 full 段）
    secs = batch["facts"]["sections"]
    assert len(secs) == 1 and secs[0]["kind"] == "full" and secs[0]["synthetic"] is True
    # 列表 / 单取
    lst = env["client"].get(f"/api/projects/{name}/arrange/list").json()["batches"]
    assert lst and lst[0]["batch_ts"] == ts and lst[0]["n_tracks"] == batch["stats"]["tracks"]
    got = env["client"].get(f"/api/projects/{name}/arrange/{ts}").json()["batch"]
    assert got["pack"] == "wotaiko-fast-6-8" and got["state"] == "pending"
    # 批次文件真落盘
    proj = env["app"].state.tsov.get_project(name)
    assert (proj.root / "arrange" / f"{ts}.json").is_file()
    # 暂存区出现 arrange 条目
    items = env["client"].get(f"/api/projects/{name}/staging").json()["items"]
    it = next(i for i in items if i["id"] == f"arrange:{ts}")
    assert it["producer"] == "arrange" and it["state"] == "pending" and it["ready"] is True


def test_generate_unknown_pack_404(env):
    name = _make_project(env)
    r = env["client"].post(f"/api/projects/{name}/arrange/generate", json={"pack": "nope"})
    assert r.status_code == 404


def test_generate_missing_pack_400(env):
    name = _make_project(env)
    r = env["client"].post(f"/api/projects/{name}/arrange/generate", json={})
    assert r.status_code == 400 and "pack" in r.json()["error"]


# ---------------------------------------------------------------------------
# preview（候选混音；工程零改动）
# ---------------------------------------------------------------------------


def test_preview_zero_touch(env, monkeypatch):
    name = _make_project(env)
    batch = env["client"].post(f"/api/projects/{name}/arrange/generate",
                               json={"pack": "wotaiko-fast-6-8"}).json()["batch"]
    ts = batch["batch_ts"]
    log: dict = {}
    monkeypatch.setattr(arrange_routes, "_render_mix", _fake_mix(log))
    r = env["client"].post(f"/api/projects/{name}/arrange/preview", json={"batch_ts": ts})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file"] == "preview-mix.wav" and "file=preview-mix.wav" in body["url"]
    # 渲染的候选 = 原轨 + 配器轨
    assert log["tracks"] == 1 + batch["stats"]["tracks"]
    # 工程零改动
    st = env["client"].get(f"/api/projects/{name}/state").json()
    assert len(st["score"]["tracks"]) == 1
    # 事件记账
    proj = env["app"].state.tsov.get_project(name)
    assert any(e["type"] == "preview" for e in store.load_events(proj.root))


# ---------------------------------------------------------------------------
# apply（真命令层事务）
# ---------------------------------------------------------------------------


def test_apply_lands_tracks_and_is_idempotent(env):
    name = _make_project(env)
    batch = env["client"].post(f"/api/projects/{name}/arrange/generate",
                               json={"pack": "wotaiko-fast-6-8"}).json()["batch"]
    ts = batch["batch_ts"]
    r = env["client"].post(f"/api/projects/{name}/arrange/apply", json={"batch_ts": ts})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["n_tracks"] == batch["stats"]["tracks"]
    st = env["client"].get(f"/api/projects/{name}/state").json()
    names = [t["name"] for t in st["score"]["tracks"]]
    assert names[0] == "melody"
    assert len(names) == 1 + batch["stats"]["tracks"]
    # 新轨程序/音符落地
    drums_i = names.index("电子鼓组")
    assert st["score"]["tracks"][drums_i]["instrument"]["program"] == "drums"
    assert st["score"]["tracks"][drums_i]["notes"]
    # 幂等：再 apply 一次（重跑先删同名旧产物轨）
    r2 = env["client"].post(f"/api/projects/{name}/arrange/apply", json={"batch_ts": ts})
    assert r2.status_code == 200, r2.text
    st2 = env["client"].get(f"/api/projects/{name}/state").json()
    names2 = [t["name"] for t in st2["score"]["tracks"]]
    assert len(names2) == 1 + batch["stats"]["tracks"]  # 不重复堆积
    assert names2.count("电子鼓组") == 1
    # 处置 + 批次回填 + 记账
    proj = env["app"].state.tsov.get_project(name)
    rec = json.loads((proj.root / ".tsov-state.json").read_text(encoding="utf-8"))
    assert rec["staging"]["records"][f"arrange:{ts}"]["state"] == "adopted"
    assert store.load_batch(proj.root, ts)["state"] == "adopted"
    assert [e["type"] for e in store.load_events(proj.root)].count("apply") == 2
    # 暂存区状态
    items = env["client"].get(f"/api/projects/{name}/staging").json()["items"]
    it = next(i for i in items if i["id"] == f"arrange:{ts}")
    assert it["state"] == "adopted"


def test_apply_missing_batch_404(env):
    name = _make_project(env)
    r = env["client"].post(f"/api/projects/{name}/arrange/apply", json={"batch_ts": "nope"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# discard / file
# ---------------------------------------------------------------------------


def test_generate_meter_mismatch_400(env):
    """拍号错配：4/4 包 × 6/8 谱 → 400 明确拒绝。"""
    name = _make_project(env)
    r = env["client"].post(f"/api/projects/{name}/arrange/generate",
                           json={"pack": "edm-electro-4-4"})
    assert r.status_code == 400 and "拍号不匹配" in r.json()["error"]


def test_discard_does_not_touch_score(env):
    name = _make_project(env)
    batch = env["client"].post(f"/api/projects/{name}/arrange/generate",
                               json={"pack": "wotaiko-fast-6-8"}).json()["batch"]
    ts = batch["batch_ts"]
    r = env["client"].post(f"/api/projects/{name}/arrange/discard", json={"batch_ts": ts})
    assert r.status_code == 200
    st = env["client"].get(f"/api/projects/{name}/state").json()
    assert len(st["score"]["tracks"]) == 1  # 工程未动
    proj = env["app"].state.tsov.get_project(name)
    assert store.load_batch(proj.root, ts)["state"] == "discarded"
    items = env["client"].get(f"/api/projects/{name}/staging").json()["items"]
    it = next(i for i in items if i["id"] == f"arrange:{ts}")
    assert it["state"] == "discarded"


def test_file_endpoint_guard(env, monkeypatch):
    name = _make_project(env)
    batch = env["client"].post(f"/api/projects/{name}/arrange/generate",
                               json={"pack": "wotaiko-fast-6-8"}).json()["batch"]
    ts = batch["batch_ts"]
    monkeypatch.setattr(arrange_routes, "_render_mix", _fake_mix())
    env["client"].post(f"/api/projects/{name}/arrange/preview", json={"batch_ts": ts})
    r = env["client"].get(f"/api/projects/{name}/arrange/{ts}/file", params={"file": "preview-mix.wav"})
    assert r.status_code == 200 and r.content == b"RIFF0000"
    r2 = env["client"].get(f"/api/projects/{name}/arrange/{ts}/file", params={"file": "../../score.json"})
    assert r2.status_code == 400
    r3 = env["client"].get(f"/api/projects/{name}/arrange/nope/file", params={"file": "x.wav"})
    assert r3.status_code == 404
