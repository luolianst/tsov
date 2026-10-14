"""批L L1-2：插件 worker 子进程（vst_worker）测试。

覆盖：常驻渲染 / 崩溃自动重启 + 无孤儿 / 超时保护 / 与进程内输出同口径 / 单例与环境开关。
依赖 vendor/vst3/Dexed.vst3 + dawdreamer（缺则整文件 skip）。
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from tsov.core.notes import Note
from tsov.host import vst_worker
from tsov.host.instrument import VST3Source

from unit._vst_helpers import assert_onset_stable

try:
    import dawdreamer  # noqa: F401

    _HAS_DAWDREAMER = True
except ImportError:  # pragma: no cover
    _HAS_DAWDREAMER = False

_DEXED = Path("vendor/vst3/Dexed.vst3")  # bundle 目录形态（worker 两种路径均可；此处顺带覆盖 bundle）
pytestmark = pytest.mark.skipif(
    not _HAS_DAWDREAMER or not _DEXED.is_dir(), reason="dawdreamer 或 vendor/vst3/Dexed 缺失"
)

_SR = 44100
_N = int(_SR * 0.6)


def _note(s, e, p, v=0.8):
    return Note(start=s, end=e, pitch_midi=p, pitch_hz=440.0 * 2 ** ((p - 69) / 12), velocity=v)


def _events(notes):
    return vst_worker.events_from_notes(notes)


def _pid_alive(pid: int) -> bool:
    """Windows 进程存活检测（OpenProcess + GetExitCodeProcess；非 Windows 恒 False）。"""
    if sys.platform != "win32" or not pid:
        return False
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(h)
    return bool(ok) and code.value == 259  # STILL_ACTIVE


def _wait_dead(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    return False


def test_worker_render_resident_and_shutdown_no_orphan():
    """常驻：load 一次多次渲染 + status 记账；shutdown 后壳/真身都不得残留。"""
    w = vst_worker.VstWorker()
    lp = rp = None
    try:
        info = w.load(str(_DEXED))
        assert info["in"] == 0 and info["out"] >= 1
        out = w.render(str(_DEXED), _events([_note(0.2, 0.7, 60)]), _SR, _N)
        assert out.shape == (_N,)
        assert float(np.abs(out).max()) > 0.005
        st = w.status()
        assert st["protocol"] == vst_worker.PROTOCOL and st["engine_sr"] == _SR
        assert any(Path(p["path"]).name == "Dexed.vst3" for p in st["plugins"])
        assert w.restarts == 0
        lp, rp = w.pid, w.worker_pid
        assert lp and rp
        if sys.platform == "win32":
            assert lp != rp, "venv launcher：壳/真身应为不同 pid"
    finally:
        w.shutdown()
    assert _wait_dead(lp) and _wait_dead(rp), "shutdown 后进程残留"


def test_worker_crash_auto_restart_reload_no_orphan():
    """崩溃（杀进程）→ 下次请求自动重启 + 惰性重载；旧壳/真身不得残留。"""
    w = vst_worker.VstWorker()
    try:
        out1 = w.render(str(_DEXED), _events([_note(0.2, 0.7, 60)]), _SR, _N)
        lp, rp = w.pid, w.worker_pid
        w.kill()  # 模拟插件崩溃
        assert w.pid is None
        assert _wait_dead(lp) and _wait_dead(rp), "kill 后进程残留（venv launcher 坑）"
        out2 = w.render(str(_DEXED), _events([_note(0.2, 0.7, 60)]), _SR, _N)  # 自动重启 + 重载
        assert w.restarts >= 1
        assert float(np.abs(out2).max()) > 0.005
        assert_onset_stable(out1, out2, onsets=[0.2], sr=_SR)
    finally:
        w.shutdown()


def test_worker_timeout_guard_and_recover():
    """超时保护：挂死请求 → VstWorkerTimeout + 杀进程；下次请求自动恢复。"""
    w = vst_worker.VstWorker(request_timeout=0.5)
    try:
        with pytest.raises(vst_worker.VstWorkerTimeout):
            w._request("debug_sleep", timeout=0.4, secs=3.0)
        assert w.pid is None
        st = w.status()
        assert st["protocol"] == vst_worker.PROTOCOL
        assert w.restarts >= 1
    finally:
        w.shutdown()


def test_worker_matches_inproc_output(monkeypatch):
    """同一输入：worker 输出与进程内直载输出同口径（起音容忍口径）。"""
    w = vst_worker.VstWorker()
    try:
        out_w = w.render(str(_DEXED), _events([_note(0.2, 0.7, 60)]), _SR, _N)
    finally:
        w.shutdown()
    monkeypatch.setenv("TSOV_VST_INPROC", "1")
    src = VST3Source(str(_DEXED))
    try:
        out_i = src.render([_note(0.2, 0.7, 60)], _SR, _N)
    finally:
        src.close()
    assert float(np.abs(out_w).max()) > 0.005 and float(np.abs(out_i).max()) > 0.005
    assert_onset_stable(out_w, out_i, onsets=[0.2], sr=_SR)


def test_singleton_and_env_toggle(monkeypatch):
    """进程级单例：重复取同对象；TSOV_VST_INPROC=1 → None（进程内模式）。"""
    w1 = vst_worker.get_worker()
    assert w1 is not None
    assert vst_worker.get_worker() is w1
    monkeypatch.setenv("TSOV_VST_INPROC", "1")
    assert vst_worker.get_worker() is None
    monkeypatch.delenv("TSOV_VST_INPROC")
    assert vst_worker.get_worker() is w1
