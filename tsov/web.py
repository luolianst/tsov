"""tsov/web.py —— M-V2 可视化宿主 Web 壳（ADR-0014 + ADR-0015，docs/05 前端接口契约）。

FastAPI 胶水层（默认 127.0.0.1:8790，仅本地回环，不暴露公网）：
- REST：工程 CRUD / state / batch（前端一切编辑走事务）/ undo/redo/rollback / log / summary
  / render / play / wav 试听
- SSE：每工程一条事件流（agent_turn / agent_tool / state_updated / diff_applied / agent_answer
  / agent_error / playback_start / playback_stop）
- 对话框：POST /api/chat 起后台 agent 会话（复用 tsov.agent 的 registry/session/llm 积木，
  事件推 SSE；编辑结果经 Project.apply_score 采用 = 一个 git commit + 音符级三色 diff）
- 纪律（handoff 坑 7）：一次只跑一个 agent 会话 / 一个后端播放（锁互斥，忙时 409）

宿主核心与 UI 完全解耦（协议先行，ADR-0014）：本层只做 JSON 协议转换，不实现乐理逻辑；
tsov 库能力零重复（Project/EditBatch/diff/HostEngine/AgentLoop 积木直接 import）。
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .core.score import Instrument, Score, Track
from .host import EditBatch, Project
from .host.cache import StemStore, score_keys   # M-V7 D1（ADR-0018）：轨道级 freeze / stem 库
from .host.state import ProjectState, global_settings_path, set_global_settings  # M-V7 D2（ADR-0019）：计数/设置
from .web_actions import (READ_TOOLS, SCORE_WRITING_TOOLS, ActionJournal,  # 批B（ADR-0017）动作呈现层
                         impact_of, summarize_args, tool_label)

WEB_VERSION = "0.1.0"
AGENT_SESSION_DIR = "output/agent-sessions"
# agent 单条消息内最大工具循环轮数；长链任务（M-V6 批3 双任务测试）可提高：
#   环境变量 TSOV_AGENT_MAX_TURNS=60（Web server 启动前设置）
AGENT_MAX_TURNS = int(os.environ.get("TSOV_AGENT_MAX_TURNS", "12"))
RENDER_WAV_NAME = "render.wav"  # 工程目录内试听 wav（WAV 不入库，工程 .gitignore 已挡）
EDITED_SCORE_NAME = "agent-edited-score.json"  # agent 高层编辑工具的落盘约定（tsov/agent/tools.py）
SSE_HEARTBEAT_SEC = 15.0

STATIC_DIR = Path(__file__).parent / "web" / "static"  # 契约 docs/05 §四：tsov/web/static/（随包分发）


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class SettingsIn(BaseModel):
    """留存设置补丁（M-V7 D2）：不动字段可省略；`timed_favorite` 为 {"enabled", "interval_min"}。"""

    auto_favorite_iters: int | None = None
    timed_favorite: dict | None = None


class WindowJumpIn(BaseModel):
    """快照点跳转（M-V7 D3）：恢复到窗口内第 cursor 个位置。"""

    cursor: int


class ProjectCreate(BaseModel):
    name: str
    score: dict | None = None


class ImportIn(BaseModel):
    source: str              # output/ 内相对路径（.json 文件，或含 score.json 的目录）
    name: str | None = None  # 工程名（缺省 = 从文件名推导）


class BatchIn(BaseModel):
    label: str = ""
    commands: list[dict]
    commit_message: str | None = None


class RollbackIn(BaseModel):
    rev: str = "HEAD~1"


class RenderIn(BaseModel):
    out: str | None = None
    rev: str | None = None   # 议题 ④：A/B 对比试听旧版（git show，不动 HEAD）


class PlayIn(BaseModel):
    """宿主播放参数（M-V8 E1）：播放轴定位 + 循环区间。"""

    start: float | None = None          # 起始秒（播放轴定位）
    loop: list[float] | None = None     # [a, b] 秒——区间循环（阻塞直到 /play/stop）


class ExportIn(BaseModel):
    """导出矩阵选项（修正轮2：UI 导出弹窗 → host export_matrix）。"""
    mix: bool = True
    stems: bool = True
    buses: bool = False
    midi: bool = True
    midi_stems: bool = False


class ChatIn(BaseModel):
    project: str
    message: str
    base_rev: str = "HEAD"   # 议题 ④：当前编辑目标版本（注入 agent prompt，A 路不允许旧版分叉）
    # M-V3（交互闭环）：人工标注确定性先行（最高优先级、不走 LLM）+ 选区上下文（「这里/这段」指代）
    annotations: list[dict] | None = None
    selection: dict | None = None
    # 批B B1-4：用户手动操作摘要（自上次对话以来；回流进 agent 上下文）
    user_actions: list[str] | None = None


class ChatResetIn(BaseModel):
    project: str


class TitleIn(BaseModel):
    title: str


class SessionLoadIn(BaseModel):
    project: str
    name: str


# ---------------------------------------------------------------------------
# 事件总线：每工程一组 SSE 订阅者（跨线程：工作线程 publish → asyncio 队列）
# ---------------------------------------------------------------------------


class _Subscriber:
    __slots__ = ("loop", "queue")

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.queue: asyncio.Queue = asyncio.Queue()


class EventBus:
    """轻量进程内事件总线：按工程名分发 SSE 事件（docs/05 §三）。"""

    def __init__(self) -> None:
        self._subs: dict[str, list[_Subscriber]] = {}
        self._lock = threading.Lock()

    def subscribe(self, project: str) -> _Subscriber:
        sub = _Subscriber(asyncio.get_running_loop())
        with self._lock:
            self._subs.setdefault(project, []).append(sub)
        return sub

    def unsubscribe(self, project: str, sub: _Subscriber) -> None:
        with self._lock:
            subs = self._subs.get(project, [])
            if sub in subs:
                subs.remove(sub)

    def publish(self, project: str, event: str, data: dict) -> None:
        with self._lock:
            subs = list(self._subs.get(project, []))
        msg = {"event": event, "data": data}
        for sub in subs:
            try:
                sub.loop.call_soon_threadsafe(sub.queue.put_nowait, msg)
            except RuntimeError:  # 订阅者 loop 已关（客户端断开未清理）——丢弃即可
                pass


# ---------------------------------------------------------------------------
# Web 全局状态（单进程单实例；Project 每工程懒加载单例）
# ---------------------------------------------------------------------------


class WebState:
    def __init__(self, output_dir: str | Path = "output") -> None:
        self.output_dir = Path(output_dir)
        self.projects: dict[str, Project] = {}
        self.projects_lock = threading.Lock()
        self.agent_lock = threading.Lock()  # 一次只跑一个 agent 会话
        self.play_lock = threading.Lock()  # 一次只跑一个后端播放
        self.play_stop: threading.Event | None = None  # M-V8 E1：宿主播放停止信号（/play/stop 置位）
        self.agent_stop = threading.Event()  # 用户请求停止当前 agent 会话
        self.chat_sessions: dict[str, dict] = {}  # 工程 → {session_id}（多轮续接同一 JSONL）
        self.bus = EventBus()
        self._engine = None  # HostEngine 懒加载（soundfont 依赖）

    # ---------------- 工程 ----------------

    def project_root(self, name: str) -> Path:
        _validate_project_name(name)
        root = (self.output_dir / name).resolve()
        if self.output_dir.resolve() not in root.parents:
            raise HTTPException(400, f"非法工程名：{name!r}")
        return root

    def project_exists(self, name: str) -> bool:
        try:
            return (self.project_root(name) / "score.json").is_file()
        except HTTPException:
            return False

    def get_project(self, name: str) -> Project:
        """懒加载 Project 单例（Project.open 自动补 git init / 基线提交）。"""
        root = self.project_root(name)
        if not (root / "score.json").is_file():
            raise HTTPException(404, f"工程不存在：{name!r}")
        with self.projects_lock:
            proj = self.projects.get(name)
            if proj is None:
                proj = Project.open(root)
                self.projects[name] = proj
            return proj

    def list_projects(self) -> list[dict]:
        """工程列表（M-V6 批1：带信息版）——name/title/tracks/notes/mtime，按 mtime 倒序（最近修改在前）。

        工程判定不变：output/ 下含 score.json 的目录。坏 score.json 不炸列表（信息留缺省，工程仍可见）。
        """
        if not self.output_dir.is_dir():
            return []
        out: list[dict] = []
        for p in self.output_dir.iterdir():
            sj = p / "score.json"
            if not (p.is_dir() and sj.is_file()):
                continue
            info = {"name": p.name, "title": p.name, "tracks": 0, "notes": 0, "mtime": sj.stat().st_mtime}
            try:
                data = json.loads(sj.read_text(encoding="utf-8"))
                info["title"] = str(data.get("title") or p.name)
                tracks = data.get("tracks") or []
                info["tracks"] = len(tracks)
                info["notes"] = sum(len(t.get("notes") or []) for t in tracks)
            except Exception:  # noqa: BLE001 坏文件不炸列表（信息留缺省）
                pass
            out.append(info)
        out.sort(key=lambda x: (-x["mtime"], x["name"]))
        return out

    def list_import_candidates(self) -> list[dict]:
        """可导入的 score json 候选（M-V6 批1 导入通道）：output/*.json 与 output/*/*.json。

        只列「能解析成 Score」的 json（脚本直出产物如 score_task2.json）；跳过工程自带
        score.json（那本就是工程）与隐藏目录；返回相对路径 + 建议工程名 + 统计信息。
        """
        if not self.output_dir.is_dir():
            return []
        base = self.output_dir.resolve()
        candidates: list[dict] = []
        for pattern in ("*.json", "*/*.json"):
            for p in sorted(self.output_dir.glob(pattern)):
                rel = p.resolve().relative_to(base)
                if any(part.startswith(".") for part in rel.parts):
                    continue
                if p.name == "score.json":
                    continue  # 工程自带谱 = 已是工程
                if len(rel.parts) > 1 and (p.parent / "score.json").is_file():
                    continue  # 工程目录内部的文件不列候选（需导入时手填路径）
                try:
                    score = Score.from_dict(json.loads(p.read_text(encoding="utf-8")))
                except Exception:  # noqa: BLE001 非 Score json → 不是候选
                    continue
                candidates.append({
                    "path": rel.as_posix(),
                    "name_hint": _default_project_name(p),
                    "title": score.title or p.stem,
                    "tracks": len(score.tracks),
                    "notes": sum(len(t.notes) for t in score.tracks),
                    "mtime": p.stat().st_mtime,
                })
        candidates.sort(key=lambda x: (-x["mtime"], x["path"]))
        return candidates

    # ---------------- 宿主引擎 ----------------

    def engine(self):
        if self._engine is None:
            from .host import HostEngine

            self._engine = HostEngine()
        return self._engine


def _validate_project_name(name: str) -> None:
    """工程名 = 单层目录名（防路径穿越/非法盘符字符）。"""
    if not name or len(name) > 64:
        raise HTTPException(400, "工程名为空或过长（≤64 字符）")
    if name in (".", "..") or ".." in name or "/" in name or ("\\" in name) or name.startswith("."):
        raise HTTPException(400, f"非法工程名：{name!r}")
    if any(ch in name for ch in ':*?"<>|'):
        raise HTTPException(400, f"工程名含非法字符：{name!r}")


def _default_project_name(path: Path) -> str:
    """从导入文件推导建议工程名（M-V6 批1）：score_task2.json → task2；score.json → 目录名。"""
    stem = path.stem
    if stem == "score":
        return path.parent.name or "imported"
    for prefix in ("score_", "score-"):
        if stem.startswith(prefix) and len(stem) > len(prefix):
            return stem[len(prefix):]
    return stem or "imported"


def _resolve_import_source(output_dir: Path, source: str) -> Path:
    """把导入 source 解析为 output/ 内的 json 文件（防路径穿越；目录 → 其 score.json）。"""
    raw = (source or "").strip().strip('"').strip("'")
    if not raw:
        raise HTTPException(400, "source 为空")
    p = Path(raw)
    if not p.is_absolute():
        p = output_dir / p
    base = output_dir.resolve()
    p = p.resolve()
    if p != base and base not in p.parents:
        raise HTTPException(400, f"只允许导入 output/ 内的文件：{source!r}")
    if p.is_dir():
        p = p / "score.json"
    if not p.is_file():
        raise HTTPException(404, f"文件不存在：{source!r}")
    if p.suffix.lower() != ".json":
        raise HTTPException(400, f"只支持 .json 文件：{source!r}")
    return p


# ---------------------------------------------------------------------------
# state schema 与工程辅助（docs/05 §五）
# ---------------------------------------------------------------------------


def project_state(proj: Project) -> dict:
    return {
        "project": proj.name,
        "score": proj.score.to_dict(),
        "selection": {"track": 0, "indices": []},  # 选择集是前端所有，后端只回默认值
        "history": {"can_undo": proj.can_undo, "can_redo": proj.can_redo},
        "git_log": proj.log(20),
        "summary": proj.summary(),
    }


def score_duration(score: Score, root=None) -> float:
    """工程时长（含 1s 释放尾）：音符尾 + 音频轨 clip 尾。

    音频部分读文件头（soundfile.info）；文件缺失/未给 root 时忽略（渲染/加载时会报错）。
    """
    ends = [n.end for t in score.tracks for n in t.notes]
    if root is not None and score.tracks:
        import soundfile as sf

        base = Path(root)
        for t in score.tracks:
            if str(getattr(t, "kind", "midi") or "midi") != "audio":
                continue
            rel = str((getattr(t, "audio", None) or {}).get("file") or "")
            if not rel:
                continue
            try:
                info = sf.info(str(base / rel))
                off = float((getattr(t, "audio", None) or {}).get("offset") or 0.0)
                ends.append(off + float(info.frames) / float(info.samplerate))
            except Exception:  # noqa: BLE001 —— 文件缺失/损坏：时长忽略（load 时报错）
                continue
    return round(max(ends) + 1.0, 3) if ends else 1.0  # 含 1s 释放尾（与 mix_graph 一致）


def _resolve_project_audio(proj, rel: str) -> Path:
    """工程内音频相对路径解析（E2 护栏：须在 <工程根>/audio/ 内，防穿越）。"""
    if not rel:
        raise HTTPException(400, "缺 file 参数")
    p = Path(str(rel))
    if p.is_absolute() or ".." in p.parts:
        raise HTTPException(400, f"非法音频路径：{rel!r}")
    root = proj.root.resolve()
    full = (proj.root / p).resolve()
    audio_dir = (root / "audio").resolve()
    if full != audio_dir and audio_dir not in full.parents:
        raise HTTPException(400, f"音频文件必须在工程 audio/ 内：{rel!r}")
    if not full.is_file():
        raise HTTPException(404, f"音频文件不存在：{rel!r}")
    return full


def _score_from_dict(data: dict) -> Score:
    try:
        return Score.from_dict(data)
    except Exception as e:  # noqa: BLE001 schema 不合法 → 400
        raise HTTPException(400, f"Score JSON 不合法：{type(e).__name__}: {e}") from e


# ---------------------------------------------------------------------------
# agent 会话（后台线程；事件推 SSE；编辑结果采用进工程；多轮续接 + 停止 + 流式）
# ---------------------------------------------------------------------------


class _StopRequested(Exception):
    """用户请求停止（agent_stop 置位时流式读取中断抛出）。"""


def _read_json_safe(path: Path) -> dict | None:
    """读 JSON（批B：动作快照用）；文件不存在/损坏 → None（快照失败不阻塞 agent 会话）。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _load_session_messages(jsonl_path: Path) -> list[dict]:
    """读会话 JSONL → wire 消息列表（role/content/tool_calls/tool_call_id）。"""
    msgs: list[dict] = []
    if jsonl_path.is_file():
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            msgs.append({k: entry[k] for k in ("role", "content", "tool_calls", "tool_call_id") if k in entry})
    return msgs


