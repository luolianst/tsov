"""M-V8 E4 段2 单测：调参域 REST（analyze / suggest / apply / preview / discard / file / staging）。

重活全部打桩（facts 渲染 / LLM / 音频渲染 / LUFS），只验证路由编排与真值落盘：
- apply → score.json 变化 + 对拍报告 + .tsov-state.json 处置 + events.jsonl + agents.md 历史 + 用户统计
- preview → 工程零改动；file 防穿越；discard 不动工程；staging 列表出现 tune 条目（producer）
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import tsov.webapp.routes.tune as tune_routes
from tsov import agents as agents_mod
from tsov.tune import store
from tsov.web import create_app

from unit._cleanup import rmtree_force
from unit._tune_fixtures import facts_stub


@pytest.fixture()
def env(monkeypatch):
    d = Path("output") / f"tuneweb-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    up = Path("output") / f"tuneuser-{uuid.uuid4().hex[:10]}.md"
    monkeypatch.setattr(agents_mod, "user_agents_path", lambda: up)
    app = create_app(output_dir=d)
    client = TestClient(app)
    try:
        yield {"dir": d, "app": app, "client": client, "user_md": up}
    finally:
        rmtree_force(d)
        if up.exists():
            up.unlink()


def _score_payload(vol=0.8):
    return {"title": "t", "tempo": 120.0, "time_signature": "4/4",
            "key_candidates": [{"key": "C major", "confidence": 0.7}],
            "tracks": [
                {"name": "piano", "instrument": {"backend": "fluidsynth", "program": "0",
                                                 "volume": vol, "effects": []},
                 "notes": [{"start": 0.0, "end": 0.5, "pitch_midi": 60, "pitch_hz": 261.6,
                            "velocity": 0.8, "confidence": 0.8, "deviation_cents": 0.0,
                            "is_ornament": False}]},
                {"name": "drums", "instrument": {"backend": "fluidsynth", "program": "",
                                                 "volume": 1.0, "effects": []},
                 "notes": [{"start": 0.0, "end": 0.5, "pitch_midi": 48, "pitch_hz": 130.8,
                            "velocity": 0.8, "confidence": 0.8, "deviation_cents": 0.0,
                            "is_ornament": False}]},
            ], "meta": {}}


def _make_project(env, name="p1", vol=0.8):
    r = env["client"].post("/api/projects", json={"name": name, "score": _score_payload(vol)})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    return name


def _facts(vol=0.8):
    return facts_stub([
        {"index": 0, "name": "piano", "rel_db": -10.0, "volume": vol},
        {"index": 1, "name": "drums", "rel_db": 0.0},
    ], targets={0: -4.0, 1: 0.0}, mix_peak_dbfs=-2.0)


def _after_vol(vol, delta=6.0):
    return round(min(2.0, max(0.0, vol * 10 ** (-delta / 20))), 4)


def _batch(ts="20260926-999999", vol=0.8):
    return {"batch_ts": ts, "project": "p1", "pack": None, "created_at": 1.0,
            "state": "pending",
            "suggestions": [{"id": "t1", "source": "det", "kind": "level", "track": 0,
                             "title": "调平 piano", "reason": "r",
                             "values": {"delta_db": 6.0, "raw_diff_db": -6.0,
                                        "target_rel_db": -4.0, "measured_rel_db": -10.0,
                                        "before_volume": vol, "after_volume": _after_vol(vol),
                                        "capped": False},
                             "evidence": {"refs": [], "text": ""}}],
            "facts": _facts(vol),
            "stats": {"det": 1, "llm": 0, "dropped": 0, "llm_errors": [], "llm_attempts": 0}}


def _fake_mix_factory(log=None):
    def fake_mix(engine, proj_, score, out, cache=None):
        Path(out).write_bytes(b"RIFF0000")
        if log is not None:
            log["preview_vol"] = float(score.tracks[0].instrument.volume)
    return fake_mix


# ---------------------------------------------------------------------------
# analyze / suggest
# ---------------------------------------------------------------------------


def test_analyze_and_suggest_endpoints(env, monkeypatch):
    name = _make_project(env)
    monkeypatch.setattr(tune_routes, "build_facts", lambda *a, **k: _facts())
    r = env["client"].post(f"/api/projects/{name}/tune/analyze", json={"pack": None})
    assert r.status_code == 200 and r.json()["facts"]["levels"]["anchor"] == 0

    monkeypatch.setattr(tune_routes.suggest_mod, "generate_batch", lambda root, score, **kw: _batch())
    r2 = env["client"].post(f"/api/projects/{name}/tune/suggest", json={})
    assert r2.status_code == 200
    assert r2.json()["batch"]["suggestions"][0]["id"] == "t1"


def test_analyze_error_maps_400(env, monkeypatch):
    name = _make_project(env)

    def boom(*a, **k):
        raise ValueError("风格包不存在：nope")

    monkeypatch.setattr(tune_routes, "build_facts", boom)
    r = env["client"].post(f"/api/projects/{name}/tune/analyze", json={"pack": "nope"})
    assert r.status_code == 400 and "风格包不存在" in r.json()["error"]


# ---------------------------------------------------------------------------
# apply（编排全程）
# ---------------------------------------------------------------------------


def test_apply_full_chain(env, monkeypatch):
    name = _make_project(env, vol=0.8)
    proj = env["app"].state.tsov.get_project(name)
    store.save_batch(proj.root, _batch(vol=0.8))
    monkeypatch.setattr(tune_routes, "build_facts", lambda *a, **k: _facts())
    monkeypatch.setattr(tune_routes, "_render_mix", _fake_mix_factory())
    monkeypatch.setattr(tune_routes.measure, "measure_lufs", lambda p: -14.0)
    monkeypatch.setattr(tune_routes, "_distill_history", lambda *a, **k: None)

    r = env["client"].post(f"/api/projects/{name}/tune/apply",
                           json={"batch_ts": "20260926-999999"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["n"] == 1 and body["commands"] == 1 and body["applied"] == ["t1"]

    rep = body["report"]
    assert rep["status"] in ("ok", "warn")
    assert any("峰值" in i["text"] for i in rep["items"])
    assert rep["details"]["lufs"] == {"before": -14.0, "after": -14.0}
    assert rep["files"] == {"before": "before.wav", "after": "after.wav"}

    # score.json 真已落盘（差量语义：0.8 × 10^(−6/20)）
    s = env["client"].get(f"/api/projects/{name}/state").json()
    got = float(s["score"]["tracks"][0]["instrument"]["volume"])
    assert abs(got - _after_vol(0.8)) < 1e-6

    # 处置记录 + 批次回填 + 事件记账
    rec = json.loads((proj.root / ".tsov-state.json").read_text(encoding="utf-8"))
    assert rec["staging"]["records"]["tune:20260926-999999"]["state"] == "adopted"
    b = store.load_batch(proj.root, "20260926-999999")
    assert b["state"] == "adopted" and b["applied"] == ["t1"] and b["report"]["status"] == rep["status"]
    types = [e["type"] for e in store.load_events(proj.root)]
    assert "apply" in types

    # agents.md 历史（公式兜底）+ 用户统计（确定性累计）
    md = (proj.root / "agents.md").read_text(encoding="utf-8")
    assert "调参应用（20260926-999999）" in md and "应用 1 条建议" in md
    umd = env["user_md"].read_text(encoding="utf-8")
    assert "累计采纳建议：1 条（电平 1 / 声像 0 / 效果 0）" in umd

    # staging 列表：tune 条目（producer=tune，已采纳）
    lst = env["client"].get(f"/api/projects/{name}/staging").json()
    it = next(i for i in lst["items"] if i["id"] == "tune:20260926-999999")
    assert it["producer"] == "tune" and it["state"] == "adopted" and it["ready"] is True


def test_apply_empty_selection_400(env):
    name = _make_project(env)
    proj = env["app"].state.tsov.get_project(name)
    store.save_batch(proj.root, _batch())
    r = env["client"].post(f"/api/projects/{name}/tune/apply",
                           json={"batch_ts": "20260926-999999", "ids": ["zz"]})
    assert r.status_code == 400 and "没有可应用" in r.json()["error"]


def test_apply_missing_batch_404(env):
    name = _make_project(env)
    r = env["client"].post(f"/api/projects/{name}/tune/apply", json={"batch_ts": "nope"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# preview / discard / file / list / get
# ---------------------------------------------------------------------------


def test_preview_endpoint_zero_touch(env, monkeypatch):
    name = _make_project(env, vol=0.8)
    proj = env["app"].state.tsov.get_project(name)
    store.save_batch(proj.root, _batch(vol=0.8))
    log: dict = {}
    monkeypatch.setattr(tune_routes, "_render_mix", _fake_mix_factory(log))

    r = env["client"].post(f"/api/projects/{name}/tune/preview",
                           json={"batch_ts": "20260926-999999", "id": "t1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file"] == "preview-t1.wav" and "file=preview-t1.wav" in body["url"]
    assert abs(body["values"]["after_volume"] - _after_vol(0.8)) < 1e-6

    s = env["client"].get(f"/api/projects/{name}/state").json()
    assert float(s["score"]["tracks"][0]["instrument"]["volume"]) == 0.8   # 工程零改动


def test_preview_bad_id_404(env):
    name = _make_project(env)
    proj = env["app"].state.tsov.get_project(name)
    store.save_batch(proj.root, _batch())
    r = env["client"].post(f"/api/projects/{name}/tune/preview",
                           json={"batch_ts": "20260926-999999", "id": "zz"})
    assert r.status_code == 404


def test_discard_and_staging_list(env):
    name = _make_project(env)
    proj = env["app"].state.tsov.get_project(name)
    store.save_batch(proj.root, _batch(ts="20260926-888888"))
    r = env["client"].post(f"/api/projects/{name}/tune/discard",
                           json={"batch_ts": "20260926-888888"})
    assert r.status_code == 200
    lst = env["client"].get(f"/api/projects/{name}/staging").json()
    it = next(i for i in lst["items"] if i["id"] == "tune:20260926-888888")
    assert it["state"] == "discarded"
    assert store.load_batch(proj.root, "20260926-888888")["state"] == "discarded"
    assert [e["type"] for e in store.load_events(proj.root)].count("discard") == 1


def test_file_endpoint_guard(env):
    name = _make_project(env)
    proj = env["app"].state.tsov.get_project(name)
    store.save_batch(proj.root, _batch(ts="20260926-777777"))
    bdir = proj.root / "tune" / "20260926-777777"
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "before.wav").write_bytes(b"RIFF123")
    r = env["client"].get(f"/api/projects/{name}/tune/20260926-777777/file",
                          params={"file": "before.wav"})
    assert r.status_code == 200 and r.content == b"RIFF123"
    r2 = env["client"].get(f"/api/projects/{name}/tune/20260926-777777/file",
                           params={"file": "../../score.json"})
    assert r2.status_code == 400
    r3 = env["client"].get(f"/api/projects/{name}/tune/nope/file", params={"file": "x.wav"})
    assert r3.status_code == 404


def test_list_and_get_endpoints(env):
    name = _make_project(env)
    proj = env["app"].state.tsov.get_project(name)
    store.save_batch(proj.root, _batch(ts="20260926-666666"))
    lst = env["client"].get(f"/api/projects/{name}/tune/list").json()
    assert lst["batches"][0]["batch_ts"] == "20260926-666666"
    assert lst["batches"][0]["n_suggestions"] == 1
    got = env["client"].get(f"/api/projects/{name}/tune/20260926-666666").json()
    assert got["batch"]["suggestions"][0]["id"] == "t1"
    r404 = env["client"].get(f"/api/projects/{name}/tune/nope")
    assert r404.status_code == 404
