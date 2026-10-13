"""对话框 agent 域路由：chat / stop / reset / 会话列表与导入（F5 自 tsov/web.py 拆出）。

agent 会话落盘目录经 `config.AGENT_SESSION_DIR` 动态读取（测试打桩点）。
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException

from .. import config, session_store
from ..agent_session import _load_session_messages, _run_agent_session
from ..models import ChatIn, ChatResetIn, SessionDeleteIn, SessionLoadIn, SessionRenameIn
from ..state import WebState


def register(app: FastAPI) -> None:
    def st() -> WebState:
        return app.state.tsov

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
        if conv:
            session_id = conv["session_id"]
        else:
            # 批I（热载 L2）：内存无绑定时懒读盘恢复——服务重启后继续原会话
            restored = session_store.bound_session(body.project)
            if restored and (Path(config.AGENT_SESSION_DIR) / f"{restored}.jsonl").is_file():
                session_id = restored
            else:
                session_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        state.chat_sessions[body.project] = {"session_id": session_id}
        try:   # 批I：绑定持久化 + 元数据（失败不阻断对话主流程）
            session_store.save_binding(body.project, session_id)
            session_store.update_meta(session_id, project=body.project, status="open")
        except Exception:  # noqa: BLE001
            pass
        state.agent_stop.clear()
        try:
            threading.Thread(
                target=_run_agent_session,
                args=(state, body.project, body.message, session_id, body.base_rev,
                      body.annotations, body.selection, body.user_actions, body.refs),
                daemon=True,
                name=f"tsov-agent-{session_id[-8:]}",
            ).start()
        except Exception:
            state.agent_lock.release()
            raise
        return {
            "session_id": session_id,
            "session_path": str(Path(config.AGENT_SESSION_DIR) / f"{session_id}.jsonl"),
        }

    # ---------------- 会话控制 / 会话导入（M-V2.1 增补） ----------------

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
        old = state.chat_sessions.pop(body.project, None)
        try:   # 批I：结束续接——清持久化绑定 + 旧会话标记 closed（重启后不复活）
            sid = (old or {}).get("session_id") or session_store.bound_session(body.project)
            if sid:
                session_store.update_meta(sid, status="closed")
            session_store.clear_binding(body.project)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True}

    @app.get("/api/sessions")
    def list_sessions(project: str | None = None, all_projects: bool = False) -> dict:
        """列出 agent 会话（新→旧；批I：meta 富化——标题/工程/轮数/状态 + 过滤）。

        project 缺省 = 不过滤；all_projects=1 = 全部工程（默认过滤本工程 + 未归类旧会话）。
        """
        return {"sessions": session_store.list_sessions(project=project, all_flag=all_projects)}

    @app.post("/api/sessions/load")
    def load_session(body: SessionLoadIn) -> dict:
        """导入历史会话：消息回放给前端 + 设为当前工程的续接会话（接着聊）。"""
        state = st()
        state.get_project(body.project)
        if state.agent_lock.locked():
            raise HTTPException(409, "会话进行中，先停止再导入")
        fname = Path(body.name).name  # 只取 basename，防路径穿越
        path = Path(config.AGENT_SESSION_DIR) / fname
        if path.suffix != ".jsonl" or not path.is_file():
            raise HTTPException(404, f"会话文件不存在：{fname!r}")
        sid = fname[: -len(".jsonl")]
        state.chat_sessions[body.project] = {"session_id": sid}
        try:   # 批I：导入即绑定持久化 + 元数据补写（工程归属 / 开启态）
            session_store.save_binding(body.project, sid)
            session_store.update_meta(sid, project=body.project, status="open")
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "session_id": sid, "messages": _load_session_messages(path)}

    # ---------------- 批I：会话管理（改名 / 软删） ----------------

    @app.post("/api/sessions/rename")
    def rename_session(body: SessionRenameIn) -> dict:
        """会话改名（P14 配套）：写 meta.title；不触碰文件名（id 不变）。"""
        sid = Path(body.name).name
        if sid.endswith(".jsonl"):
            sid = sid[: -len(".jsonl")]
        path = Path(config.AGENT_SESSION_DIR) / f"{sid}.jsonl"
        if not path.is_file():
            raise HTTPException(404, f"会话文件不存在：{body.name!r}")
        try:
            meta = session_store.rename_session(sid, body.title)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "id": sid, "title": meta["title"]}

    @app.post("/api/sessions/delete")
    def delete_session(body: SessionDeleteIn) -> dict:
        """会话软删：移入 `.trash/`（可人工找回）；清绑定；运行中一律 409。"""
        state = st()
        if state.agent_lock.locked():
            raise HTTPException(409, "会话进行中，先停止再删除")
        sid = Path(body.name).name
        if sid.endswith(".jsonl"):
            sid = sid[: -len(".jsonl")]
        try:
            dest = session_store.soft_delete(sid)
        except FileNotFoundError:
            raise HTTPException(404, f"会话文件不存在：{body.name!r}")
        for proj, v in list(state.chat_sessions.items()):
            if isinstance(v, dict) and v.get("session_id") == sid:
                state.chat_sessions.pop(proj, None)
        return {"ok": True, "id": sid, "trashed_to": dest}
