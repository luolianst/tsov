"""批L L1-3/L1-4：VST3 参数机制 / 预设机制测试。

覆盖：Instrument 增补字段（旧数据兼容）/ 命令层两 op / 参数表资产（生成 + 策展保留）/
worker 渲染（参数生效、严格失配、预设失配、state 往返）/ 工具面 / MCP 注册与端到端。
依赖 dawdreamer + vendor/vst3/Dexed.vst3 的用例缺料自动 skip。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import pytest

from tsov.agent.tools_compose import (
    tool_list_instrument_params,
    tool_load_instrument_preset,
    tool_set_instrument_param,
)
from tsov.core.notes import Note
from tsov.core.score import Instrument, Score, Track
from tsov.host.command import EditBatch

from unit._cleanup import rmtree_force
from unit._vst_helpers import assert_onset_stable

try:
    import dawdreamer  # noqa: F401

    _HAS_DAWDREAMER = True
except ImportError:  # pragma: no cover
    _HAS_DAWDREAMER = False

_DEXED = Path("vendor/vst3/Dexed.vst3")
_HAS_PLUGIN = _DEXED.is_dir()
_MAP = Path("presets/v3-maps/dexed.json")

_SR = 44100
_N = int(_SR * 0.6)

_NEED_VST = pytest.mark.skipif(not _HAS_DAWDREAMER or not _HAS_PLUGIN, reason="dawdreamer 或 vendor/vst3/Dexed 缺失")
_NEED_MAP = pytest.mark.skipif(not _MAP.is_file(), reason="参数表缺失（先跑 tsov vst3-map vendor/vst3/Dexed.vst3）")


def _note(s, e, p, v=0.8):
    return Note(start=s, end=e, pitch_midi=p, pitch_hz=261.63, velocity=v)


def _score(params=None, preset=None):
    return Score(title="t", tempo=100.0, tracks=[Track(
        name="dex",
        instrument=Instrument(program=f"vst3:{_DEXED.as_posix()}", params=params or {}, preset=preset),
        notes=[_note(0.2, 0.7, 60)])])


def _tmpdir(prefix: str) -> Path:
    d = Path("output") / f"{prefix}-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# 数据模型（批L 增补 + 旧数据兼容）
# ---------------------------------------------------------------------------


def test_instrument_schema_params_preset_roundtrip_and_legacy():
    ins = Instrument(program="vst3:x", params={"Cutoff": 0.5},
                     preset={"kind": "state", "path": "assets/presets/a.state"})
    d = ins.to_dict()
    assert d["params"] == {"Cutoff": 0.5} and d["preset"]["kind"] == "state"
    back = Instrument.from_dict(d)
    assert back.params == {"Cutoff": 0.5} and back.preset == ins.preset
    # 旧数据（无 params/preset 键）→ 缺省，行为同构
    legacy = {"backend": "fluidsynth", "program": "piano", "volume": 1.0, "effects": []}
    old = Instrument.from_dict(legacy)
    assert old.params == {} and old.preset is None
    assert Instrument.from_dict({**legacy, "preset": None}).preset is None
    # Score 全链往返（含 track）
    s = Score(title="t", tracks=[Track(name="a", instrument=ins)])
    s2 = Score.from_dict(s.to_dict())
    assert s2.tracks[0].instrument.params == {"Cutoff": 0.5}


# ---------------------------------------------------------------------------
# 命令层 op
# ---------------------------------------------------------------------------


def test_command_ops_reject_non_vst3_track():
    score = Score(title="t", tracks=[Track(name="p", instrument=Instrument(program="piano"))])
    _, res = EditBatch().add("set_instrument_param", track=0, value={"name": "Cutoff", "value": 0.5}).apply(score)
    assert not res.ok and "vst3" in res.errors[0]
    _, res = EditBatch().add("load_instrument_preset", track=0, value={"kind": "state", "path": "x"}).apply(score)
    assert not res.ok and "vst3" in res.errors[0]


@_NEED_MAP
def test_command_set_param_alias_range_clear():
    new, res = EditBatch().add("set_instrument_param", track=0,
                               value={"name": "滤波截止", "value": 0.5}).apply(_score())
    assert res.ok and new.tracks[0].instrument.params == {"Cutoff": 0.5}  # 别名 → 规范名
    _, res = EditBatch().add("set_instrument_param", track=0,
                             value={"name": "不存在X", "value": 0.5}).apply(_score())
    assert not res.ok and "参数不存在" in res.errors[0]
    _, res = EditBatch().add("set_instrument_param", track=0,
                             value={"name": "Cutoff", "value": 1.5}).apply(_score())
    assert not res.ok and "0..1" in res.errors[0]
    new, res = EditBatch().add("set_instrument_param", track=0,
                               value={"name": "Cutoff"}).apply(_score(params={"Cutoff": 0.5}))
    assert res.ok and new.tracks[0].instrument.params == {}  # 清除


def test_command_load_preset_set_bad_kind_clear():
    new, res = EditBatch().add("load_instrument_preset", track=0, value={
        "kind": "vstpreset", "path": "assets/presets/x.vstpreset"}).apply(_score())
    assert res.ok and new.tracks[0].instrument.preset == {
        "kind": "vstpreset", "path": "assets/presets/x.vstpreset"}
    _, res = EditBatch().add("load_instrument_preset", track=0,
                             value={"kind": "bad", "path": "x"}).apply(_score())
    assert not res.ok and "未知预设类型" in res.errors[0]
    new, res = EditBatch().add("load_instrument_preset", track=0,
                               value=None).apply(_score(preset={"kind": "state", "path": "a"}))
    assert res.ok and new.tracks[0].instrument.preset is None


# ---------------------------------------------------------------------------
# 参数表资产（plugin_maps）
# ---------------------------------------------------------------------------


@_NEED_MAP
def test_resolve_param_name_forms():
    from tsov.host.plugin_maps import resolve_param_name

    assert resolve_param_name(str(_DEXED), "Cutoff") == ("Cutoff", None)
    assert resolve_param_name(str(_DEXED), "滤波截止") == ("Cutoff", None)
    assert resolve_param_name(str(_DEXED), "0") == ("Cutoff", None)
    canon, err = resolve_param_name(str(_DEXED), "不存在X")
    assert canon is None and "参数不存在" in err
    # 无表 → 透传（渲染期严格校验兜底）
    assert resolve_param_name("no/such/plugin.vst3", "Whatever") == (None, None)


@_NEED_VST
def test_generate_map_schema_and_curation_survive_rescan():
    from tsov.host import plugin_maps

    d = _tmpdir("vstmaptest")
    try:
        p1 = plugin_maps.generate_map(str(_DEXED), out_dir=d)
        doc = json.loads(p1.read_text(encoding="utf-8"))
        assert doc["plugin"] == "Dexed" and doc["param_count"] > 100
        assert {"index", "name", "default", "discrete"} <= set(doc["params"][0])
        assert doc["generator"]["dawdreamer"]
        for e in doc["params"]:  # 模拟人工策展
            if e["name"] == "Cutoff":
                e["alias_zh"] = "测试别名"
        p1.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        doc2 = json.loads(plugin_maps.generate_map(str(_DEXED), out_dir=d).read_text(encoding="utf-8"))
        cut = next(e for e in doc2["params"] if e["name"] == "Cutoff")
        assert cut.get("alias_zh") == "测试别名"  # 重扫保留策展
    finally:
        rmtree_force(d)


# ---------------------------------------------------------------------------
# worker 渲染侧（参数生效 / 严格失配 / 预设失配 / state 往返）
# ---------------------------------------------------------------------------


@_NEED_VST
def test_worker_render_params_effective_and_strict_mismatch():
    from tsov.host import vst_worker

    w = vst_worker.VstWorker()
    try:
        ev = vst_worker.events_from_notes([_note(0.2, 0.7, 60)])
        a0 = w.render(str(_DEXED), ev, _SR, _N)
        a1 = w.render(str(_DEXED), ev, _SR, _N, params={"Cutoff": 0.1})
        assert float(np.abs(a1 - a0).max()) > 0.01, "参数应生效（渲染结果改变）"
        with pytest.raises(vst_worker.VstWorkerError, match="参数不存在"):
            w.render(str(_DEXED), ev, _SR, _N, params={"NoSuchParam": 0.1})
        a2 = w.render(str(_DEXED), ev, _SR, _N, params={"Cutoff": 0.1})  # 失配后照常
        assert_onset_stable(a1, a2, onsets=[0.2], sr=_SR)
    finally:
        w.shutdown()


@_NEED_VST
def test_worker_resets_baseline_between_specs():
    """批L：预设/参数组合变化（含回到空组合）→ worker 重建实例回确定起点，不带前次残留。"""
    from tsov.host import vst_worker

    ev = vst_worker.events_from_notes([_note(0.2, 0.5, 60)])
    w = vst_worker.VstWorker()
    w2 = vst_worker.VstWorker()
    try:
        a_param = w.render(str(_DEXED), ev, _SR, _N, params={"0": 0.1})
        a_plain_after = w.render(str(_DEXED), ev, _SR, _N)   # 空组合 → 必须回默认
        a_plain_fresh = w2.render(str(_DEXED), ev, _SR, _N)  # 全新 worker 的默认
        assert_onset_stable(a_plain_after, a_plain_fresh, onsets=[0.2], sr=_SR)
        assert float(np.abs(a_param - a_plain_after).max()) > 0.005  # 参数确实改变过声音
    finally:
        w.shutdown()
        w2.shutdown()


@_NEED_VST
def test_preset_state_roundtrip_and_mismatch(monkeypatch):
    from tsov.host import vst_worker
    from tsov.host.instrument import VST3Source

    d = _tmpdir("vstprestest")
    try:
        sp = d / "dex.state"
        monkeypatch.setenv("TSOV_VST_INPROC", "1")
        src = VST3Source(str(_DEXED), params={"Cutoff": 0.1})
        try:
            src._proc.save_state(str(sp))
        finally:
            src.close()
        monkeypatch.delenv("TSOV_VST_INPROC")

        w = vst_worker.VstWorker()
        try:
            ev = vst_worker.events_from_notes([_note(0.2, 0.7, 60)])
            a_params = w.render(str(_DEXED), ev, _SR, _N, params={"Cutoff": 0.1})
            a_state = w.render(str(_DEXED), ev, _SR, _N, preset={"kind": "state", "path": str(sp)})
            assert_onset_stable(a_state, a_params, onsets=[0.2], sr=_SR)  # state 往返等价
            with pytest.raises(vst_worker.VstWorkerError, match="预设加载失败"):
                w.render(str(_DEXED), ev, _SR, _N, preset={"kind": "state", "path": str(d / "nope.state")})
        finally:
            w.shutdown()
    finally:
        rmtree_force(d)


@_NEED_VST
def test_inproc_fallback_params_and_bad_preset(monkeypatch):
    from tsov.host.instrument import VST3Source

    monkeypatch.setenv("TSOV_VST_INPROC", "1")
    src = VST3Source(str(_DEXED), params={"Cutoff": 0.1})
    try:
        out = src.render([_note(0.2, 0.7, 60)], _SR, _N)
        assert float(np.abs(out).max()) > 0.005
    finally:
        src.close()
    with pytest.raises(RuntimeError, match="参数失配"):
        VST3Source(str(_DEXED), params={"NoSuchParam": 0.1}).close()
    with pytest.raises(RuntimeError, match="预设加载失败"):
        VST3Source(str(_DEXED), preset={"kind": "state", "path": "no/such.state"}).close()


@_NEED_VST
def test_session_wires_params_to_render():
    from tsov.host import HostEngine
    from tsov.render.fluidsynth_backend import default_soundfont

    if not default_soundfont():
        pytest.skip("缺 SoundFont（HostEngine 依赖）")
    e = HostEngine()
    s0 = e.load(_score())
    try:
        a0 = e.render(s0)
    finally:
        s0.close()
    s1 = e.load(_score(params={"Cutoff": 0.1}))
    try:
        a1 = e.render(s1)
    finally:
        s1.close()
    assert float(np.abs(a1 - a0).max()) > 0.01, "工程参数应经会话接线传到渲染"


# ---------------------------------------------------------------------------
# 工具面（agent 三个工具）
# ---------------------------------------------------------------------------


@_NEED_MAP
def test_tools_set_list_and_preset():
    d = _tmpdir("vsttooltest")
    try:
        sp = d / "score.json"
        sp.write_text(json.dumps(_score().to_dict(), ensure_ascii=False), encoding="utf-8")
        out = tool_set_instrument_param({"score_path": str(sp), "param": "0", "value": 0.25})
        assert "Cutoff" in out and "0.25" in out
        edited = d / "agent-edited-score.json"
        doc = json.loads(edited.read_text(encoding="utf-8"))
        assert doc["tracks"][0]["instrument"]["params"] == {"Cutoff": 0.25}
        _ = tool_load_instrument_preset(
            {"score_path": str(edited), "kind": "state", "path": "assets/presets/x.state"})
        doc2 = json.loads(edited.read_text(encoding="utf-8"))
        assert doc2["tracks"][0]["instrument"]["preset"] == {"kind": "state", "path": "assets/presets/x.state"}
        listing = tool_list_instrument_params({"plugin": str(_DEXED)})
        assert "滤波截止" in listing and "2238" in listing and "Cutoff" in listing
        q = tool_list_instrument_params({"plugin": str(_DEXED), "query": "OP1 F"})
        assert "OP1 F COARSE" in q
        ctx = tool_list_instrument_params({"score_path": str(edited)})
        assert "当前" in ctx  # 带工程上下文显示 diff
    finally:
        rmtree_force(d)


# ---------------------------------------------------------------------------
# MCP 面（注册 + 写工具端到端）
# ---------------------------------------------------------------------------


def test_mcp_registry_surface_includes_new_tools():
    from tsov.agent.tools import build_default_registry
    from tsov.mcp.server import READ_REGISTRY_TOOLS, WRITE_REGISTRY_TOOLS

    assert "list_instrument_params" in READ_REGISTRY_TOOLS
    assert {"set_instrument_param", "load_instrument_preset"} <= set(WRITE_REGISTRY_TOOLS)
    reg = build_default_registry()
    for nm in ("list_instrument_params", "set_instrument_param", "load_instrument_preset"):
        assert reg.get(nm) is not None


@_NEED_MAP
def test_mcp_write_tool_end_to_end():
    from mcp import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    from tsov.mcp.server import build_server
    from tsov.mcp.sessions import ProjectStore

    ws = _tmpdir("vstmcptest")
    try:
        root = ws / "output" / "vp"
        root.mkdir(parents=True)
        (root / "score.json").write_text(json.dumps(_score().to_dict(), ensure_ascii=False), encoding="utf-8")
        store = ProjectStore(ws, allow_write=True)

        async def case():
            server = build_server(store)
            async with create_client_server_memory_streams() as (cs, ss):
                task = asyncio.create_task(server.run(ss[0], ss[1], server.create_initialization_options()))
                try:
                    async with ClientSession(cs[0], cs[1]) as session:
                        await session.initialize()
                        tools = {t.name for t in (await session.list_tools()).tools}
                        assert "list_instrument_params" in tools and "set_instrument_param" in tools
                        r = await session.call_tool(
                            "set_instrument_param", {"name": "vp", "param": "滤波截止", "value": 0.3})
                        return r
                finally:
                    task.cancel()
                    try:
                        await task
                    except BaseException:  # noqa: BLE001 收尾吞掉取消噪声
                        pass

        r = asyncio.run(case())
        assert r.is_error is False, r.content[0].text if r.content else ""
        saved = json.loads((root / "score.json").read_text(encoding="utf-8"))
        assert saved["tracks"][0]["instrument"]["params"] == {"Cutoff": 0.3}
    finally:
        rmtree_force(ws)


@_NEED_VST
def test_vst3_track_freeze_cache_semantics():
    """L1-6：VST3 轨 freeze——首渲入库；重复渲染命中且逐位一致；改 params → 失效重渲；导出有产物。"""
    from tsov.host import HostEngine
    from tsov.host.cache import StemStore
    from tsov.host.mix import render_buses
    from tsov.render.fluidsynth_backend import default_soundfont

    state = Path("output/v02-batchL-verify/sfizz-vpo-alto-flute.state")
    if not state.is_file():
        pytest.skip("缺 sfizz 长笛 state（先跑 gate_sweep 生成）")
    if not Path("D:/VST3/sfizz.vst3").is_dir():
        pytest.skip("缺 D:/VST3/sfizz.vst3")
    if not default_soundfont():
        pytest.skip("缺 SoundFont（HostEngine 依赖）")
    d = _tmpdir("vstfreeze")
    try:
        score = Score(title="t", tempo=100.0, tracks=[Track(
            name="flute",
            instrument=Instrument(program="vst3:D:/VST3/sfizz.vst3",
                                  preset={"kind": "state", "path": str(state.resolve())}),
            notes=[_note(0.2, 1.0, 69)])])
        e = HostEngine()
        store = StemStore(d)

        s1 = e.load(score)
        try:
            st1 = {}
            a1 = render_buses(s1, samplerate=e.samplerate, cache=store, stats=st1)
        finally:
            s1.close()
        assert st1.get("rendered") == ["flute"] and not st1.get("cached")

        s2 = e.load(score)
        try:
            st2 = {}
            a2 = render_buses(s2, samplerate=e.samplerate, cache=store, stats=st2)
        finally:
            s2.close()
        assert st2.get("rendered", []) == [] and st2.get("cached") == ["flute"]
        assert np.array_equal(a1, a2)  # 缓存拼装 == 首渲

        score.tracks[0].instrument.params = {"0": 0.3}   # 改参 → 必须失效
        s3 = e.load(score)
        try:
            st3 = {}
            e.render(s3, cache=store, stats=st3)
        finally:
            s3.close()
        assert st3.get("rendered", []) == ["flute"], "改参后仍命中缓存 = 陈旧音色回放"

        s4 = e.load(score)
        try:
            rep = e.export(s4, d / "exports", midi=False, cache=store)
        finally:
            s4.close()
        assert Path(rep["mix"]).is_file()
    finally:
        rmtree_force(d)