def _stream_chat(bus: EventBus, project: str, session_id: str, messages: list[dict],
                 tools: list[dict] | None, stop_event: threading.Event) -> dict:
    """流式 LLM 调用（OpenAI 兼容 SSE）：思考/正文增量推 agent_delta 事件。

    返回与 tsov.agent.llm.chat 同构的 {content, tool_calls, message}；
    连接/协议失败且尚无增量时回退非流式（行为与 AgentLoop 一致）。
    """
    import requests

    from .agent.llm import _json_decision
    from .analysis.llm import LLM_ENDPOINT, LLM_MODEL, resolve_api_key

    api_key = resolve_api_key()
    if not api_key:
        raise RuntimeError("缺少 LLM key（TSOV_LLM_API_KEY / DEEPSEEK_API_KEY，agent 会话无法启动）")

    payload = {"model": LLM_MODEL, "messages": messages, "temperature": 0.2, "stream": True}
    if tools:
        payload["tools"] = tools
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    deltas = {"n": 0}
    try:
        resp = requests.post(LLM_ENDPOINT, json=payload, headers=headers, timeout=(10, 300), stream=True)
        resp.raise_for_status()
        content_acc: list[str] = []
        tool_acc: dict[int, dict] = {}
        for raw in resp.iter_lines(decode_unicode=True):
            if stop_event.is_set():
                raise _StopRequested()
            if not raw or not raw.startswith("data:"):
                continue
            data = raw[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            rc = delta.get("reasoning_content")
            if rc:
                deltas["n"] += 1
                bus.publish(project, "agent_delta", {"session_id": session_id, "kind": "thinking", "text": rc})
            piece = delta.get("content")
            if piece:
                deltas["n"] += 1
                content_acc.append(piece)
                bus.publish(project, "agent_delta", {"session_id": session_id, "kind": "content", "text": piece})
            for tcd in delta.get("tool_calls") or []:
                idx = int(tcd.get("index", 0))
                slot = tool_acc.setdefault(idx, {"id": "", "name": "", "args": ""})
                if tcd.get("id"):
                    slot["id"] = tcd["id"]
                fn = tcd.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]
    except _StopRequested:
        raise
    except Exception as e:  # noqa: BLE001 连接/协议失败
        if deltas["n"] == 0:
            from .agent.llm import chat as llm_chat

            return llm_chat(messages, tools=tools)
        raise RuntimeError(f"流式 LLM 中途失败：{type(e).__name__}: {e}") from e

    content = "".join(content_acc)
    tool_calls: list[dict] = []
    for idx in sorted(tool_acc):
        slot = tool_acc[idx]
        if not slot["name"]:
            continue
        try:
            args = json.loads(slot["args"] or "{}")
        except json.JSONDecodeError:
            args = {}
        tool_calls.append({"id": slot["id"] or f"call-{idx + 1}", "name": slot["name"],
                           "arguments": args if isinstance(args, dict) else {}})

    if not content and not tool_calls:
        from .agent.llm import chat as llm_chat

        return llm_chat(messages, tools=tools)  # 空流（思考型模型正文可能为空）→ 非流式兜底

    if not tool_calls:
        decision = _json_decision(content)
        if decision.get("tool"):
            name = str(decision["tool"])
            args = decision.get("arguments") if isinstance(decision.get("arguments"), dict) else {}
            tool_calls.append({"id": "json-1", "name": name, "arguments": args})

    wire = [{"id": tc["id"], "type": "function",
             "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}}
            for tc in tool_calls]
    message: dict = {"role": "assistant", "content": content}
    if wire:
        message["tool_calls"] = wire
    return {"content": content, "tool_calls": tool_calls, "message": message}


def _retention_tick(state: WebState, project_name: str, *, agent_turns: int = 0) -> None:
    """留存钩子（ADR-0019 / M-V7 D2）：迭代计数 → 阈值自动收藏；定时档惰性检查。

    - 调用点：agent 轮末（finally，正常/停止/异常都过）+ 用户写操作（/batch 等）。
    - 计数口径：自上次 git 点以来的 agent 迭代轮数（手势不计入）；HEAD 变化 → 自动归零。
    - 自动收藏：计数 ≥ 阈值且有改动 → commit + `fav/<ts>-auto`；无改动保留计数（防空收）。
    - 定时档：默认关；开启后（惰性检查）距上次检查 ≥ 间隔且有改动 → `fav/<ts>-time`。
    - 失败静默（由调用方兜底），本函数内不抛。
    """
    proj = state.get_project(project_name)
    ps = ProjectState(proj.root)
    ps.sync_git_point(proj.head_hash())          # 任何 HEAD 变化（收藏/回滚…）→ 计数归零
    if agent_turns:
        ps.add_turns(int(agent_turns))

    settings = ps.settings()
    threshold = max(1, int(settings.get("auto_favorite_iters", 40)))
    if ps.counter >= threshold and proj.is_dirty():
        iters = ps.counter
        r = proj.favorite(auto=True)
        if r.get("ok"):
            ps.reset_counter()
            ps.data["last_commit"] = proj.head_hash() or ps.last_commit
            state.bus.publish(project_name, "favorite",
                              {"tag": r["tag"], "source": "auto", "iters": iters})

    tf = settings.get("timed_favorite") or {}
    if bool(tf.get("enabled")):
        interval = max(60.0, float(tf.get("interval_min", 30)) * 60.0)
        now = time.time()
        if now - ps.last_timed_check >= interval:
            ps.mark_timed_check(now)
            if proj.is_dirty():
                r = proj.favorite(timed=True)
                if r.get("ok"):
                    ps.reset_counter()
                    ps.data["last_commit"] = proj.head_hash() or ps.last_commit
                    state.bus.publish(project_name, "favorite", {"tag": r["tag"], "source": "time"})
    ps.save()


def _run_agent_session(state: WebState, project_name: str, task: str, session_id: str, base_rev: str = "HEAD",
                       annotations: list[dict] | None = None, selection: dict | None = None,
                       user_actions: list[str] | None = None) -> None:
    """一次对话框 agent 会话（后台线程跑；agent_lock 由调用方获取，本函数 finally 释放）。

    base_rev（议题 ④）：编辑目标版本标识，注入 brief；A 路下始终 = 用户当前工作版本（HEAD），
    仅用于让 LLM 明确「我改的是哪个版本」，不改变工程采纳逻辑。

    M-V3（交互闭环）：annotations = 用户人工标注（确定性先行应用，ADR-0009 最高优先级、不走 LLM），
    失败则拒绝整条消息（谱不变）；selection = 当前选区（注入 brief 供指代）。
    """
    from .agent import AgentSession, build_default_registry
    from .agent.prompt import build_system_prompt
    from .agent.skills import SkillLibrary

    bus = state.bus
    proj = state.get_project(project_name)
    root = proj.root
    edited_path = root / EDITED_SCORE_NAME
    journal = ActionJournal(root)   # 快照窗口（M-V7 D2：动作级撤销 + 弱留存双口径）
    round_key = f"{session_id}:{int(time.time())}"   # 本对话轮标识（agent 窗口淘汰口径）
    # 清掉上一轮残留——只采用「本轮」的编辑结果
    try:
        edited_path.unlink()
    except FileNotFoundError:
        pass

    # ---- M-V3：人工标注确定性先行（最高优先级；ADR-0009「不走 LLM」）----
    # 失败（非法标注 / 空结果守卫）→ 拒绝整条消息、谱不变；成功 → 落盘 + 快照窗口条目（M-V7 D2：零 commit）
    ann_note = ""
    if annotations:
        try:
            from .analysis.edit import edit_score as _edit_annotations

            ann_result = _edit_annotations(proj.score, annotations=annotations, llm=False)
            if ann_result.error:
                bus.publish(project_name, "agent_error",
                            {"session_id": session_id, "error": f"人工标注非法（拒绝）：{ann_result.error}"})
                state.agent_lock.release()
                return
            adopted = proj.apply_score(ann_result.new_score, f"人工标注（{len(annotations)} 条）")
            if adopted["ok"]:
                bus.publish(project_name, "diff_applied", {**adopted["diff"], "commit": adopted["commit"], "seq": adopted.get("seq")})
                bus.publish(project_name, "state_updated", project_state(proj))
                ann_note = (
                    f"- 用户人工标注已确定性应用（{adopted['diff']['summary']}，快照 #{adopted.get('seq')}）："
                    + "；".join(ann_result.diff_summary[:6])
                    + "\n  这些是用户精确指定的修改：请在结果中保留、不得回退\n"
                )
        except Exception as e:  # noqa: BLE001 标注应用异常 → 拒绝消息（不泄漏锁）
            bus.publish(project_name, "agent_error",
                        {"session_id": session_id, "error": f"人工标注应用失败：{type(e).__name__}: {e}"})
            state.agent_lock.release()
            return

    score_path = root / "score.json"
    render_path = root / RENDER_WAV_NAME
    sel_note = ""
    if selection and selection.get("indices"):
        idxs = "、".join(str(i) for i in list(selection["indices"])[:24])
        sel_note = f"- 用户当前选区：track {selection.get('track', 0)} 音符 [{idxs}]（用户说「这里/这段」时指的就是它）\n"
    ua_note = ""
    if user_actions:
        ua_note = ("- 用户手动操作（自上次对话以来，命令层已落盘；用户说\"我刚改了什么\"时以这些为准）："
                   + "；".join(str(x) for x in list(user_actions)[:10]) + "\n")
    brief = (
        f"{task}\n\n"
        f"【工程上下文】\n"
        f"- 当前工程名：{proj.name}；工程 score 路径：{score_path}\n"
        f"- 编辑目标版本：{base_rev}（工程 git 版本标识；请勿自行 git 回滚/切分支，版本切换由宿主负责）\n"
        f"{ann_note}{sel_note}{ua_note}"
        f"- 改谱：用 edit_score 工具（score_path 用上面的工程 score 路径，feedback 写用户的修改要求），"
        f"工具会把新谱自动落盘为同目录的 {EDITED_SCORE_NAME}\n"
        f"- 改完谱自查：用 load_score 读 {EDITED_SCORE_NAME}，检查全部音高是否属于目标调式音阶"
        f"（如 D 多利亚 = D E F G A B C）；不纯（含调外音）就再用 edit_score 修一轮，直到纯净\n"
        f"- 自查通过后再用 render_wav 渲染新谱试听（score_path={edited_path}，out_wav={render_path}）\n"
        f"- 完成后用中文一句话汇报改动要点（含调式与音阶名）"
    )

    skills_lib = SkillLibrary()   # skill 机制（M-V4）：目录进系统提示，正文按需 use_skill 加载
    jsonl_path = Path(AGENT_SESSION_DIR) / f"{session_id}.jsonl"
    session = AgentSession(task=task, session_dir=AGENT_SESSION_DIR, session_id=session_id)
    if jsonl_path.is_file():
        session.messages = _load_session_messages(jsonl_path)  # 续接：同一 JSONL 多轮上下文
    else:
        session.add("system", build_system_prompt(skills_lib))

    # 首轮带工程上下文；后续轮只发用户消息（上下文已在会话历史里）
    if any(m.get("role") == "user" for m in session.messages):
        user_msg = task
    else:
        user_msg = brief
    session.add("user", user_msg)

    tool_count = 0
    turns = 0
    stopped = False
    try:
        registry = build_default_registry(skills=skills_lib)

        answer = ""
        for turn in range(1, AGENT_MAX_TURNS + 1):
            turns = turn
            if state.agent_stop.is_set():
                stopped = True
                break
            out = _stream_chat(bus, project_name, session_id, session.api_messages,
                               registry.openai_tools(), state.agent_stop)
            bus.publish(
                project_name,
                "agent_turn",
                {
                    "session_id": session_id,
                    "turn": turn,
                    "content": out["content"] or "",
                    "tool_calls": [{"name": tc["name"], "arguments": tc["arguments"],
                                    "id": tc.get("id")} for tc in out["tool_calls"]],
                },
            )
            if not out["tool_calls"]:
                session.add("assistant", out["content"])
                answer = out["content"]
                break
            session.add("assistant", out["content"], tool_calls=out["message"].get("tool_calls"))
            for tc in out["tool_calls"]:
                if state.agent_stop.is_set():
                    stopped = True
                    break
                tool_name = tc["name"]
                targs = tc["arguments"] if isinstance(tc["arguments"], dict) else {}
                # 批B（ADR-0017）：动作前快照——scratch 不存在 = 轮首，取工程当前谱
                pre_snap = _read_json_safe(edited_path) or proj.score.to_dict()
                pre_hash = journal.snapshot(pre_snap)
                try:
                    observation = registry.get(tool_name).handler(tc["arguments"])
                except Exception as e:  # noqa: BLE001 工具出错也作为观测回喂（与 AgentLoop 同策略）
                    observation = f"工具 {tool_name} 错误: {type(e).__name__}: {e}"
                tool_count += 1
                # 批B：动作后快照 → 影响范围（"改了哪些轨/几个音/哪个参数"）+ 动作日志（可撤销）
                post_snap = _read_json_safe(edited_path)
                post_hash = journal.snapshot(post_snap)
                impact = impact_of(pre_snap, post_snap) if (post_snap and tool_name in SCORE_WRITING_TOOLS) else None
                entry = None
                if impact is not None and impact.get("text") and tool_name in SCORE_WRITING_TOOLS:
                    entry = journal.append(source="agent", label=tool_label(tool_name),
                                           session_id=session_id, turn=turn, round=round_key,
                                           tool=tool_name, args=summarize_args(tool_name, targs),
                                           pre=pre_hash, post=post_hash, impact=impact)
                bus.publish(
                    project_name,
                    "agent_tool",
                    {
                        "session_id": session_id, "tool": tool_name, "observation": observation,
                        # ---- 动作卡 v2 字段（批B B1-1；旧字段保留 = SSE 兼容）----
                        "tool_call_id": tc.get("id"),
                        "turn": turn,
                        "action_id": f"{session_id}:{turn}:{tc.get('id') or tool_count}",
                        "label": tool_label(tool_name),
                        "summary": summarize_args(tool_name, targs),
                        "impact": impact,
                        "read_only": tool_name in READ_TOOLS,
                        "seq": entry["seq"] if entry else None,
                        "undoable": bool(entry),
                    },
                )
                session.add("tool", observation, tool_call_id=tc["id"])
        else:
            answer = f"已达最大轮次（{AGENT_MAX_TURNS}）未形成最终回答；会话已落盘。"
        if stopped:
            answer = "会话已被用户停止（已落盘的修改保留，未完成的轮次中断）。"

        # 采用编辑结果：agent-edited-score.json 存在且与现谱有差异 → 一个 commit + 三色 diff
        adopted: dict | None = None
        if edited_path.is_file():
            try:
                new_score = Score.from_dict(json.loads(edited_path.read_text(encoding="utf-8")))
                result = proj.apply_score(new_score, f"agent：{task[:40]}", source="agent")
                adopted = result if result["ok"] else None
            except Exception as e:  # noqa: BLE001 坏文件不阻塞回答
                bus.publish(
                    project_name,
                    "agent_tool",
                    {"session_id": session_id, "tool": "apply_score",
                     "observation": f"编辑结果采用失败：{type(e).__name__}: {e}"},
                )
            if adopted:
                bus.publish(project_name, "diff_applied", {**adopted["diff"], "commit": adopted["commit"], "seq": adopted.get("seq")})
                bus.publish(project_name, "state_updated", project_state(proj))

        bus.publish(
            project_name,
            "agent_answer",
            {
                "session_id": session_id,
                "content": answer,
                "turns": turns,
                "tool_calls_made": tool_count,
                "adopted": bool(adopted),
                "stopped": stopped,
            },
        )
    except _StopRequested:
        bus.publish(
            project_name,
            "agent_answer",
            {"session_id": session_id, "content": "会话已被用户停止（流式中断）。",
             "turns": turns, "tool_calls_made": tool_count, "adopted": False, "stopped": True},
        )
    except Exception as e:  # noqa: BLE001 LLM/致命错误 → agent_error 事件（锁在 finally 释放）
        bus.publish(project_name, "agent_error", {"session_id": session_id, "error": f"{type(e).__name__}: {e}"})
    finally:
        # M-V7 D2（ADR-0019）：轮末留存钩子——迭代计数 + 阈值自动收藏 + 定时档惰性（失败静默）
        try:
            _retention_tick(state, project_name, agent_turns=turns)
        except Exception:  # noqa: BLE001 留存钩子失败不影响主流程
            pass
        state.agent_lock.release()


# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------


def create_app(output_dir: str | Path = "output") -> FastAPI:
    app = FastAPI(title="the shape of voice — 可视化宿主", version=WEB_VERSION)
    app.state.tsov = WebState(output_dir)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        # 契约统一：错误返回 {"error": "<中文描述>"}（docs/05 §三）
        return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"error": f"内部错误：{type(exc).__name__}: {exc}"}, status_code=500)

    def st() -> WebState:
        return app.state.tsov

    # ---------------- 基础 ----------------

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "version": WEB_VERSION}

    @app.get("/api/meta")
    def meta() -> dict:
        """UI 批A：面板元数据（GM 音色名 / 效果类型）。"""
        from .host.effect import effect_kinds
        from .midi.export import GM_PROGRAMS

        return {"programs": sorted(GM_PROGRAMS), "effect_kinds": effect_kinds()}

    # ---------------- 工程 ----------------

    @app.get("/api/projects")
    def list_projects() -> dict:
        return {"projects": st().list_projects()}

    @app.post("/api/projects")
    def create_project(body: ProjectCreate) -> dict:
        state = st()
        root = state.project_root(body.name)
        if (root / "score.json").is_file():
            raise HTTPException(400, f"工程已存在：{body.name!r}")
        if body.score is None:
            # 空谱也带一条空旋律轨（审计修 M-V2.3：edit_score 按轨编辑，零轨工程会被硬拒 → 空谱创作不可用）
            score = Score(title=body.name, tempo=120.0, key_candidates=[],
                          tracks=[Track(name="melody", instrument=Instrument(), notes=[])], meta={})
        else:
            score = _score_from_dict(body.score)
        proj = Project.create(body.name, score, parent=state.output_dir)
        with state.projects_lock:
            state.projects[body.name] = proj
        return {"ok": True, "name": body.name}

    @app.get("/api/import-candidates")
    def import_candidates() -> dict:
        """导入候选：output/ 下能解析成 Score 的 json（M-V6 批1 导入通道）。"""
        return {"candidates": st().list_import_candidates()}

    @app.post("/api/projects/import")
    def import_project(body: ImportIn) -> dict:
        """导入 score json → 新工程（M-V6 批1：脚本直出产物回流通道；git init + 基线 commit）。"""
        state = st()
        src = _resolve_import_source(state.output_dir, body.source)
        name = (body.name or "").strip() or _default_project_name(src)
        _validate_project_name(name)
        root = state.project_root(name)
        if (root / "score.json").is_file():
            raise HTTPException(400, f"工程已存在：{name!r}（换个名字，或先打开旧工程）")
        try:
            data = json.loads(src.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"JSON 读取失败：{e}") from e
        score = _score_from_dict(data)
        proj = Project.create(name, score, parent=state.output_dir)
        with state.projects_lock:
            state.projects[name] = proj
        rel = src.resolve().relative_to(state.output_dir.resolve()).as_posix()
        return {"ok": True, "name": name, "source": rel}

    @app.get("/api/projects/{name}/state")
    def get_state(name: str) -> dict:
        return project_state(st().get_project(name))

    @app.post("/api/projects/{name}/batch")
    def apply_batch(name: str, body: BatchIn) -> dict:
        proj = st().get_project(name)
        batch = EditBatch(label=body.label)
        for i, c in enumerate(body.commands):
            op = c.get("op")
            if not isinstance(op, str):
                raise HTTPException(400, f"第 {i} 条命令缺 op")
            batch.add(op, track=int(c.get("track", 0)), index=c.get("index"), value=c.get("value"))
        result = proj.apply_batch(batch, commit_message=body.commit_message or body.label or "编辑",
                                  source="user")
        if result["applied"] == 0:
            # 全部命令被拒 → 工程不动；契约 §五：applied=0 + errors 清单仍在响应体里
            return result
        st().bus.publish(name, "diff_applied", {**result["diff"], "commit": result["commit"], "seq": result.get("seq")})
        st().bus.publish(name, "state_updated", project_state(proj))
        try:
            _retention_tick(st(), name)   # M-V7 D2：写操作触发留存检查（定时档惰性）
        except Exception:  # noqa: BLE001 留存钩子失败不影响主流程
            pass
        return result

    @app.post("/api/projects/{name}/undo")
    def undo(name: str) -> dict:
        proj = st().get_project(name)
        if not proj.undo():
            raise HTTPException(400, "没有可撤销的历史")
        st().bus.publish(name, "state_updated", project_state(proj))
        return {"ok": True}

    @app.post("/api/projects/{name}/redo")
    def redo(name: str) -> dict:
        proj = st().get_project(name)
        if not proj.redo():
            raise HTTPException(400, "没有可重做的历史")
        st().bus.publish(name, "state_updated", project_state(proj))
        return {"ok": True}

    @app.post("/api/projects/{name}/rollback")
    def rollback(name: str, body: RollbackIn) -> dict:
        proj = st().get_project(name)
        if not proj.rollback(body.rev):
            raise HTTPException(400, f"回滚失败：{body.rev!r}（版本不存在或工程无 git 历史）")
        st().bus.publish(name, "state_updated", project_state(proj))
        return {"ok": True}

    @app.get("/api/projects/{name}/log")
    def git_log(name: str) -> dict:
        return {"log": st().get_project(name).log(20)}

    @app.get("/api/projects/{name}/summary")
    def summary(name: str) -> dict:
        return {"summary": st().get_project(name).summary()}


    # ---------------- 渲染 / 回放（M-V7 D1：轨道级 freeze + 统一 stem 库，ADR-0018） ----------------

    def _assemble(name: str, proj, score, out_path, *, rev: str | None = None) -> dict:
        """按 stem 库拼装混音（缺轨渲后入库）；逐轨进度上 SSE。返回统计。"""
        engine = st().engine()
        store = StemStore(proj.root)
        stats: dict = {}
        bus = st().bus

        def on_progress(ev: dict) -> None:
            bus.publish(name, "render_progress", ev)

        session = engine.load(score, base_dir=proj.root)
        try:
            audio = engine.render(session, out_wav=out_path, stereo=True,
                                  cache=store, stats=stats, on_progress=on_progress)
        finally:
            session.close()
        info = {
            "rendered": stats.get("rendered", []),
            "cached": stats.get("cached", []),
            "duration": round(audio.shape[0] / engine.samplerate, 3),
        }
        bus.publish(name, "render_done", {"rendered": info["rendered"], "cached": len(info["cached"]),
                                          **({"rev": rev[:8]} if rev else {})})
        return info

    @app.post("/api/projects/{name}/render")
    def render(name: str, body: RenderIn) -> dict:
        proj = st().get_project(name)
        engine = st().engine()
        if body.rev:
            # 任意 git 版本：按 manifest 拼装（缺轨渲后入库；不进 rollback）
            score = proj.score_at(body.rev)
            if score is None:
                raise HTTPException(400, f"版本不存在或内容损坏：{body.rev!r}")
            mix_dir = proj.root / ".mix-cache"
            mix_dir.mkdir(exist_ok=True)
            out = mix_dir / f"{body.rev[:8]}.wav"
            info = _assemble(name, proj, score, out, rev=body.rev)
            return {"wav": str(out), "sr": engine.samplerate, "rev": body.rev[:8], **info}
        out = Path(body.out) if body.out else proj.root / RENDER_WAV_NAME
        proj.save()  # score.json 落最新真相（渲染与 git 版本一致）
        info = _assemble(name, proj, proj.score, out)
        return {"wav": str(out), "sr": engine.samplerate, **info}

    @app.get("/api/projects/{name}/wav")
    def wav(name: str, rev: str | None = None):
        """浏览器试听 wav（ADR-0018：stem 库拼装；缺失/比 score.json 旧时自动重拼）。

        ?rev=<版本>：按该版本拼装（git show，不动 HEAD），用于对比滑块左槽试听。
        """
        from fastapi.responses import FileResponse

        proj = st().get_project(name)
        if rev:
            score = proj.score_at(rev)
            if score is None:
                raise HTTPException(400, f"版本不存在或内容损坏：{rev!r}")
            mix_dir = proj.root / ".mix-cache"
            mix_dir.mkdir(exist_ok=True)
            wav_path = mix_dir / f"{rev[:8]}.wav"
            if not wav_path.is_file():
                _assemble(name, proj, score, wav_path, rev=rev)
            return FileResponse(str(wav_path), media_type="audio/wav", filename=f"{name}-{rev[:8]}.wav")

        wav_path = proj.root / RENDER_WAV_NAME
        score_path = proj.root / "score.json"
        stale = not wav_path.is_file() or score_path.stat().st_mtime > wav_path.stat().st_mtime
        if stale:
            _assemble(name, proj, proj.score, wav_path)
        return FileResponse(str(wav_path), media_type="audio/wav", filename=f"{name}.wav")

    # ---------------- stem 缓存 GC（M-V7 D1 / ADR-0018） ----------------

    # ---------------- 音频素材（M-V8 E2：音频轨·第一刀） ----------------

    @app.post("/api/projects/{name}/audio/import")
    async def audio_import(name: str, request: Request) -> dict:
        """音频入库（E2）：JSON {path: 本机绝对路径, name?} 或 multipart file 上传。

        - 路径来源：后端直读本机文件（原曲素材场景，仅本机服务）
        - 上传来源：浏览器 file input / 拖拽 → 落临时文件后同路径入库（ffmpeg 转 44.1k flac）
        """
        proj = st().get_project(name)
        ctype = (request.headers.get("content-type") or "").lower()
        tmp_path = None
        try:
            if "multipart/form-data" in ctype:
                form = await request.form()
                up = form.get("file")
                if up is None or not getattr(up, "filename", ""):
                    raise HTTPException(400, "multipart 缺 file 字段")
                up_name = str(form.get("name") or "").strip() or None
                suffix = Path(str(up.filename)).suffix or ".bin"
                incoming = proj.root / "audio" / ".incoming"
                incoming.mkdir(parents=True, exist_ok=True)
                tmp_path = incoming / f"upload-{uuid.uuid4().hex[:8]}{suffix}"
                tmp_path.write_bytes(await up.read())
                info = proj.import_audio(tmp_path, name=up_name)
            else:
                body = await request.json()
                src = str((body or {}).get("path") or "").strip().strip('"').strip("'")
                if not src:
                    raise HTTPException(400, "缺 path（本机音频绝对路径）")
                info = proj.import_audio(src, name=(body or {}).get("name"))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
        return {"project": name, **info}

    @app.get("/api/projects/{name}/audio/peaks")
    def audio_peaks(name: str, file: str, buckets: int = 800) -> dict:
        """波形峰值（E2）：mono 降采样 min/max 桶数组 → 前端 canvas 绘制。"""
        import soundfile as sf

        proj = st().get_project(name)
        path = _resolve_project_audio(proj, file)
        buckets = max(8, min(4000, int(buckets)))
        data, sr = sf.read(str(path), dtype="float32", always_2d=True)
        mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
        n = int(mono.shape[0])
        per = max(1, n // buckets)
        m = (n // per) * per
        if m >= per:
            blk = mono[:m].reshape(-1, per)
            mins = blk.min(axis=1)
            maxs = blk.max(axis=1)
            if m < n:  # 尾部余量并入末桶
                mins[-1] = min(float(mins[-1]), float(mono[m:].min()))
                maxs[-1] = max(float(maxs[-1]), float(mono[m:].max()))
        else:
            mins, maxs = mono[:1], mono[:1]
        return {
            "file": path.name,
            "seconds": round(n / int(sr), 4),
            "buckets": int(len(mins)),
            "min": [round(float(x), 5) for x in mins],
            "max": [round(float(x), 5) for x in maxs],
        }

    @app.get("/api/projects/{name}/audio/file")
    def audio_file(name: str, file: str):
        """试听/交付工程内音频文件（FileResponse）。"""
        from fastapi.responses import FileResponse

        proj = st().get_project(name)
        path = _resolve_project_audio(proj, file)
        media = "audio/flac" if path.suffix.lower() == ".flac" else "application/octet-stream"
        return FileResponse(str(path), media_type=media, filename=path.name)

    @app.post("/api/projects/{name}/cache/gc")
    def cache_gc(name: str) -> dict:
        """删除不被 HEAD/收藏版本引用的 stem；顺带清理退役的 `.render-cache/`。"""
        proj = st().get_project(name)
        engine = st().engine()
        store = StemStore(proj.root)
        keep = set(score_keys(proj.score, engine.samplerate))
        for fav in proj.favorites():
            s = proj.score_at(fav.get("tag") or "HEAD")
            if s is not None:
                keep |= score_keys(s, engine.samplerate)
        report = store.gc(keep)
        report["keep"] = len(keep)
        report["size_mb"] = round(store.size_bytes() / 1024 / 1024, 2)
        return report

    # ---------------- 导出 / 收藏（修正轮2） ----------------

    @app.post("/api/projects/{name}/export")
    def export(name: str, body: ExportIn) -> dict:
        """导出矩阵（master / 总线 / 每轨 stems + MIDI）→ 工程 exports/<时间戳>/。"""
        proj = st().get_project(name)
        proj.save()
        engine = st().engine()
        out_dir = proj.root / "exports" / datetime.now().strftime("%Y%m%d-%H%M%S")
        session = engine.load(proj.score, base_dir=proj.root)
        try:
            report = engine.export(session, out_dir,
                                   mix=body.mix, buses=body.buses, stems=body.stems,
                                   midi=body.midi, midi_stems=body.midi_stems,
                                   cache=StemStore(proj.root))
        finally:
            session.close()
        return report

    @app.post("/api/projects/{name}/favorite")
    def favorite(name: str) -> dict:
        """收藏当前版本（M-V7 D2：commit+tag 二连；强留存 + 恢复入口）。"""
        proj = st().get_project(name)
        r = proj.favorite()
        if not r.get("ok"):
            raise HTTPException(400, r.get("error") or "收藏失败")
        ps = ProjectState(proj.root)      # 收藏 = 新 git 点 → 计数归零
        ps.sync_git_point(proj.head_hash())
        ps.reset_counter()
        ps.save()
        return r

    @app.get("/api/projects/{name}/favorites")
    def favorites(name: str) -> dict:
        return {"favorites": st().get_project(name).favorites()}

    @app.delete("/api/projects/{name}/favorites")
    def favorite_delete(name: str, tag: str) -> dict:
        """删除收藏 tag（只删标签，不动提交；M-V7 D3）。"""
        if not st().get_project(name).delete_favorite(tag):
            raise HTTPException(400, f"删除失败：{tag!r}（不存在或非收藏标签）")
        return {"ok": True, "tag": tag}

    @app.get("/api/projects/{name}/agent-actions")
    def agent_actions(name: str, limit: int = 60) -> dict:
        """动作快照日志（窗口内最近 limit 条；供前端回放与失效置灰）。"""
        proj = st().get_project(name)
        j = proj.journal
        return {"entries": j.entries[-max(1, int(limit)):]}

    # ---------------- 快照窗口 / 留存设置（M-V7 D2，ADR-0019） ----------------

    @app.get("/api/projects/{name}/window")
    def window(name: str, limit: int = 40) -> dict:
        """快照窗口（弱留存）：条目 + 游标 + 容量（供 D3 UI / 回滚下拉）。"""
        proj = st().get_project(name)
        w = proj.journal.window()
        w["entries"] = w["entries"][-max(1, int(limit)):]
        return w

    @app.post("/api/projects/{name}/window/jump")
    def window_jump(name: str, body: WindowJumpIn) -> dict:
        """快照点跳转（M-V7 D3）：恢复到窗口内任意位置（零 commit；其后条目下次编辑分歧置灰）。"""
        proj = st().get_project(name)
        if not proj.jump_window(int(body.cursor)):
            raise HTTPException(400, "该快照点不可恢复（越界或快照缺失）")
        st().bus.publish(name, "state_updated", project_state(proj))
        w = proj.journal.window()
        return {"ok": True, "cursor": w["cursor"], "can_undo": w["can_undo"], "can_redo": w["can_redo"]}

    @app.get("/api/projects/{name}/settings")
    def get_settings(name: str) -> dict:
        """留存设置（合成视图：工程档 > 全局档 > env > 默认）+ 当前迭代计数。"""
        proj = st().get_project(name)
        ps = ProjectState(proj.root)
        return {"settings": ps.settings(), "counter": ps.counter,
                "global_path": str(global_settings_path())}

    @app.post("/api/projects/{name}/settings")
    def set_settings(name: str, body: SettingsIn) -> dict:
        """写工程档设置（`.tsov-state.json`；部分字段更新）。"""
        proj = st().get_project(name)
        ps = ProjectState(proj.root)
        patch = {k: v for k, v in body.model_dump().items() if v is not None}
        return {"ok": True, "settings": ps.set_project_settings(patch)}

    @app.post("/api/settings")
    def set_settings_global(body: SettingsIn) -> dict:
        """写全局档设置（仓库根 `tsov-settings.json`，跨工程）。"""
        patch = {k: v for k, v in body.model_dump().items() if v is not None}
        return {"ok": True, "global": set_global_settings(patch),
                "path": str(global_settings_path())}

    @app.post("/api/projects/{name}/agent-actions/{seq}/undo")
    def agent_action_undo(name: str, seq: int) -> dict:
        """动作级撤销（M-V7 D2：窗口回跳——恢复该动作前快照，零 commit，其后动作置灰）。"""
        proj = st().get_project(name)
        j = proj.journal
        entry = j.get(seq)
        if not entry:
            raise HTTPException(404, f"动作 #{seq} 不存在（可能已超出留存窗口）")
        snap = j.jump(seq)   # 游标跳到动作前 + 其后（含自身）标记失效
        if not snap:
            raise HTTPException(409, "该动作没有可回退的快照")
        try:
            new_score = Score.from_dict(snap)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"快照解析失败：{type(e).__name__}: {e}") from e
        result = proj.apply_score(new_score, f"撤销动作：{entry.get('label')}（#{seq}）",
                                  source="user", record=False)
        if result.get("ok"):
            st().bus.publish(name, "diff_applied", {**result["diff"], "commit": result["commit"], "seq": result.get("seq")})
            st().bus.publish(name, "state_updated", project_state(proj))
        st().bus.publish(name, "action_undone",
                         {"seq": seq, "ok": bool(result.get("ok")), "commit": result.get("commit")})
        return {"ok": bool(result.get("ok")), "seq": seq, "commit": result.get("commit"),
                "message": "" if result.get("ok") else "；".join(result.get("errors") or [])}

    @app.post("/api/projects/{name}/play")
    def play(name: str, body: PlayIn | None = None) -> dict:
        """后端宿主播放（sounddevice 阻塞到播完；一次只跑一个，忙时 409）。

        M-V8 E1：body.start = 起始秒（播放轴定位）；body.loop = [a, b] 区间循环
        （循环时阻塞直到 `/play/stop`）。
        """
        proj = st().get_project(name)
        if not st().play_lock.acquire(blocking=False):
            raise HTTPException(409, "已有播放在进行中")
        bus = st().bus
        duration = score_duration(proj.score, proj.root)
        start = float(body.start or 0.0) if body else 0.0
        loop = None
        if body and body.loop is not None:
            if len(body.loop) != 2:
                st().play_lock.release()
                raise HTTPException(400, "loop 需 [a, b] 两元素")
            a, b = float(body.loop[0]), float(body.loop[1])
            if a < 0 or b <= a:
                st().play_lock.release()
                raise HTTPException(400, f"loop 区间非法：{a}/{b}")
            loop = (a, b)
        st().play_stop = threading.Event()
        try:
            bus.publish(name, "playback_start", {"duration": duration, "start": start,
                                                 "loop": list(loop) if loop else None})
            engine = st().engine()
            engine.play(engine.load(proj.score, base_dir=proj.root), blocking=True, start=start, loop=loop,
                        stop_event=st().play_stop)
            bus.publish(name, "playback_stop", {"duration": duration})
            return {"ok": True, "duration": duration, "start": start,
                    "loop": list(loop) if loop else None}
        finally:
            st().play_stop = None
            st().play_lock.release()

    @app.post("/api/projects/{name}/play/stop")
    def play_stop(name: str) -> dict:
        """停止宿主播放（M-V8 E1）：置停止信号 + 打断 sounddevice。"""
        st().get_project(name)  # 工程存在性校验
        ev = st().play_stop
        if ev is not None:
            ev.set()
        try:
            import sounddevice as sd

            sd.stop()
        except Exception:  # noqa: BLE001 —— 无设备/未装时静默（无播放可停）
            pass
        return {"ok": True}

    # ---------------- SSE 事件流 ----------------

    @app.get("/api/projects/{name}/events")
    async def events(name: str, request: Request, close_after: int | None = None) -> StreamingResponse:
        """SSE 事件流。close_after=N：产出 N 帧后正常收流（测试用；浏览器缺省无限流）。"""
        state = st()
        if not state.project_exists(name):
            raise HTTPException(404, f"工程不存在：{name!r}")
        sub = state.bus.subscribe(name)

        async def gen():
            frames = 0
            try:
                yield ": connected\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        msg = await asyncio.wait_for(sub.queue.get(), timeout=SSE_HEARTBEAT_SEC)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        frames += 1
                    else:
                        yield (
                            f"event: {msg['event']}\n"
                            f"data: {json.dumps(msg['data'], ensure_ascii=False)}\n\n"
                        )
                        frames += 1
                    if close_after is not None and frames >= close_after:
                        break
            finally:
                state.bus.unsubscribe(name, sub)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    # ---------------- 对话框 agent 会话 ----------------

    @app.post("/api/chat")
    def chat(body: ChatIn) -> dict:
        state = st()
        if not body.message.strip():
            raise HTTPException(400, "消息为空")
        state.get_project(body.project)  # 工程存在性前置校验（404）
        if not state.agent_lock.acquire(blocking=False):
            raise HTTPException(409, "已有 agent 会话在跑（一次只跑一个）")
        conv = state.chat_sessions.get(body.project)
        session_id = (
            conv["session_id"] if conv else f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        )
        state.chat_sessions[body.project] = {"session_id": session_id}
        state.agent_stop.clear()
        try:
            threading.Thread(
                target=_run_agent_session,
                args=(state, body.project, body.message, session_id, body.base_rev,
                      body.annotations, body.selection, body.user_actions),
                daemon=True,
                name=f"tsov-agent-{session_id[-8:]}",
            ).start()
        except Exception:
            state.agent_lock.release()
            raise
        return {
            "session_id": session_id,
            "session_path": str(Path(AGENT_SESSION_DIR) / f"{session_id}.jsonl"),
        }

    # ---------------- 会话控制 / 工程 显示名 / 会话导入（M-V2.1 增补） ----------------

    @app.post("/api/chat/stop")
    def chat_stop() -> dict:
        """请求停止当前 agent 会话（协作式：轮间/工具前/流式 chunk 检查）。"""
        state = st()
        busy = state.agent_lock.locked()
        if busy:
            state.agent_stop.set()
        return {"ok": True, "stopping": busy}

    @app.post("/api/chat/reset")
    def chat_reset(body: ChatResetIn) -> dict:
        """结束当前对话线程：下一轮 /api/chat 起新 JSONL（历史文件保留）。"""
        state = st()
        state.get_project(body.project)
        if state.agent_lock.locked():
            raise HTTPException(409, "会话进行中，先停止再开新会话")
        state.chat_sessions.pop(body.project, None)
        return {"ok": True}

    @app.post("/api/projects/{name}/title")
    def set_title(name: str, body: TitleIn) -> dict:
        """改工程显示名（score.title；目录不动）。走 apply_score = 一个 commit，可撤销。"""
        proj = st().get_project(name)
        title = body.title.strip()
        if not title or len(title) > 80:
            raise HTTPException(400, "标题为空或过长（≤80 字符）")
        new_score = copy.deepcopy(proj.score)
        new_score.title = title
        result = proj.apply_score(new_score, f"改名：{title[:40]}")
        st().bus.publish(name, "state_updated", project_state(proj))
        return {"ok": True, "title": title, "commit": result.get("commit")}

    @app.get("/api/sessions")
    def list_sessions() -> dict:
        """列出 agent 会话 JSONL（新→旧，最多 50 条），供导入回看/续接。"""
        d = Path(AGENT_SESSION_DIR)
        items: list[dict] = []
        if d.is_dir():
            for p in sorted(d.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True)[:50]:
                st_ = p.stat()
                items.append({
                    "name": p.name,
                    "size": st_.st_size,
                    "mtime": datetime.fromtimestamp(st_.st_mtime).isoformat(timespec="seconds"),
                })
        return {"sessions": items}

    @app.post("/api/sessions/load")
    def load_session(body: SessionLoadIn) -> dict:
        """导入历史会话：消息回放给前端 + 设为当前工程的续接会话（接着聊）。"""
        state = st()
        state.get_project(body.project)
        if state.agent_lock.locked():
            raise HTTPException(409, "会话进行中，先停止再导入")
        fname = Path(body.name).name  # 只取 basename，防路径穿越
        path = Path(AGENT_SESSION_DIR) / fname
        if path.suffix != ".jsonl" or not path.is_file():
            raise HTTPException(404, f"会话文件不存在：{fname!r}")
        sid = fname[: -len(".jsonl")]
        state.chat_sessions[body.project] = {"session_id": sid}
        return {"ok": True, "session_id": sid, "messages": _load_session_messages(path)}

    # ---------------- 静态前端（最后挂载；/api 路由先注册，不受影响） ----------------

    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

    return app


app = create_app()


def main(host: str = "127.0.0.1", port: int = 8790) -> None:
    """`tsov web` 入口（ADR-0014：默认 127.0.0.1:8790，仅本地回环）。"""
    import uvicorn

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
