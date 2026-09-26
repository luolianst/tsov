"""工程上下文（agents.md 双层）路由（M-V8 E4 段1 · Q1.5/Q15）。

- GET  /agents       → 工程 agents.md + 全局 agents-user.md 现状（「AI」页签展示用）
- POST /agents/sync  → 重生成工程状态摘要块 + 确保全局文件建档（幂等）

写路径 = 本路由：UI 按钮与外部 agent 同径（ADR-0017 共享动作路径）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from ... import agents as agents_mod
from ..state import WebState


def _read(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def register(app: FastAPI) -> None:
    def st() -> WebState:
        return app.state.tsov

    @app.get("/api/projects/{name}/agents")
    def agents_get(name: str) -> dict:
        """工程上下文现状：工程 agents.md + 全局 agents-user.md 内容/路径。"""
        proj = st().get_project(name)
        up = agents_mod.user_agents_path()
        return {
            "project": name,
            "project_file": str(proj.root / agents_mod.FILE),
            "project_md": _read(proj.root / agents_mod.FILE),
            "user_file": str(up),
            "user_md": _read(up),
        }

    @app.post("/api/projects/{name}/agents/sync")
    def agents_sync(name: str) -> dict:
        """刷新工程状态摘要（确定性重生成）+ 全局偏好文件建档（幂等）。"""
        proj = st().get_project(name)
        r1 = agents_mod.sync_project_agents(proj.root, proj.score, name=proj.name)
        r2 = agents_mod.ensure_user_agents()
        return {"ok": True, "project": r1, "user": r2}
