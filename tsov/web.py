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
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .core.score import Instrument, Score, Track
from .host import EditBatch, Project

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


def score_duration(score: Score) -> float:
    ends = [n.end for t in score.tracks for n in t.notes]
    return round(max(ends) + 1.0, 3) if ends else 1.0  # 含 1s 释放尾（与 mix_graph 一致）


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


def _run_agent_session(state: WebState, project_name: str, task: str, session_id: str, base_rev: str = "HEAD",
                       annotations: list[dict] | None = None, selection: dict | None = None) -> None:
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
    # 清掉上一轮残留——只采用「本轮」的编辑结果
    try:
        edited_path.unlink()
    except FileNotFoundError:
        pass

    # ---- M-V3：人工标注确定性先行（最高优先级；ADR-0009「不走 LLM」）----
    # 失败（非法标注 / 空结果守卫）→ 拒绝整条消息、谱不变；成功 → 一个独立 commit
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
                bus.publish(project_name, "diff_applied", {**adopted["diff"], "commit": adopted["commit"]})
                bus.publish(project_name, "state_updated", project_state(proj))
                ann_note = (
                    f"- 用户人工标注已确定性应用（{adopted['diff']['summary']}，commit {adopted['commit']}）："
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
    brief = (
        f"{task}\n\n"
        f"【工程上下文】\n"
        f"- 当前工程名：{proj.name}；工程 score 路径：{score_path}\n"
        f"- 编辑目标版本：{base_rev}（工程 git 版本标识；请勿自行 git 回滚/切分支，版本切换由宿主负责）\n"
        f"{ann_note}{sel_note}"
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
                    "tool_calls": [{"name": tc["name"], "arguments": tc["arguments"]} for tc in out["tool_calls"]],
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
                try:
                    observation = registry.get(tc["name"]).handler(tc["arguments"])
                except Exception as e:  # noqa: BLE001 工具出错也作为观测回喂（与 AgentLoop 同策略）
                    observation = f"工具 {tc['name']} 错误: {type(e).__name__}: {e}"
                tool_count += 1
                bus.publish(
                    project_name,
                    "agent_tool",
                    {"session_id": session_id, "tool": tc["name"], "observation": observation},
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
                result = proj.apply_score(new_score, f"agent：{task[:40]}")
                adopted = result if result["ok"] else None
            except Exception as e:  # noqa: BLE001 坏文件不阻塞回答
                bus.publish(
                    project_name,
                    "agent_tool",
                    {"session_id": session_id, "tool": "apply_score",
                     "observation": f"编辑结果采用失败：{type(e).__name__}: {e}"},
                )
            if adopted:
                bus.publish(project_name, "diff_applied", {**adopted["diff"], "commit": adopted["commit"]})
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
        result = proj.apply_batch(batch, commit_message=body.commit_message or body.label or "编辑")
        if result["applied"] == 0:
            # 全部命令被拒 → 工程不动；契约 §五：applied=0 + errors 清单仍在响应体里
            return result
        st().bus.publish(name, "diff_applied", {**result["diff"], "commit": result["commit"]})
        st().bus.publish(name, "state_updated", project_state(proj))
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


    # ---------------- 渲染 / 回放 ----------------

    @app.post("/api/projects/{name}/render")
    def render(name: str, body: RenderIn) -> dict:
        proj = st().get_project(name)
        engine = st().engine()
        if body.rev:
            # 议题 ④：渲染任意 git 版本（git show，不进 rollback；缓存到 .render-cache/<rev8>.wav）
            score = proj.score_at(body.rev)
            if score is None:
                raise HTTPException(400, f"版本不存在或内容损坏：{body.rev!r}")
            cache_dir = proj.root / ".render-cache"
            cache_dir.mkdir(exist_ok=True)
            out = cache_dir / f"{body.rev[:8]}.wav"
            if not out.is_file():
                session = engine.load(score)
                try:
                    audio = engine.render(session, out_wav=out)
                finally:
                    session.close()
            else:
                import wave
                with wave.open(str(out), "rb") as wf:
                    frames = wf.getnframes()
                    sr = wf.getframerate()
                audio = None
            duration = (frames / sr) if audio is None else round(len(audio) / engine.samplerate, 3)
            return {"wav": str(out), "duration": duration, "sr": (sr if audio is None else engine.samplerate), "rev": body.rev[:8]}
        out = Path(body.out) if body.out else proj.root / RENDER_WAV_NAME
        proj.save()  # score.json 落最新真相（渲染与 git 版本一致）
        session = engine.load(proj.score)
        try:
            audio = engine.render(session, out_wav=out)
        finally:
            session.close()
        return {"wav": str(out), "duration": round(len(audio) / engine.samplerate, 3), "sr": engine.samplerate}

    @app.get("/api/projects/{name}/wav")
    def wav(name: str, rev: str | None = None):
        """浏览器试听 wav：缺失或比 score.json 旧时自动重渲（保证与工程真相一致）。

        ?rev=<版本> （议题 ④）：渲染缓存版（git show，不动 HEAD），用于对比滑块左槽试听。
        """
        from fastapi.responses import FileResponse

        proj = st().get_project(name)
        if rev:
            score = proj.score_at(rev)
            if score is None:
                raise HTTPException(400, f"版本不存在或内容损坏：{rev!r}")
            cache_dir = proj.root / ".render-cache"
            cache_dir.mkdir(exist_ok=True)
            wav_path = cache_dir / f"{rev[:8]}.wav"
            if not wav_path.is_file():
                engine = st().engine()
                session = engine.load(score)
                try:
                    engine.render(session, out_wav=wav_path)
                finally:
                    session.close()
            return FileResponse(str(wav_path), media_type="audio/wav", filename=f"{name}-{rev[:8]}.wav")

        wav_path = proj.root / RENDER_WAV_NAME
        score_path = proj.root / "score.json"
        stale = not wav_path.is_file() or score_path.stat().st_mtime > wav_path.stat().st_mtime
        if stale:
            engine = st().engine()
            session = engine.load(proj.score)
            try:
                engine.render(session, out_wav=wav_path)
            finally:
                session.close()
        return FileResponse(str(wav_path), media_type="audio/wav", filename=f"{name}.wav")

    # ---------------- 导出 / 收藏（修正轮2） ----------------

    @app.post("/api/projects/{name}/export")
    def export(name: str, body: ExportIn) -> dict:
        """导出矩阵（master / 总线 / 每轨 stems + MIDI）→ 工程 exports/<时间戳>/。"""
        proj = st().get_project(name)
        proj.save()
        engine = st().engine()
        out_dir = proj.root / "exports" / datetime.now().strftime("%Y%m%d-%H%M%S")
        session = engine.load(proj.score)
        try:
            report = engine.export(session, out_dir,
                                   mix=body.mix, buses=body.buses, stems=body.stems,
                                   midi=body.midi, midi_stems=body.midi_stems)
        finally:
            session.close()
        return report

    @app.post("/api/projects/{name}/favorite")
    def favorite(name: str) -> dict:
        """收藏当前版本（git tag：fav/<时间戳>；强留存 + 恢复入口）。"""
        proj = st().get_project(name)
        r = proj.favorite()
        if not r.get("ok"):
            raise HTTPException(400, r.get("error") or "收藏失败")
        return r

    @app.get("/api/projects/{name}/favorites")
    def favorites(name: str) -> dict:
        return {"favorites": st().get_project(name).favorites()}

    @app.post("/api/projects/{name}/play")
    def play(name: str) -> dict:
        """后端宿主播放（sounddevice 阻塞到播完；一次只跑一个，忙时 409）。"""
        proj = st().get_project(name)
        if not st().play_lock.acquire(blocking=False):
            raise HTTPException(409, "已有播放在进行中")
        bus = st().bus
        duration = score_duration(proj.score)
        try:
            bus.publish(name, "playback_start", {"duration": duration})
            engine = st().engine()
            engine.play(engine.load(proj.score), blocking=True)
            bus.publish(name, "playback_stop", {"duration": duration})
            return {"ok": True, "duration": duration}
        finally:
            st().play_lock.release()

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
                      body.annotations, body.selection),
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
