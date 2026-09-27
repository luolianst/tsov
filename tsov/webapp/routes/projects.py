"""工程域路由：CRUD / state / batch（一切编辑走事务）/ undo-redo-rollback / log / summary / 改名。

F5 自 tsov/web.py 拆出（函数体逐行保留）。
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

from ...core.score import Instrument, Score, Track
from ...host import EditBatch, Project
from ..agent_session import _retention_tick
from ..helpers import _score_from_dict, project_state, saved_at_of
from ..models import BatchIn, ImportIn, ProjectCreate, RollbackIn, TitleIn
from ..state import WebState, _default_project_name, _resolve_import_source, _validate_project_name


def register(app: FastAPI) -> None:
    def st() -> WebState:
        return app.state.tsov

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

    @app.post("/api/projects/{name}/midi/import")
    async def midi_import(name: str, request: Request) -> dict:
        """MIDI 导入（E5）：JSON {path: 本机绝对路径, name?} 或 multipart file 上传（.mid/.midi）。

        - 路径来源：后端直读本机文件；上传来源：落临时文件后同路径导入（与音频入库同模式）
        - 导入语义：host/Project.import_midi（追加非空轨；空工程采纳文件元数据）
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
                suffix = Path(str(up.filename)).suffix or ".mid"
                incoming = proj.root / "midi" / ".incoming"
                incoming.mkdir(parents=True, exist_ok=True)
                tmp_path = incoming / f"upload-{uuid.uuid4().hex[:8]}{suffix}"
                tmp_path.write_bytes(await up.read())
                info = proj.import_midi(tmp_path, name=up_name)
            else:
                body = await request.json()
                src = str((body or {}).get("path") or "").strip().strip('"').strip("'")
                if not src:
                    raise HTTPException(400, "缺 path（本机 MIDI 绝对路径）")
                info = proj.import_midi(src, name=(body or {}).get("name"))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
        st().bus.publish(name, "state_updated", project_state(proj))
        return {"project": name, **info}

    @app.get("/api/projects/{name}/state")
    def get_state(name: str) -> dict:
        return project_state(st().get_project(name))

    @app.delete("/api/projects/{name}")
    def delete_project(name: str) -> dict:
        """删除工程（M-V8 E6 段3）：安全删——移入 `output/.trash/`（可手动找回）；打开中先安全关句柄。"""
        dest = st().delete_project(name)
        return {"ok": True, "name": name, "trashed_to": dest}

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
        result["saved_at"] = saved_at_of(proj)   # M-V8 E6 段2：保存徽章直读（不等 SSE）
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
