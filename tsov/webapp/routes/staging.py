"""暂存区域路由（M-V8 E4 段1 · Q10.5）：AI/链产物默认先进暂存，人批才落地。

- list / adopt / discard / artifact（试听）
- 采纳执行 = 命令层事务（链复用 routes.chain.apply_chain_run——同一动作路径，ADR-0017）
- 条目枚举与处置记录见 tsov/staging.py；「暂存零残留」= 丢弃不动工程（score.json sha 不变、
  链 run 目录保留为历史）
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from ... import staging
from ..state import WebState
from .chain import apply_chain_run

_FILE_MEDIA = {"wav": "audio/wav", "flac": "audio/flac", "mp3": "audio/mpeg",
               "mid": "audio/midi", "json": "application/json"}


def register(app: FastAPI) -> None:
    def st() -> WebState:
        return app.state.tsov

    @app.get("/api/projects/{name}/staging")
    def staging_list(name: str) -> dict:
        """暂存区列表（pending 在前 + 已处置历史）。"""
        proj = st().get_project(name)
        return {"project": name, **staging.list_items(proj.root)}

    @app.post("/api/projects/{name}/staging/{item_id}/adopt")
    def staging_adopt(name: str, item_id: str, body: dict | None = None) -> dict:
        """采纳（真一键）——producer 分发；chain：双轨进工程（命令层事务）。"""
        body = body or {}
        proj = st().get_project(name)
        item = staging.get_item(proj.root, str(item_id))
        if item is None:
            raise HTTPException(404, f"暂存条目不存在：{item_id!r}")
        if item["state"] == "adopted":
            return {"ok": True, "already": True, "item_id": item["id"]}
        if item["producer"] != "chain":
            raise HTTPException(400, f"段1 仅支持链产物采纳：{item['producer']!r}")
        if not item.get("ready"):
            miss = "、".join(item["meta"].get("missing") or []) or "产物"
            raise HTTPException(409, f"链产物未跑完（缺 {miss}）——先补齐转录与处理步")
        r = apply_chain_run(st(), proj, run_ts=item["refs"]["run_ts"],
                            source_track=body.get("source_track"))
        if not r.get("ok"):
            return {**r, "item_id": item["id"]}
        rec = staging.set_state(proj.root, item["id"], "adopted")
        return {**r, "item_id": item["id"], "record": rec}

    @app.post("/api/projects/{name}/staging/{item_id}/discard")
    def staging_discard(name: str, item_id: str) -> dict:
        """丢弃（真一键）——仅记处置：不动工程、不删产物（链 run 目录保留为历史）。"""
        proj = st().get_project(name)
        item = staging.get_item(proj.root, str(item_id))
        if item is None:
            raise HTTPException(404, f"暂存条目不存在：{item_id!r}")
        rec = staging.set_state(proj.root, item["id"], "discarded")
        return {"ok": True, "item_id": item["id"], "record": rec}

    @app.get("/api/projects/{name}/staging/{item_id}/artifact")
    def staging_artifact(name: str, item_id: str, file: str):
        """条目产物试听/下载（限该条目目录内，防穿越）。段1：链 run 目录（01/02 wav 等）。"""
        proj = st().get_project(name)
        item = staging.get_item(proj.root, str(item_id))
        if item is None:
            raise HTTPException(404, f"暂存条目不存在：{item_id!r}")
        if item["producer"] != "chain":
            raise HTTPException(400, f"段1 仅链产物可试听：{item['producer']!r}")
        base = (proj.root / "chain" / str(item["refs"]["run_ts"])).resolve()
        path = (base / file).resolve()
        if path != base and base not in path.parents:
            raise HTTPException(400, f"非法产物路径：{file!r}")
        if not path.is_file():
            raise HTTPException(404, f"产物不存在：{file}")
        media = _FILE_MEDIA.get(path.suffix.lower().lstrip("."), "application/octet-stream")
        return FileResponse(str(path), media_type=media, filename=path.name)
