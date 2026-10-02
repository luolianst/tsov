"""链运行器（M-V8 E3 段2）：步骤状态机 + 后台线程 + 取消检查点 + chain.json 持久化。

状态机（E3 讨论记录 Q1/Q2/Q7/Q8）：
- 步骤态：pending | running | done | failed | skipped（mute）| cancelled | dirty
- 快档（fast）改参自动顺跑：从最上游脏步起，顺「连续快工具段」向下自动跑，遇慢档停下标脏
- 慢档（slow）改参：标脏 + 「应用」按钮（点 = 该步起的区间 run）
- 每步可 mute（跑时跳过，上下游直连）
- 失败：停步标红（重试 / 改参再跑 / 跳过此步 = 前端以不同 steps 重新 run）
- 取消：步边界检查点；已完成产物保留

持久化：<project>/chain.json（source/preset/run_ts/逐步参数与状态；不碰 ADR-0005 的 score schema）
产物：<project>/chain/<run-ts>/（工程 .gitignore 排除）
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .tools import ChainCancelled, ChainContext, get_tool

CHAIN_DIRNAME = "chain"
PRESET_DIR = Path(__file__).resolve().parents[2] / "presets" / "chains"
STEP_STATUSES = ("pending", "running", "done", "failed", "skipped", "cancelled", "dirty")


# ---------------------------------------------------------------------------
# chain.json（工程伴生）
# ---------------------------------------------------------------------------


def _chain_json_path(proj_root: Path) -> Path:
    return Path(proj_root) / "chain.json"


def load_chain_json(proj_root: Path) -> dict:
    """读工程链配置；不存在/损坏 → {}。"""
    path = _chain_json_path(proj_root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_chain_json(proj_root: Path, data: dict) -> Path:
    """写工程链配置（原子替换）。"""
    path = _chain_json_path(proj_root)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def load_preset(preset_id: str) -> dict:
    """读链预设 presets/chains/<id>.json。"""
    path = PRESET_DIR / f"{preset_id}.json"
    if not path.is_file():
        raise ValueError(f"链预设不存在：{preset_id!r}（找 {path}）")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
        raise ValueError(f"链预设格式非法：{path}")
    return data


# ---------------------------------------------------------------------------
# 运行器
# ---------------------------------------------------------------------------


@dataclass
class StepState:
    tool_id: str
    name: dict
    tier: str
    params: dict
    mute: bool = False
    status: str = "pending"
    error: str | None = None
    artifact: str | None = None
    stats: dict = field(default_factory=dict)
    elapsed_sec: float | None = None


class ChainRunner:
    """单工程单活跃链（WebState.chains 持有；一次一个 run）。"""

    def __init__(self, proj, *, preset_id: str = "humming-quicklane",
                 source_track: int | None = None, overrides: dict | None = None):
        self.proj = proj
        self.preset_id = preset_id
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self.run_ts: str | None = None
        self.error: str | None = None
        self.on_event = None  # webapp 注入：fn(event: str, data: dict)（SSE）

        preset = load_preset(preset_id)
        saved = load_chain_json(proj.root)
        saved_steps = {s.get("tool"): s for s in (saved.get("steps") or [])}

        # 源轨：显式参数 > chain.json > 第一条音频轨
        if source_track is None:
            source_track = saved.get("source_track")
        if source_track is None:
            source_track = self._first_audio_track(proj)
        if source_track is None:
            raise ValueError("工程内没有音频轨（先导入/录制音频，或指定 source_track）")
        if not (0 <= int(source_track) < len(proj.score.tracks)):
            raise ValueError(f"source_track 越界：{source_track}（共 {len(proj.score.tracks)} 轨）")
        if proj.score.tracks[int(source_track)].kind != "audio":
            raise ValueError(f"source_track {source_track} 不是音频轨（kind="
                             f"{proj.score.tracks[int(source_track)].kind!r}）")
        self.source_track = int(source_track)

        # 步骤：预设为准；参数 = 工具默认 < chain.json 存值 < overrides
        ov = overrides or {}
        steps: list[StepState] = []
        for entry in preset["steps"]:
            tid = str(entry.get("tool") or "")
            tool = get_tool(tid)
            params = {k: v.get("default") for k, v in tool["params"].items()}
            params.update(entry.get("params") or {})
            old = saved_steps.get(tid) or {}
            params.update(old.get("params") or {})
            params.update(ov.get(tid) or {})
            # M-V8 E3 段2：从 chain.json 恢复上轮结果（web 重启 / 重新挂链后 UI 仍有「上次跑到哪」）
            old_status = str(old.get("status") or "pending")
            if old_status not in ("pending", "done", "failed", "skipped", "cancelled", "dirty"):
                old_status = "pending"   # running 等瞬态不恢复
            steps.append(StepState(
                tool_id=tid, name=tool["name"], tier=tool["tier"],
                params=params, mute=bool(old.get("mute", False)),
                status=old_status,
                error=old.get("error"),
                artifact=old.get("artifact"),
                stats=dict(old.get("stats") or {}),
                elapsed_sec=old.get("elapsed_sec"),
            ))
        self.steps = steps
        self.run_ts = saved.get("run_ts") or None   # 恢复「最近运行」时间戳（start 时会刷新）

    # ---------------- 查询 ----------------

    @staticmethod
    def _first_audio_track(proj) -> int | None:
        for i, t in enumerate(proj.score.tracks):
            if t.kind == "audio":
                return i
        return None

    def source_audio_path(self) -> Path:
        """源音频绝对路径（工程 audio/ 内）。

        E6 兼容：轨道被 clip 编辑（拖动/切片）后 audio 为 ``{"clips": [...]}`` 形态——
        单片段取该片段的文件引用；多片段暂不支持（明确报错，避免取错素材）。
        """
        t = self.proj.score.tracks[self.source_track]
        audio = t.audio or {}
        rel = str(audio.get("file") or "")
        if not rel and "clips" in audio:
            clips = [c for c in t.audio_clips() if c.get("file")]
            if len(clips) > 1:
                raise ValueError(
                    f"音频轨 {self.source_track} 为多片段（{len(clips)} 段）——"
                    "链暂不支持多片段源（先合并为单段再跑）")
            rel = str((clips[0].get("file") if clips else "") or "")
        if not rel:
            raise ValueError(f"音频轨 {self.source_track} 没有 file 字段")
        path = (self.proj.root / rel).resolve()
        if self.proj.root.resolve() not in path.parents:
            raise ValueError(f"音频路径越出工程根：{rel!r}")
        if not path.is_file():
            raise ValueError(f"音频文件缺失：{path}")
        return path

    @property
    def running(self) -> bool:
        th = self._thread
        return th is not None and th.is_alive()

    def snapshot(self) -> dict:
        """状态快照（REST/前端轮询；线程安全）。"""
        with self._lock:
            steps = [asdict(s) for s in self.steps]
        done = sum(1 for s in steps if s["status"] in ("done", "skipped"))
        terminal = all(s["status"] in ("done", "skipped", "failed", "cancelled") for s in steps)
        return {
            "preset": self.preset_id,
            "source_track": self.source_track,
            "run_ts": self.run_ts,
            "running": self.running,
            "error": self.error,
            "finished": terminal and self.run_ts is not None,
            "progress": {"done": done, "total": len(steps)},
            "steps": steps,
        }

    # ---------------- 控制 ----------------

    def _emit(self, event: str, data: dict) -> None:
        if callable(self.on_event):
            try:
                self.on_event(event, data)
            except Exception:  # noqa: BLE001
                pass

    def start(self, *, steps: list[str] | None = None) -> dict:
        """开跑（后台线程）：steps = 工具 id 子集（默认全部；子集用于「应用」「只跑这步」）。"""
        if self.running:
            raise RuntimeError("链正在运行中（先取消或等完成）")
        targets = self.steps
        if steps:
            want = [str(x) for x in steps]
            targets = [s for s in self.steps if s.tool_id in want]
            if not targets:
                raise ValueError(f"steps 子集为空（可选：{', '.join(s.tool_id for s in self.steps)}）")
        self._cancel.clear()
        self.error = None
        # 继承基准：以下 _save() 会刷新 chain.json（run_ts 与 targets 的 artifact），故先记旧快照
        _old = load_chain_json(self.proj.root)
        self._prev_run_ts = str(_old.get("run_ts") or "")
        self._prev_steps = {str(x.get("tool")): x for x in (_old.get("steps") or [])}
        # run_ts：秒级时间戳；同秒重跑防撞（防目录复用 + 保跨 run 继承判据成立）
        ts = time.strftime("%Y%m%d-%H%M%S")
        base = ts
        n = 1
        while ts == self._prev_run_ts or (self.proj.root / CHAIN_DIRNAME / ts).exists():
            n += 1
            ts = f"{base}-{n}"
        self.run_ts = ts
        for s in self.steps:
            if s not in targets:
                continue
            s.status = "pending"
            s.error = None
            s.artifact = None
            s.stats = {}
            s.elapsed_sec = None
        self._save()
        th = threading.Thread(target=self._run, args=(targets,), daemon=True, name=f"tsov-chain-{self.proj.name}")
        self._thread = th
        th.start()
        return self.snapshot()

    def cancel(self) -> dict:
        """请求取消（步边界生效；已完成产物保留）。"""
        self._cancel.set()
        return self.snapshot()

    # ---------------- 线程体 ----------------

    def _run_dir(self) -> Path:
        d = self.proj.root / CHAIN_DIRNAME / str(self.run_ts)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _run(self, targets: list[StepState]) -> None:
        try:
            run_dir = self._run_dir()
            source = self.source_audio_path()
            ctx_base = dict(project_root=self.proj.root, run_dir=run_dir,
                            source_audio=source, score_tempo=float(self.proj.score.tempo or 120.0),
                            cancel=self._cancel)
            prev: Path | None = None
            old_ts = str(getattr(self, "_prev_run_ts", "") or "")   # start 预存盘前的旧 run_ts
            old_dir = (self.proj.root / CHAIN_DIRNAME / old_ts) if old_ts and old_ts != self.run_ts else None
            old_by_tool = dict(getattr(self, "_prev_steps", {}) or {})   # start 预存盘前的旧步快照
            self._emit("chain_started", {"run_ts": self.run_ts, "preset": self.preset_id,
                                         "source_track": self.source_track})
            for st in targets:
                if self._cancel.is_set():
                    with self._lock:
                        st.status = "cancelled"
                    break
                if st.mute:
                    with self._lock:
                        st.status = "skipped"
                    continue
                tool = get_tool(st.tool_id)
                # 输入解析（Q2/重试/跳过）：本轮上游产物优先（prev 由前步 done 更新）；
                # 缺则沿链序回退，在「历史 run 目录（新→旧）」中找最近一份上游步产物（跨 run 继承）
                if prev is None and old_by_tool:
                    order = [x.tool_id for x in self.steps]
                    upto = order.index(st.tool_id)
                    chain_root = self.proj.root / CHAIN_DIRNAME
                    dirs = []
                    if chain_root.is_dir():
                        dirs = sorted((p for p in chain_root.iterdir() if p.is_dir() and p.name != self.run_ts),
                                      key=lambda p: p.name, reverse=True)
                    for j in range(upto - 1, -1, -1):
                        art = str((old_by_tool.get(order[j]) or {}).get("artifact") or "")
                        if not art:
                            continue
                        for dd in dirs:
                            cand = dd / art
                            if cand.is_file():
                                prev = cand
                                break
                        if prev is not None:
                            break
                ctx = ChainContext(step_id=st.tool_id, params=dict(st.params), prev=prev, **ctx_base)
                ctx.on_log = lambda m, _st=st: self._emit("chain_log", {"tool": _st.tool_id, "message": m})
                with self._lock:
                    st.status = "running"
                self._emit("chain_step", {"tool": st.tool_id, "status": "running"})
                t0 = time.time()
                try:
                    stats = tool["run"](ctx)
                except ChainCancelled:
                    with self._lock:
                        st.status = "cancelled"
                        st.elapsed_sec = round(time.time() - t0, 3)
                    self._emit("chain_step", {"tool": st.tool_id, "status": "cancelled"})
                    break
                except Exception as e:  # noqa: BLE001 任何异常 → 停步标红（前端三选择）
                    with self._lock:
                        st.status = "failed"
                        st.error = f"{type(e).__name__}: {e}"
                        st.elapsed_sec = round(time.time() - t0, 3)
                    self._emit("chain_step", {"tool": st.tool_id, "status": "failed",
                                              "error": st.error})
                    break
                with self._lock:
                    st.status = "done"
                    st.elapsed_sec = round(time.time() - t0, 3)
                    st.stats = dict(stats or {})
                    art = (stats or {}).get("artifact")
                    if art:
                        st.artifact = str(art)
                if st.artifact:
                    prev = run_dir / st.artifact
                self._emit("chain_step", {"tool": st.tool_id, "status": "done",
                                          "artifact": st.artifact, "elapsed_sec": st.elapsed_sec})
                self._save()
            self._save()
            self._emit("chain_finished", {"run_ts": self.run_ts, "status": self._final_status()})
        except Exception as e:  # noqa: BLE001 运行器级异常（源缺失等）
            self.error = f"{type(e).__name__}: {e}"
            self._emit("chain_finished", {"run_ts": self.run_ts, "status": "failed",
                                          "error": self.error})
        finally:
            self._save()

    def _final_status(self) -> str:
        with self._lock:
            if any(s.status == "failed" for s in self.steps):
                return "failed"
            if any(s.status == "cancelled" for s in self.steps):
                return "cancelled"
            return "done"

    def _save(self) -> None:
        """chain.json 落盘（参数/mute/状态/产物持久化；running 态不落）。"""
        with self._lock:
            steps = []
            for s in self.steps:
                steps.append({
                    "tool": s.tool_id,
                    "params": s.params,
                    "mute": s.mute,
                    "status": s.status if s.status != "running" else "pending",
                    "artifact": s.artifact,
                    "stats": s.stats,
                    "elapsed_sec": s.elapsed_sec,
                    "error": s.error,
                })
        try:
            save_chain_json(self.proj.root, {
                "source_track": self.source_track,
                "preset": self.preset_id,
                "run_ts": self.run_ts,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "steps": steps,
            })
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 配置更新（mute / 参数 / 源轨——不跑；跑由 REST run 触发）
# ---------------------------------------------------------------------------


def apply_config(proj, *, mute: dict | None = None, overrides: dict | None = None,
                 source_track: int | None = None, preset_id: str | None = None) -> dict:
    """把 mute/参数/源 合并进 chain.json（不动 score）；返回更新后的配置。"""
    data = load_chain_json(proj.root)
    steps = data.get("steps") or []
    by_tool = {s.get("tool"): s for s in steps}
    mute = mute or {}
    overrides = overrides or {}
    for tid, val in mute.items():
        by_tool.setdefault(str(tid), {"tool": str(tid)})["mute"] = bool(val)
    for tid, params in overrides.items():
        entry = by_tool.setdefault(str(tid), {"tool": str(tid)})
        entry["params"] = {**(entry.get("params") or {}), **(params or {})}
    data["steps"] = list(by_tool.values())
    if source_track is not None:
        data["source_track"] = int(source_track)
    if preset_id:
        data["preset"] = str(preset_id)
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_chain_json(proj.root, data)
    return data


def auto_run_plan(proj, changed_tool: str, preset_id: str = "humming-quicklane") -> dict:
    """改参后的顺跑计划（Q2）：返回 {'auto': [工具…]} 或 {'needs_apply': 工具}。

    规则：从最上游「脏步」（=changed_tool 所在步）起，顺连续 fast 段向下；
    若首个脏步是 slow（或链中下一非 mute 步是 slow）→ 停下等「应用」。
    """
    preset = load_preset(preset_id)
    saved = {s.get("tool"): s for s in (load_chain_json(proj.root).get("steps") or [])}
    order = [str(e.get("tool")) for e in preset["steps"]]
    if changed_tool not in order:
        raise ValueError(f"工具不在链内：{changed_tool!r}")
    idx = order.index(changed_tool)
    seq = []
    for i in range(idx, len(order)):
        tid = order[i]
        if (saved.get(tid) or {}).get("mute"):
            continue
        tool = get_tool(tid)
        if tool["tier"] == "slow":
            # 遇慢档停下（Q2）：起点即慢档 → 等「应用」；否则快段跑完后停在该步前
            if i == idx:
                return {"needs_apply": tid}
            return {"auto": seq, "stops_at": tid}
        seq.append(tid)
    return {"auto": seq}
