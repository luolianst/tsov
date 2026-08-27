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
import json
import threading
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .core.score import Score
from .host import EditBatch, Project

WEB_VERSION = "0.1.0"
AGENT_SESSION_DIR = "output/agent-sessions"
AGENT_MAX_TURNS = 12
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


class BatchIn(BaseModel):
    label: str = ""
    commands: list[dict]
    commit_message: str | None = None


class RollbackIn(BaseModel):
    rev: str = "HEAD~1"


class RenderIn(BaseModel):
    out: str | None = None


class ChatIn(BaseModel):
    project: str
    message: str


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

    def list_projects(self) -> list[str]:
        if not self.output_dir.is_dir():
            return []
        return sorted(
            p.name for p in self.output_dir.iterdir() if p.is_dir() and (p / "score.json").is_file()
        )

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
# agent 会话（后台线程；事件推 SSE；编辑结果采用进工程）
# ---------------------------------------------------------------------------


def _run_agent_session(state: WebState, project_name: str, task: str, session_id: str) -> None:
    """一次对话框 agent 会话（后台线程跑；agent_lock 由调用方获取，本函数 finally 释放）。"""
    from .agent import AgentSession, build_default_registry, chat as llm_chat
    from .agent.prompt import SYSTEM_PROMPT

    bus = state.bus
    proj = state.get_project(project_name)
    root = proj.root
    edited_path = root / EDITED_SCORE_NAME
    # 清掉上一轮残留——只采用「本轮」的编辑结果
    try:
        edited_path.unlink()
    except FileNotFoundError:
        pass

    score_path = root / "score.json"
    render_path = root / RENDER_WAV_NAME
    brief = (
        f"{task}\n\n"
        f"【工程上下文】\n"
        f"- 当前工程名：{proj.name}；工程 score 路径：{score_path}\n"
        f"- 改谱：用 edit_score 工具（score_path 用上面的工程 score 路径，feedback 写用户的修改要求），"
        f"工具会把新谱自动落盘为同目录的 {EDITED_SCORE_NAME}\n"
        f"- 改完谱自查：用 load_score 读 {EDITED_SCORE_NAME}，检查全部音高是否属于目标调式音阶"
        f"（如 D 多利亚 = D E F G A B C）；不纯（含调外音）就再用 edit_score 修一轮，直到纯净\n"
        f"- 自查通过后再用 render_wav 渲染新谱试听（score_path={edited_path}，out_wav={render_path}）\n"
        f"- 完成后用中文一句话汇报改动要点（含调式与音阶名）"
    )

    tool_count = 0
    turns = 0
    try:
        session = AgentSession(task=task, session_dir=AGENT_SESSION_DIR, session_id=session_id)
        session.add("system", SYSTEM_PROMPT)
        session.add("user", brief)
        registry = build_default_registry()

        answer = ""
        for turn in range(1, AGENT_MAX_TURNS + 1):
            turns = turn
            out = llm_chat(session.api_messages, tools=registry.openai_tools())
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
            },
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
            score = Score(title=body.name, tempo=120.0, key_candidates=[], tracks=[], meta={})
        else:
            score = _score_from_dict(body.score)
        proj = Project.create(body.name, score, parent=state.output_dir)
        with state.projects_lock:
            state.projects[body.name] = proj
        return {"ok": True, "name": body.name}

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
        out = Path(body.out) if body.out else proj.root / RENDER_WAV_NAME
        proj.save()  # score.json 落最新真相（渲染与 git 版本一致）
        engine = st().engine()
        session = engine.load(proj.score)
        try:
            audio = engine.render(session, out_wav=out)
        finally:
            session.close()
        return {"wav": str(out), "duration": round(len(audio) / engine.samplerate, 3), "sr": engine.samplerate}

    @app.get("/api/projects/{name}/wav")
    def wav(name: str):
        """浏览器试听 wav：缺失或比 score.json 旧时自动重渲（保证与工程真相一致）。"""
        from fastapi.responses import FileResponse

        proj = st().get_project(name)
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
        session_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        try:
            threading.Thread(
                target=_run_agent_session,
                args=(state, body.project, body.message, session_id),
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
