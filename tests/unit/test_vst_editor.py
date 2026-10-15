"""P62b 插件编辑器过桥单测（2026-10-14）：worker 命令流 / 单窗口锁 / 渲染互斥(Q3) / 关窗事件 / 命令层 op。

全部用假 daw / 假 worker——不在测试中开真实 GUI 窗口（真机过桥流程走验收脚本）。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from tsov.core.notes import Note
from tsov.core.score import Instrument, Score, Track
from tsov.host import vst_worker

from unit._cleanup import rmtree_force


@pytest.fixture()
def outdir():
    d = Path("output") / f"vstedit-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    rmtree_force(d)


class _FakeProc:
    def __init__(self, gate: threading.Event, fail: bool = False):
        self._gate = gate
        self._fail = fail

    def open_editor(self):
        self._gate.wait(timeout=10)
        if self._fail:
            raise RuntimeError("window boom")

    def get_latency_samples(self):
        return 0

    def clear_midi(self):
        pass

    def add_midi_note(self, *a):
        pass


class _FakeEngine:
    def __init__(self, gate: threading.Event, fail: bool = False):
        self.sr = 44100
        self._gate = gate
        self._fail = fail
        self._buf = np.zeros((2, 8), dtype=np.float32)

    def make_plugin_processor(self, name, path):
        return _FakeProc(self._gate, self._fail)

    def load_graph(self, g):
        pass

    def render(self, duration):
        n = max(1, int(duration * self.sr))
        self._buf = np.zeros((2, n), dtype=np.float32)
        self._buf[0, :] = 0.25

    def get_audio(self):
        return self._buf


def _state(engine: _FakeEngine) -> vst_worker._ChildState:
    daw = SimpleNamespace(RenderEngine=lambda sr, block: engine, __version__="fake")
    return vst_worker._ChildState(daw)


def _rmsg(path: str, out: Path) -> dict:
    return {"id": 7, "path": path, "sr": 44100, "n": 441, "events": [],
            "params": {}, "preset": None, "out": str(out)}


def test_child_editor_flow(outdir, monkeypatch):
    """打开 ack → 单窗口锁 → Q3 拒渲（该实例）/其他实例放行 → 主线程泵关窗 → 事件 → 恢复可渲。"""
    events: list[dict] = []
    monkeypatch.setattr(vst_worker, "_emit", lambda obj: events.append(obj))
    gate = threading.Event()
    st = _state(_FakeEngine(gate))
    a, b = "C:/fake/A.vst3", "C:/fake/B.vst3"

    r = st.handle("open_editor", {"path": a, "sr": 44100})
    assert r["opened"] is True and r["path"] == a
    assert st.handle("status", {})["editor"] == a

    with pytest.raises(RuntimeError):
        st.handle("open_editor", {"path": b, "sr": 44100})   # 单窗口锁

    with pytest.raises(RuntimeError):
        st._render(_rmsg(a, outdir / "a.npy"))               # Q3：编辑中拒渲（泵占主循环→一律拒）
    with pytest.raises(RuntimeError):
        st._render(_rmsg(b, outdir / "b.npy"))               # 其他实例同样拒（同进程主循环被占）

    gate.set()                                # 假窗放行（= 用户关窗）
    st._editor_pump(a)                        # 主循环在主线程的这个调用（同步返回）
    assert any(e.get("event") == "editor_closed" for e in events)
    assert st.handle("status", {})["editor"] is None

    ra = st._render(_rmsg(a, outdir / "a2.npy"))             # 关窗后恢复可渲
    assert ra["frames"] == 441 and ra["peak"] > 0


def test_child_editor_error_still_closes(outdir, monkeypatch):
    """窗口异常：editor_error + editor_closed 双事件；状态清空（不许卡死渲染门）。"""
    events: list[dict] = []
    monkeypatch.setattr(vst_worker, "_emit", lambda obj: events.append(obj))
    gate = threading.Event()
    st = _state(_FakeEngine(gate, fail=True))
    st.handle("open_editor", {"path": "C:/fake/X.vst3", "sr": 44100})
    gate.set()
    st._editor_pump("C:/fake/X.vst3")
    kinds = [e.get("event") for e in events]
    assert "editor_error" in kinds and "editor_closed" in kinds
    assert st.handle("status", {})["editor"] is None


def test_client_editor_state_and_render_fastfail():
    """客户端：同实例渲染快速失败（不拉进程）/ 单窗口开第二窗被拒 / 关窗事件清状态（不匹配保留）。"""
    w = vst_worker.VstWorker()
    ap = str(Path("C:/fake/A.vst3"))
    assert w.editor_open_path is None
    w._editor_open = ap
    with pytest.raises(vst_worker.VstWorkerError):
        w.render(ap, [], 44100, 441)
    with pytest.raises(vst_worker.VstWorkerError):
        w.render(str(Path("C:/fake/B.vst3")), [], 44100, 441)   # 其他实例同样拒（主循环被泵占）
    assert w.pid is None, "快速失败不应拉起 worker 子进程"
    with pytest.raises(vst_worker.VstWorkerError):
        w.open_editor(ap)      # 单窗口：已有窗口（未启 worker 即拒）
    assert w.pid is None
    w._on_editor_closed("C:/fake/OTHER.vst3")   # 路径不匹配 → 状态保留
    assert w.editor_open_path == ap
    w._on_editor_closed(ap)
    assert w.editor_open_path is None


def test_module_helpers(monkeypatch):
    monkeypatch.setattr(vst_worker, "_GLOBAL", None)
    monkeypatch.setenv("TSOV_VST_INPROC", "1")
    assert vst_worker.editor_status() == {"open": False, "path": None, "worker_pid": None}
    with pytest.raises(vst_worker.VstWorkerError):
        vst_worker.open_editor("C:/fake/A.vst3")   # INPROC 模式明确拒绝
    fake = SimpleNamespace(_editor_open="C:/x/A.vst3", worker_pid=123)
    monkeypatch.setattr(vst_worker, "_GLOBAL", fake)
    st = vst_worker.editor_status()
    assert st["open"] is True and st["path"] == "C:/x/A.vst3" and st["worker_pid"] == 123


def test_command_op_open_instrument_editor(monkeypatch):
    """命令层 op：vst3 轨→worker 调用（路径/采样率）/ 非 vst3 拒 / 越界拒（经 EditBatch 同径）。"""
    from tsov.host import command as cmd_mod

    calls: list[tuple] = []
    monkeypatch.setattr(vst_worker, "open_editor",
                        lambda p, sr=44100: (calls.append((p, sr)), {"opened": True})[1])
    s = Score(tempo=120.0, tracks=[Track(
        name="mt",
        instrument=Instrument(program=r"vst3:D:\VST3\MT-PowerDrumKit.vst3"),
        notes=[Note(start=0.0, end=0.5, pitch_midi=36, pitch_hz=73.42, velocity=0.9)])])
    _, res = cmd_mod.EditBatch(label="t").add("open_instrument_editor", track=0).apply(s)
    assert res.ok and res.applied == 1 and calls and calls[0][0] == r"D:\VST3\MT-PowerDrumKit.vst3"

    s2 = Score(tempo=120.0, tracks=[Track(
        name="p", notes=[Note(start=0.0, end=0.5, pitch_midi=60, pitch_hz=261.6)])])
    _, r2 = cmd_mod.EditBatch(label="t").add("open_instrument_editor", track=0).apply(s2)
    assert not r2.ok and "仅适用于" in r2.errors[0]

    _, r3 = cmd_mod.EditBatch(label="t").add("open_instrument_editor", track=9).apply(s)
    assert not r3.ok and "越界" in r3.errors[0]


def test_editor_epoch_bumps_and_stem_keys_change(monkeypatch):
    """P62b：关窗 → 插件代际 +1；stem 缓存键随代际变化（解锁必须失效重渲）。"""
    from tsov.host import cache as cache_mod

    w = vst_worker.VstWorker()
    ap = str(Path("C:/fake/A.vst3"))
    w._editor_open = ap
    w._on_editor_closed(ap)
    assert w._editor_epochs[ap] == 1
    w._on_editor_closed(ap)
    assert w._editor_epochs[ap] == 2

    tr = Track(name="mt", instrument=Instrument(program=r"vst3:C:/fake/A.vst3"),
               notes=[Note(start=0.0, end=0.5, pitch_midi=36, pitch_hz=73.42, velocity=0.9)])
    monkeypatch.setattr(vst_worker, "plugin_epoch", lambda p: 0)
    k0 = cache_mod.track_key(tr, 44100)
    monkeypatch.setattr(vst_worker, "plugin_epoch", lambda p: 3)
    k1 = cache_mod.track_key(tr, 44100)
    assert k0 != k1, "代际变化未失效缓存键（解锁后会命中静默 stem）"
    gm = Track(name="p", notes=[Note(start=0.0, end=0.5, pitch_midi=60, pitch_hz=261.6)])
    assert cache_mod.track_key(gm, 44100) == cache_mod.track_key(gm, 44100)


def test_plugin_epoch_helper(monkeypatch):
    monkeypatch.setattr(vst_worker, "_GLOBAL", None)
    assert vst_worker.plugin_epoch("C:/x/A.vst3") == 0
    fake = SimpleNamespace(_editor_epochs={str(Path("C:/x/A.vst3")): 2})
    monkeypatch.setattr(vst_worker, "_GLOBAL", fake)
    assert vst_worker.plugin_epoch("C:/x/A.vst3") == 2
    assert vst_worker.plugin_epoch("C:/x/B.vst3") == 0


def test_tool_open_instrument_editor_handler(monkeypatch, tmp_path):
    """agent 工具句柄 glue：score_path + track → 命令层 op → worker.open_editor（路径正确）。"""
    from tsov.agent.tools_compose import tool_open_instrument_editor

    calls: list[str] = []
    monkeypatch.setattr(vst_worker, "open_editor",
                        lambda p, sr=44100: (calls.append(p), {"opened": True})[1])
    s = Score(tempo=120.0, tracks=[Track(
        name="mt",
        instrument=Instrument(program=r"vst3:D:\VST3\MT-PowerDrumKit.vst3"),
        notes=[Note(start=0.0, end=0.5, pitch_midi=36, pitch_hz=73.42, velocity=0.9)])])
    p = tmp_path / "score.json"
    p.write_text(json.dumps(s.to_dict(), ensure_ascii=False), encoding="utf-8")
    msg = tool_open_instrument_editor({"score_path": str(p), "track": 0})
    assert "已打开插件界面" in msg and calls and calls[0] == r"D:\VST3\MT-PowerDrumKit.vst3"
