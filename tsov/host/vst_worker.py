"""插件 worker 子进程（批L L1-2；ADR-0025 决策 2）。

第三方插件（VST3 乐器）一律在专属子进程内加载与渲染：

- **崩溃隔离**：插件崩 = worker 崩，宿主（web 服务 / CLI / 测试进程）不倒；
  下次请求自动重启 + 按需重载（惰性：重启后首次用到某插件才重载）。
- **生命周期**：进程级单例（`get_worker()` 懒启动）；`tsov web` 服务退出时
  `shutdown_global()` 收尾；worker 在 stdin EOF（父进程退出）时自行退出，不留孤儿。
- **IPC（Q4 默认）**：stdin/stdout NDJSON；渲染任务带 stage 进度行；
  单件超时 600s 可配（`TSOV_VST_WORKER_TIMEOUT` 环境变量或构造参数）；
  插件加载另有较短超时（挂死保护）。
- **渲染前重置（回确定起点）**：`clear_midi` + 参数全量重设；预设段随批L L1-4 接入。
- 本模块 **不 import dawdreamer**（只在子进程 `_serve()` 里导入）——父进程零插件负担；
  排障/应急可 `TSOV_VST_INPROC=1` 让 `VST3Source` 退回进程内直载（不隔离）。

协议（protocol=1，行分隔 JSON）：
- 请求   ``{"id":N,"cmd":"load|render|status|shutdown|debug_sleep",...}``
- 进度   ``{"id":N,"event":"stage","stage":"load|render"}``
- 应答   ``{"id":N,"ok":true,"result":{...}}`` / ``{"id":N,"ok":false,"error":"..."}``
- 启动   ``{"event":"ready","pid":...,"protocol":1,"dawdreamer":"..."}``
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

PROTOCOL = 1
VST_BLOCK = 512           # dawdreamer RenderEngine 块大小（与 instrument.py 共用同源常量）
DEFAULT_TIMEOUT = 600.0   # 单件（一次请求）超时秒数（Q4 默认，可配）
LOAD_TIMEOUT = 120.0      # 插件加载超时（挂死保护）
START_TIMEOUT = 60.0      # worker 启动（含 dawdreamer 导入）超时

_ROOT = Path(__file__).resolve().parents[2]            # 仓库根（tsov/host/ → 上两级）
_SCRATCH = _ROOT / "output" / ".vst-worker"            # 渲染音频交换目录 + worker 日志（临时产物）

_CRASH = object()  # 队列哨兵：worker 崩溃（EOF/异常）时投给所有等待者


class VstWorkerError(RuntimeError):
    """worker 层错误基类（信息给足，不静默）。"""


class VstWorkerCrashed(VstWorkerError):
    """worker 子进程崩溃/退出（宿主不受影响；下次请求自动重启）。"""


class VstWorkerTimeout(VstWorkerError):
    """worker 响应超时（已杀进程；下次请求自动重启）。"""


# ---------------------------------------------------------------------------
# 纯助手（子进程与进程内直载两条路共用同一套事件转换 / 收尾）
# ---------------------------------------------------------------------------


def events_from_notes(notes) -> list[list]:
    """Note 序列 → ``[[pitch, vel127, t_on, dur], ...]``（两条渲染路同一规则）。"""
    out: list[list] = []
    for n in notes:
        vel = int(round(127.0 * min(1.0, max(0.0, float(n.velocity)))))
        vel = max(1, min(127, vel))
        t_on = max(0.0, float(n.start))
        t_off = max(t_on + 0.01, float(n.end))
        out.append([int(n.pitch_midi), vel, t_on, t_off - t_on])
    return out


def finish_audio(audio, n_frames: int, latency: int = 0) -> np.ndarray:
    """dawdreamer 输出 → mono float32 长度 n_frames（延迟补偿 + 立体声折叠 + 补零/裁剪）。"""
    a = np.asarray(audio, dtype=np.float32)
    if latency and a.ndim == 2 and a.shape[1] > latency:
        a = a[:, latency:]           # 插件上报延迟 L：丢掉前 L 样本对齐
    if a.ndim == 2:                  # (channels, frames) → mono
        a = a.mean(axis=0)
    if a.shape[0] < n_frames:
        a = np.pad(a, (0, n_frames - a.shape[0]))
    return a[:n_frames]


def apply_params(proc, params: dict, names: dict | None = None, strict: bool = False) -> dict | None:
    """把 ``{参数键: 值}`` 归一化（0..1）写入插件。

    键形态（批N A4）：唯一名称 / 十进制下标 / ``名称#下标``（重名参数必须用）。
    返回「参数名 → index」映射（调用方可缓存复用）。``strict=True`` 时失配（名字不存在 /
    下标越界 / 键名不符 / 值非法）直接 raise（「失配可检测报错」）；缺省 False 跳过。
    """
    if not params:
        return names
    desc = None

    def _desc():
        nonlocal desc
        if desc is None:
            desc = proc.get_parameters_description()
        return desc

    if names is None:
        names = {d["name"]: int(d["index"]) for d in _desc()}
    dup_cache: set[str] | None = None

    def _dups() -> set[str]:
        nonlocal dup_cache
        if dup_cache is None:
            cnt: dict[str, int] = {}
            for d in _desc():
                cnt[d["name"]] = cnt.get(d["name"], 0) + 1
            dup_cache = {k for k, v in cnt.items() if v > 1}
        return dup_cache

    for key, value in params.items():
        k = str(key)
        if "#" in k:
            # 名称#下标：重名参数的显式寻址
            base, _, tail = k.rpartition("#")
            if not tail.lstrip("-").isdigit():
                if strict:
                    raise ValueError(f"参数键非法：{k!r}（形如 名称#下标）")
                continue
            idx = int(tail)
            d = _desc()
            if not (0 <= idx < len(d)):
                if strict:
                    raise ValueError(f"参数下标越界：{idx}（现有 {len(d)} 个参数）")
                continue
            if base and str(d[idx].get("name")) != base:
                if strict:
                    raise ValueError(
                        f"参数名与下标不匹配：{base!r} ≠ params[{idx}].name={d[idx].get('name')!r}")
                continue
        elif k.lstrip("-").isdigit():
            idx = int(k)
            # names 因重名会塌缩（len < 真实参数数）——越界时用全量描述复核
            if not (0 <= idx < len(names)):
                d = _desc()
                if not (0 <= idx < len(d)):
                    if strict:
                        raise ValueError(f"参数下标越界：{k}（现有 {len(d)} 个参数）")
                    continue
        else:
            idx = names.get(k)
            if idx is None:
                if strict:
                    raise ValueError(f"参数不存在：{k!r}（先用 list_instrument_params 检索核对）")
                continue
            if strict and k in _dups():
                raise ValueError(f"参数名重名：{k!r}——请用「{k}#<下标>」或纯下标指定")
        try:
            v = float(value)
        except (TypeError, ValueError):
            if strict:
                raise ValueError(f"参数值非法：{k}={value!r}") from None
            continue
        proc.set_parameter(int(idx), float(min(1.0, max(0.0, v))))
    return names


# ---------------------------------------------------------------------------
# 管理器（父进程侧）
# ---------------------------------------------------------------------------


class VstWorker:
    """常驻 worker 管理器：一个实例 = 一个子进程 + 请求串行队列。

    - 崩溃/超时后**本请求报错**，下次请求自动重启（+ 惰性重载已用插件）
    - 渲染音频经 ``output/.vst-worker/render-*.npy`` 交换（用完即删）
    """

    def __init__(self, request_timeout: float = DEFAULT_TIMEOUT) -> None:
        self.request_timeout = float(request_timeout)
        self.restarts = 0
        self._proc: subprocess.Popen | None = None
        self._pending: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()          # 请求串行（worker 单线程处理）
        self._next_id = 1
        self._started_once = False
        self._ready = threading.Event()
        self._dead_reason: str | None = None
        self._worker_pid: int | None = None  # 子进程真身 pid（venv python.exe 是壳，见 _force_kill）
        self.worker_info: dict = {}            # ready 上报（pid/protocol/dawdreamer；诊断与参数表落款用）
        self._loaded: dict[str, dict] = {}     # 请求侧记账（绝对路径 → load 结果；崩溃后清空）
        self._editor_open: str | None = None   # P62b：编辑器窗口打开中的插件路径（关窗事件清除）
        self._editor_epochs: dict[str, int] = {}  # P62b：插件「编辑器代际」（关窗 +1——解锁可能改变渲染输出）
        self._noise: list[str] = []            # 子进程 stdout 非协议行 / 事件（诊断）
        self._log_path = _SCRATCH / "worker.log"

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        """启动子进程并等 ready（失败信息带 log 尾部）。已存活则空操作。"""
        if self._alive():
            return
        if self._started_once:
            self.restarts += 1
        _SCRATCH.mkdir(parents=True, exist_ok=True)
        self._ready.clear()
        self._dead_reason = None
        self.worker_info = {}
        self._pending.clear()
        self._loaded.clear()
        self._editor_open = None    # P62b：新进程无编辑器
        env = dict(os.environ)
        env["PYTHONPATH"] = str(_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONIOENCODING"] = "utf-8"
        log = open(self._log_path, "ab", buffering=0)
        try:
            # 用 `-c` 直呼（而非 `-m`）：tsov.host 包初始化链已加载 .vst_worker，
            # `-m` 会二次执行同一模块并触发 runpy「found in sys.modules」警告（2026-10-14 实测）。
            self._proc = subprocess.Popen(
                [sys.executable, "-u", "-c",
                 "from tsov.host import vst_worker; raise SystemExit(vst_worker._serve())"],
                cwd=str(_ROOT), env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        finally:
            log.close()
        self._started_once = True
        threading.Thread(target=self._reader_loop, args=(self._proc,),
                         name="vst-worker-reader", daemon=True).start()
        deadline = time.monotonic() + START_TIMEOUT
        while True:
            if self._ready.wait(0.2):
                return
            code = self._proc.poll() if self._proc is not None else -1
            if code is not None:
                self._kill()
                raise VstWorkerError(
                    f"VST worker 启动即退出（code={code}）；log 尾部：\n{self._log_tail()}"
                )
            if time.monotonic() > deadline:
                self._kill()
                raise VstWorkerError(
                    f"VST worker 启动超时（{START_TIMEOUT:.0f}s）；log 尾部：\n{self._log_tail()}"
                )

    def shutdown(self) -> None:
        """优雅关闭：shutdown 命令 → 等退 → 强杀兜底；可重复调用、绝不抛出。"""
        with self._lock:
            proc, self._proc = self._proc, None
            if proc is None:
                return
            try:
                if proc.poll() is None:
                    try:
                        if proc.stdin and not proc.stdin.closed:
                            proc.stdin.write(json.dumps({"id": 0, "cmd": "shutdown"}) + "\n")
                            proc.stdin.flush()
                    except OSError:
                        pass
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self._force_kill(proc)
            except Exception:  # noqa: BLE001 收尾路径：尽力而为
                pass
            finally:
                self._dispose_io(proc)
                self._crash_pending()

    def kill(self) -> None:
        """强制终止（测试 / 运维用）；下次请求自动重启 + 重载。"""
        with self._lock:
            self._kill()
            self._loaded.clear()

    @property
    def pid(self) -> int | None:
        """壳（launcher）pid；子进程真身 pid 见 ``worker_pid``（ready 上报）。"""
        return self._proc.pid if self._alive() else None

    @property
    def worker_pid(self) -> int | None:
        """子进程真身 pid（venv launcher 的测试/诊断口径）。"""
        return self._worker_pid

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        self._force_kill(proc)
        self._dispose_io(proc)
        self._crash_pending()

    @staticmethod
    def _force_kill(proc: subprocess.Popen) -> None:
        """强杀子进程树。

        注意（2026-10-14 本机实测）：Windows venv 的 python.exe 是 **launcher**——
        真身（解释器）是它的子进程（实测壳/真身 pid 不同；杀壳与 taskkill /T 均能带走两者）。
        这里以 ``taskkill /T /F`` 显式杀树兜底（pid 全为本项目自己的子进程，非扫杀）。
        """
        try:
            if proc.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                                   capture_output=True, text=True, errors="replace", timeout=20)
                else:
                    proc.kill()
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _dispose_io(proc: subprocess.Popen) -> None:
        for f in (proc.stdin, proc.stdout):
            try:
                f and f.close()
            except Exception:  # noqa: BLE001
                pass

    def _crash_pending(self) -> None:
        for q in list(self._pending.values()):
            q.put(_CRASH)
        self._pending.clear()
        self._ready.clear()

    def _log_tail(self, n: int = 20) -> str:
        try:
            lines = self._log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(lines[-n:]) or "（log 为空）"
        except OSError:
            return "（无 log）"

    # ---------------- 读线程 / 请求 ----------------

    def _reader_loop(self, proc: subprocess.Popen) -> None:
        """常驻读线程：分发应答 / ready / 噪音行；EOF → 标记崩溃并唤醒等待者。"""
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    self._noise = (self._noise + [line[:200]])[-50:]
                    continue
                if not isinstance(msg, dict):
                    continue
                rid = msg.get("id")
                if "event" not in msg and isinstance(rid, int):
                    q = self._pending.pop(rid, None)
                    if q is not None:
                        q.put(msg)
                        continue
                if msg.get("event") == "ready":
                    if msg.get("protocol") != PROTOCOL:
                        self._noise = (self._noise + [f"protocol 不匹配: {msg}"])[-50:]
                    self._worker_pid = int(msg.get("pid") or 0) or None
                    self.worker_info = {"pid": self._worker_pid, "protocol": msg.get("protocol"),
                                        "dawdreamer": msg.get("dawdreamer")}
                    self._ready.set()
                    continue
                if msg.get("event") == "editor_closed":
                    self._on_editor_closed(str(msg.get("plugin") or ""))
                    continue
                if msg.get("event") == "editor_error":
                    self._noise = (self._noise + [f"[editor_error] {msg.get('plugin')}: {msg.get('error')}"])[-50:]
                    continue
                self._noise = (self._noise + [f"{msg}"])[-50:]  # 进度行等 → 诊断缓冲
        except Exception as exc:  # noqa: BLE001
            self._noise = (self._noise + [f"reader 异常: {exc!r}"])[-50:]
        finally:
            if self._proc is proc:     # 只有仍属当前进程的 reader 才改全局状态
                self._dead_reason = f"worker 进程已退出（code={proc.poll()}）"
                self._crash_pending()

    def _on_editor_closed(self, plugin: str) -> None:
        """P62b：worker 关窗事件 → 清编辑器状态 + 插件代际 +1（解锁可能改变渲染输出→失效 stem 缓存）。"""
        self._noise = (self._noise + [f"[editor_closed] {plugin} (epoch={self._editor_epochs.get(plugin, 0) + 1})"])[-50:]
        if plugin:
            self._editor_epochs[plugin] = self._editor_epochs.get(plugin, 0) + 1
        if not plugin or plugin == self._editor_open:
            self._editor_open = None

    def _request(self, cmd: str, timeout: float | None = None, **payload) -> dict:
        with self._lock:
            return self._request_locked(cmd, timeout, payload)

    def _request_locked(self, cmd: str, timeout: float | None, payload: dict) -> dict:
        if not self._alive():          # 崩溃/未启动 → 自动重启
            self.start()
        rid = self._next_id
        self._next_id += 1
        q: queue.Queue = queue.Queue()
        self._pending[rid] = q
        line = json.dumps({"id": rid, "cmd": cmd, **payload}, ensure_ascii=False)
        try:
            assert self._proc is not None and self._proc.stdin is not None
            self._proc.stdin.write(line + "\n")
            self._proc.stdin.flush()
        except OSError as exc:
            self._pending.pop(rid, None)
            raise VstWorkerCrashed(f"worker 写入失败：{exc}；{self._dead_reason or '进程已退出'}") from exc
        t = self.request_timeout if timeout is None else float(timeout)
        try:
            msg = q.get(timeout=t)
        except queue.Empty:
            self._pending.pop(rid, None)
            self._kill()
            raise VstWorkerTimeout(
                f"worker 响应超时（{t:.1f}s，cmd={cmd}）；已终止进程，下次请求自动重启"
            ) from None
        if msg is _CRASH:
            raise VstWorkerCrashed(
                f"{self._dead_reason or 'worker 崩溃'}\nlog 尾部：\n{self._log_tail()}"
            )
        if not msg.get("ok"):
            raise VstWorkerError(f"{cmd} 失败：{msg.get('error')}")
        result = msg.get("result")
        return result if isinstance(result, dict) else {}

    # ---------------- 公开 API ----------------

    def load(self, path: str, sr: int = 44100) -> dict:
        """加载（或复用）插件实例；失败抛 RuntimeError（同进程内模式文案风格）。"""
        apath = str(Path(path).resolve())
        if apath in self._loaded and self._alive():
            return self._loaded[apath]
        info = self._request("load", timeout=LOAD_TIMEOUT, path=apath, sr=int(sr))
        self._loaded[apath] = info
        return info

    def list_params(self, path: str, sr: int = 44100) -> dict:
        """全量参数反查（批L L1-3 introspection；生成参数表的来源）→ {"count", "params": [...]}。"""
        apath = str(Path(path).resolve())
        return self._request("params", path=apath, sr=int(sr))

    def render(self, path: str, events: list, sr: int, n_frames: int,
               *, params: dict | None = None, preset: dict | None = None) -> np.ndarray:
        """渲染一个插件（events 用 ``events_from_notes`` 生成）→ mono float32。"""
        apath = str(Path(path).resolve())
        if preset and preset.get("path"):
            pp = Path(str(preset["path"]))
            if not pp.is_absolute():
                preset = {**preset, "path": str(pp.resolve())}
        if apath not in self._loaded:
            self.load(apath, sr=sr)
        out = _SCRATCH / f"render-{os.getpid()}-{time.time_ns()}.npy"
        try:
            self._request(
                "render", path=apath, sr=int(sr), n=int(n_frames),
                events=[list(e) for e in events], params=dict(params or {}),
                preset=preset, out=str(out),
            )
            arr = np.load(str(out), allow_pickle=False)
        finally:
            try:
                out.unlink()
            except OSError:
                pass
        arr = np.asarray(arr, dtype=np.float32)
        if arr.shape[0] < n_frames:
            arr = np.pad(arr, (0, n_frames - arr.shape[0]))
        return arr[:n_frames]

    def status(self) -> dict:
        """worker 侧状态 + 管理器记账（诊断 / 验收证据用）。"""
        st = self._request("status")
        st.update({"manager_pid": os.getpid(), "launcher_pid": self.pid,
                   "worker_pid": self._worker_pid, "restarts": self.restarts})
        return st

    # ---------------- P62b：插件编辑器过桥 ----------------

    def open_editor(self, path: str, sr: int = 44100) -> dict:
        """打开插件编辑器窗口（worker 级单窗口；立即返回；关窗由事件清状态）。"""
        apath = str(Path(path).resolve())
        if self._editor_open is not None:
            raise VstWorkerError(f"已有插件编辑器打开中：{self._editor_open}（单窗口——先关闭它）")
        if apath not in self._loaded:
            self.load(apath, sr=sr)
        # C2（P62b-6）：置位前比对代际——「打开瞬败/秒关」时 editor_closed 可能在 ack 前已被
        # 读线程处理（彼时 _editor_open 尚为 None，清点路径不匹配）；若期间代际已 +1，不得置位
        # （否则状态永久「打开中」、后续开窗被单窗锁拒，需 worker/服务重启自愈）。
        epoch_before = self._editor_epochs.get(apath, 0)
        res = self._request("open_editor", timeout=LOAD_TIMEOUT, path=apath, sr=int(sr))
        if self._editor_epochs.get(apath, 0) != epoch_before:
            raise VstWorkerError("编辑器已关闭（打开瞬败/秒关）——请重试")
        self._editor_open = apath
        return res

    @property
    def editor_open_path(self) -> str | None:
        """编辑器打开中的插件路径（worker 级；None=无）。"""
        return self._editor_open

    def wait_editor_closed(self, timeout: float = 600.0, poll: float = 0.25) -> bool:
        """阻塞等到编辑器窗口关闭（供脚本/验收；超时返回 False）。"""
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if self._editor_open is None:
                return True
            time.sleep(poll)
        return False


# ---------------------------------------------------------------------------
# 进程级单例（VST3Source 的默认入口）
# ---------------------------------------------------------------------------

_GLOBAL: VstWorker | None = None
_GLOBAL_LOCK = threading.Lock()


def get_worker() -> VstWorker | None:
    """进程级单例（懒启动）。``TSOV_VST_INPROC=1`` → 返回 None（走进程内直载）。"""
    if os.environ.get("TSOV_VST_INPROC", "").strip().lower() in ("1", "true", "yes", "on"):
        return None
    global _GLOBAL
    with _GLOBAL_LOCK:
        if _GLOBAL is None:
            t = float(os.environ.get("TSOV_VST_WORKER_TIMEOUT", "") or DEFAULT_TIMEOUT)
            w = VstWorker(request_timeout=t)
            try:
                w.start()
            except Exception as exc:
                raise VstWorkerError(
                    f"VST worker 启动失败：{exc}\n（应急可设 TSOV_VST_INPROC=1 退回进程内模式——不隔离）"
                ) from exc
            _GLOBAL = w
        return _GLOBAL


def shutdown_global() -> None:
    """收尾单例（服务退出 / 解释器退出时调用；幂等）。"""
    global _GLOBAL
    with _GLOBAL_LOCK:
        w, _GLOBAL = _GLOBAL, None
    if w is not None:
        w.shutdown()


atexit.register(shutdown_global)


def open_editor(plugin_path: str, sr: int = 44100) -> dict:
    """打开插件编辑器窗口（P62b 过桥入口）——需常驻 worker；关窗后重渲恢复出声。"""
    w = get_worker()
    if w is None:
        raise VstWorkerError("插件编辑器需要常驻 worker（当前 TSOV_VST_INPROC=1，不可用）")
    return w.open_editor(plugin_path, sr=sr)


def editor_status() -> dict:
    """编辑器状态（轻量、**不拉起 worker**——前端轮询/诊断用）。"""
    w = _GLOBAL
    if w is None:
        return {"open": False, "path": None, "worker_pid": None}
    return {"open": w._editor_open is not None, "path": w._editor_open,
            "worker_pid": w.worker_pid}


def plugin_epoch(plugin_path: str) -> int:
    """插件「编辑器代际」（P62b）：关窗一次 +1——供 stem 缓存键失效（解锁改变输出）。

    轻量、**不拉起 worker**；无 worker / 未记录 → 0。
    """
    w = _GLOBAL
    if w is None:
        return 0
    try:
        apath = str(Path(plugin_path).resolve())
    except (OSError, ValueError):
        apath = str(plugin_path)
    return int(w._editor_epochs.get(apath, 0))


# ---------------------------------------------------------------------------
# 子进程侧（python -m tsov.host.vst_worker --serve）
# ---------------------------------------------------------------------------


def _emit(obj: dict) -> None:
    try:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass   # 父管道已断（父进程亡）——静默；主循环随后由 EOF 自然退出


class _ChildState:
    """子进程内状态：一个 RenderEngine（按采样率重建）+ 按路径缓存的插件实例。"""

    def __init__(self, daw) -> None:
        self._daw = daw
        self._engine = None
        self._sr: int | None = None
        self._plugins: dict[str, dict] = {}
        self._seq = 0
        self._editors_lock = threading.Lock()
        self._editor_path: str | None = None   # P62b：打开中的编辑器插件路径（worker 级单窗口）
        self._io_lock = threading.Lock()       # P62b-5b：引擎操作互斥（跨线程并发防护）
        self._names_cache: dict[str, dict[str, int]] = {}   # P62b-6（C3）：参数名→索引（path 级
                                                          # 预缓存——弹窗期禁读参数表，见 _render）
        self._pump_fn = None                   # 主线程等锁期间的 Win32 消息泵（_serve 注入）

    def _run_io(self, fn, *a, **kw):
        """引擎互斥（load / params / render / open_editor 建实例）——主线程等锁期间继续泵消息。"""
        while not self._io_lock.acquire(timeout=0.02):
            if self._pump_fn is not None and threading.current_thread() is threading.main_thread():
                self._pump_fn()
        try:
            return fn(*a, **kw)
        finally:
            self._io_lock.release()

    def editor_open_any(self) -> bool:
        with self._editors_lock:
            return self._editor_path is not None

    def _ensure_engine(self, sr: int) -> None:
        if self._engine is None or self._sr != int(sr):
            self._engine = self._daw.RenderEngine(int(sr), VST_BLOCK)
            self._sr = int(sr)
            self._plugins.clear()  # 引擎重建：旧插件实例全部失效（极端场景：采样率变化）

    def _plugin(self, path: str) -> dict:
        ent = self._plugins.get(path)
        if ent is not None:
            return ent
        self._seq += 1
        try:
            proc = self._fresh_processor(path)
        except Exception as exc:
            raise RuntimeError(f"VST3 加载失败：{path}（{exc}）") from exc
        # 同 instrument.py：输入通道数不作拒绝依据（Surge XT 2/2 合成器反例）
        ent = {
            "proc": proc,
            "latency": int(proc.get_latency_samples()),
            "names": None,
            "applied_key": None,
        }
        self._plugins[path] = ent
        return ent

    def _fresh_processor(self, path: str):
        """新建一个插件实例（fresh 默认状态；回确定起点用）。"""
        self._seq += 1
        return self._engine.make_plugin_processor(f"p{self._seq}", path)

    def _apply_preset(self, proc, spec: dict) -> None:
        kind = str(spec.get("kind") or "")
        path = str(spec.get("path") or "")
        if not path:
            raise ValueError("preset 需要 path")
        if kind == "vstpreset":
            proc.load_vst3_preset(path)
        elif kind == "fxp":
            proc.load_preset(path)
        elif kind == "state":
            proc.load_state(path)
        else:
            raise ValueError(f"未知 preset kind：{kind!r}（vstpreset / fxp / state）")

    def handle(self, cmd: str, msg: dict) -> dict:
        if cmd == "load":
            return self._run_io(self._load, str(msg["path"]), int(msg.get("sr") or 44100))
        if cmd == "render":
            return self._run_io(self._render, msg)
        if cmd == "params":
            return self._run_io(self._params, str(msg["path"]), int(msg.get("sr") or 44100))
        if cmd == "open_editor":
            return self._run_io(self._open_editor, msg)
        if cmd == "status":
            return {
                "protocol": PROTOCOL,
                "pid": os.getpid(),
                "engine_sr": self._sr,
                "editor": self._editor_path,
                "plugins": [{"path": p, "latency": e["latency"]}
                            for p, e in list(self._plugins.items())],   # 快照：渲染线程并发插入防护
            }
        if cmd == "debug_sleep":  # 超时保护测试专用
            secs = float(msg.get("secs", 1.0))
            time.sleep(secs)
            return {"slept": secs}
        raise ValueError(f"未知命令：{cmd!r}")

    def _load(self, path: str, sr: int) -> dict:
        self._ensure_engine(sr)
        ent = self._plugin(path)
        proc = ent["proc"]
        desc = proc.get_parameters_description()
        self._names_cache[path] = {d["name"]: int(d["index"]) for d in desc}   # P62b-6（C3）预缓存
        return {
            "latency": ent["latency"],
            "in": int(proc.get_num_input_channels()),
            "out": int(proc.get_num_output_channels()),
            "params": len(desc),
        }

    def _params(self, path: str, sr: int) -> dict:
        """全量参数反查（introspection；批L L1-3 参数表来源）。"""
        self._ensure_engine(sr)
        ent = self._plugin(path)
        desc = ent["proc"].get_parameters_description()
        out: list[dict] = []
        for d in desc:
            labels = [str(s) for s in (d.get("valueStrings") or []) if str(s)]
            out.append({
                "index": int(d["index"]),
                "name": str(d["name"]),
                "default": float(d.get("defaultValue") or 0.0),
                "default_text": str(d.get("defaultValueText") or ""),
                "discrete": bool(d.get("isDiscrete") or d.get("isBoolean")),
                "labels": labels or None,
            })
        self._names_cache[path] = {p["name"]: p["index"] for p in out}   # P62b-6（C3）预缓存
        return {"count": len(out), "params": out}

    def _open_editor(self, msg: dict) -> dict:
        """P62b：校验 + 占位（ack 后由 **_主循环** 在主线程调 ``proc.open_editor()``）。

        源码依据（dawdreamer PluginProcessor.cpp）：窗口只能在 **消息主线程** 弹——
        helper 线程调用会直接抛 "Plugin UI windows can only be shown from the main thread"。
        故此处只做校验/占位；真正的阻塞弹出见 ``_editor_pump``。
        """
        path = str(msg["path"])
        sr = int(msg.get("sr") or 44100)
        self._ensure_engine(sr)
        self._plugin(path)   # 预热/加载（加载失败在 ack 前抛出，主循环返回错误响应）
        with self._editors_lock:
            if self._editor_path is not None:
                raise RuntimeError(f"已有插件编辑器打开中：{self._editor_path}（单窗口）")
            self._editor_path = path
        return {"opened": True, "path": path}

    def _editor_pump(self, path: str) -> None:
        """P62b：在主线程阻塞弹窗（``openWindowAndWait`` 语义——返回即关窗），随后发事件。"""
        err: str | None = None
        try:
            ent = self._plugins.get(path)   # 只读缓存（建实例在 _open_editor 已完成，带锁）
            if ent is None:
                raise RuntimeError(f"插件未加载：{path}")
            ent["proc"].open_editor()   # 主线程；阻塞到用户关窗
        except Exception as exc:  # noqa: BLE001 窗口异常不许杀 worker
            err = f"{type(exc).__name__}: {exc}"
        finally:
            with self._editors_lock:
                if self._editor_path == path:
                    self._editor_path = None
            if err:
                _emit({"event": "editor_error", "plugin": path, "error": err})
            _emit({"event": "editor_closed", "plugin": path})

    def _render(self, msg: dict) -> dict:
        path = str(msg["path"])
        sr = int(msg["sr"])
        n = int(msg["n"])
        self._ensure_engine(sr)
        spec = msg.get("preset")
        params = msg.get("params") or {}
        # 回确定起点（批L 语义）：**预设/参数组合变化 → 重建实例**（fresh 默认）→ 载预设 → 应用 diff。
        # 组合不变则复用（多遍重渲快）；组合消失（回到无预设/无参数）也会重建 → 不带前次残留。
        key = json.dumps([spec, sorted((str(k), v) for k, v in params.items())],
                         sort_keys=True, ensure_ascii=False)
        ent = self._plugin(path)
        with self._editors_lock:
            ep = self._editor_path
        # P62b-6（C3）：弹窗期（ep 非空 → 主线程在模态泵）**禁用依赖消息线程协作的插件调用**。
        # py-spy 实测定格（output/v02-p62b6-verify/c3-hang-diag.log）：弹窗期
        # `get_parameters_description()`（参数表预取）必死锁且不可恢复（WM_CLOSE 亦无解）；
        # `set_parameter`（names 已缓存）安全（0.01s，diag_modal_setparam.log）。故 names 一律走
        # path 级预缓存（正常 load/params/渲染时填充）；弹窗期缺缓存 → 明确拒绝，不许挂死。
        modal = ep is not None
        names_hint = ent["names"] if ent.get("names") is not None else self._names_cache.get(path)
        if ent.get("applied_key") != key:
            if ep == path:
                # P62b-5b（c+）：开窗实例 = 用户正在调的状态权威——**就地应用、绝不重建**
                #（重建会把窗口绑到死实例上，GUI 调整全丢）。「组合消失回干净态」语义
                # 在开窗期不适用：以 GUI 实际状态为准（关窗后同组合重渲沿用实例 → 保住解锁态）。
                if spec and modal:
                    raise RuntimeError("插件界面打开中不支持加载预设（模态期插件调用有死锁风险）——请先关闭窗口")
                if spec:
                    try:
                        self._apply_preset(ent["proc"], spec)
                    except Exception as exc:
                        raise RuntimeError(f"预设加载失败：{spec.get('path')!r}（{exc}）") from exc
                if params and names_hint is None and modal:
                    raise RuntimeError("插件界面打开中且参数表未缓存——请先关闭窗口后重试（弹窗期不可读取参数表）")
                ent["names"] = apply_params(ent["proc"], params, names_hint, strict=True)
                if ent["names"] is not None:
                    self._names_cache[path] = ent["names"]
                ent["applied_key"] = key
            else:
                proc = ent["proc"] = self._fresh_processor(path)
                ent["names"] = None
                ent["applied_key"] = None  # 先失效：中途失败（坏预设/坏参数）不留下"已应用"标记
                ent["latency"] = int(proc.get_latency_samples())
                if spec and modal:
                    raise RuntimeError("插件界面打开中不支持加载预设（模态期插件调用有死锁风险）——请先关闭窗口")
                if spec:
                    try:
                        self._apply_preset(proc, spec)
                    except Exception as exc:
                        raise RuntimeError(f"预设加载失败：{spec.get('path')!r}（{exc}）") from exc
                if params and names_hint is None and modal:
                    raise RuntimeError("插件界面打开中且参数表未缓存——请先关闭窗口后重试（弹窗期不可读取参数表）")
                ent["names"] = apply_params(proc, params, names_hint, strict=True)
                if ent["names"] is not None:
                    self._names_cache[path] = ent["names"]
                ent["applied_key"] = key
        proc = ent["proc"]
        proc.clear_midi()
        for e in msg.get("events") or []:
            pitch, vel, t_on, dur = e
            proc.add_midi_note(int(pitch), int(vel), float(t_on), float(dur))
        latency = int(ent["latency"])
        duration = max(0.05, (n + latency) / float(sr))
        _emit({"id": msg.get("id"), "event": "stage", "stage": "render"})
        self._engine.load_graph([(proc, [])])
        self._engine.render(float(duration))
        arr = finish_audio(self._engine.get_audio(), n, latency)
        out = str(msg["out"])
        np.save(out, arr)
        return {"file": out, "frames": int(arr.shape[0]),
                "peak": float(abs(arr).max()) if arr.size else 0.0}


def _serve() -> int:
    """子进程：**主线程 = JUCE 线程**（load / params / open_editor / 泵窗），
    **读取线程 = 渲染线程**（render / status 就地执行）——弹窗期并行渲染（P62b-5b）。

    dawdreamer 判定（PluginProcessor.cpp）：``MessageManager::getInstance()->
    isThisTheMessageThread()``——JUCE 消息线程 = **首次触碰 JUCE GUI 的线程**，
    而插件加载（make_plugin_processor → loadPlugin）会创建 MessageManager。故首个
    实例创建必须发生在主线程（load 路由保证），此后渲染线程可与泵窗并发 render
    （dawdreamer 加载带静态互斥 + 释放 GIL；探针 G 实证泵窗/渲染并发可行）。
    **主线程闲时兼任 Win32 消息泵**（JUCE 异步回调/定时器依赖消息派发——弹窗期由
    openWindowAndWait 的模态循环负责，平时由 PeekMessage/DispatchMessage 喂；否则
    渲染线程的 make_plugin_processor 会因异步无人收尸而死锁——py-spy 实证）。
    stdin EOF → 哨兵唤醒主线程退出，不留孤儿。"""
    import dawdreamer as daw  # 子进程专属导入：父进程零 dawdreamer / LLVM 负担

    try:
        import ctypes
        import ctypes.wintypes as _wt
        _PM_REMOVE = 0x0001
        _u32 = ctypes.windll.user32

        def _pump_win32() -> None:
            m = _wt.MSG()
            while _u32.PeekMessageW(ctypes.byref(m), None, 0, 0, _PM_REMOVE):
                _u32.TranslateMessage(ctypes.byref(m))
                _u32.DispatchMessageW(ctypes.byref(m))
    except Exception:  # noqa: BLE001 非 Windows / 无 user32：退回纯队列循环
        def _pump_win32() -> None:
            pass

    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[union-attr]
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass
    _emit({"event": "ready", "pid": os.getpid(), "protocol": PROTOCOL,
           "dawdreamer": getattr(daw, "__version__", "?")})
    st = _ChildState(daw)
    st._pump_fn = _pump_win32   # 主线程等引擎锁期间继续泵消息
    main_q: queue.Queue = queue.Queue()
    _SENTINEL: object = object()

    def _reader() -> None:
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                rid = msg.get("id")
                cmd = str(msg.get("cmd") or "")
                if cmd == "shutdown":
                    _emit({"id": rid, "ok": True, "result": {"done": True}})
                    break
                if cmd == "status":
                    # 查询在读取线程就地执行（轻量快照）
                    try:
                        result = st.handle(cmd, msg)
                    except Exception as exc:  # noqa: BLE001
                        _emit({"id": rid, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
                        continue
                    _emit({"id": rid, "ok": True, "result": result})
                    continue
                if cmd == "render" and st.editor_open_any():
                    # P62b-5b：弹窗期渲染走读取线程——模态循环在泵（探针 G 同拓扑，实证可行）。
                    # 关窗后渲染回主线程（= JUCE 消息线程，旧设计语义；读取线程闲时渲 load_graph 会死锁）。
                    try:
                        _emit({"id": rid, "event": "stage", "stage": cmd})  # 进度行（Q4）
                        result = st.handle(cmd, msg)
                    except Exception as exc:  # noqa: BLE001 单命令隔离：坏命令不杀 worker
                        _emit({"id": rid, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
                        continue
                    _emit({"id": rid, "ok": True, "result": result})
                    continue
                main_q.put(msg)   # load / params / open_editor / 关窗后 render / debug_sleep → JUCE 线程
        finally:
            main_q.put(_SENTINEL)   # stdin EOF / shutdown：唤醒主线程收尾退出

    threading.Thread(target=_reader, daemon=True).start()
    while True:
        _pump_win32()                      # 闲时泵消息（JUCE 异步回调/定时器收尸）
        try:
            msg = main_q.get(timeout=0.02)   # 20ms 轮询：兼顾消息泵与命令响应
        except queue.Empty:
            continue
        if msg is _SENTINEL:
            break
        rid = msg.get("id")
        cmd = str(msg.get("cmd") or "")
        try:
            if cmd == "load":
                _emit({"id": rid, "event": "stage", "stage": cmd})  # 进度行（Q4）
            result = st.handle(cmd, msg)
        except Exception as exc:  # noqa: BLE001 单命令隔离：坏命令不杀 worker
            _emit({"id": rid, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
            continue
        _emit({"id": rid, "ok": True, "result": result})
        if cmd == "open_editor":
            # ack 已发 → 主线程（= JUCE 消息线程）阻塞泵窗（返回=关窗）→ 事件。
            st._editor_pump(str(result.get("path") or ""))
    return 0


if __name__ == "__main__":
    if "--serve" in sys.argv[1:]:
        sys.exit(_serve())
    print("tsov 插件 worker（管理器实际经 `-c` 直呼 vst_worker._serve()；本 `-m` 入口留给手工排障，"
          "会带一条 runpy 双重导入警告——无碍）")
