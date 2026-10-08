"""对话框 agent 域路由：chat / stop / reset / 会话列表与导入（F5 自 tsov/web.py 拆出）。

agent 会话落盘目录经 `config.AGENT_SESSION_DIR` 动态读取（测试打桩点）。
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException

from .. import config
from ..agent_session import _load_session_messages, _run_agent_session
from ..models import ChatIn, ChatResetIn, SessionLoadIn
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
        session_id = (
            conv["session_id"] if conv else f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        )
        state.chat_sessions[body.project] = {"session_id": session_id}
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
        state.chat_sessions.pop(body.project, None)
        return {"ok": True}

    @app.get("/api/sessions")
    def list_sessions() -> dict:
        """列出 agent 会话 JSONL（新→旧，最多 50 条），供导入回看/续接。"""
        d = Path(config.AGENT_SESSION_DIR)
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
        path = Path(config.AGENT_SESSION_DIR) / fname
        if path.suffix != ".jsonl" or not path.is_file():
            raise HTTPException(404, f"会话文件不存在：{fname!r}")
        sid = fname[: -len(".jsonl")]
        state.chat_sessions[body.project] = {"session_id": sid}
        return {"ok": True, "session_id": sid, "messages": _load_session_messages(path)}
