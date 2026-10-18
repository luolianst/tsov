"""轨模板 REST（S4.2）：全局轨布局的列表/保存/应用。

应用模板严格走命令层 `EditBatch`（add_track / mount_source / set_instrument /
set_track_mix / add_effect）；模板不存音符。
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ...host import EditBatch
from ...sources import SourceError, SourceLibrary
from ...track_templates import TemplateError, TrackTemplateLibrary
from ..helpers import project_state, saved_at_of
from ..models import TrackTemplateApplyIn, TrackTemplateSaveIn


def register(app: FastAPI) -> None:
    def st():
        return app.state.tsov

    def _lib() -> TrackTemplateLibrary:
        return TrackTemplateLibrary()

    @app.get("/api/templates")
    def list_templates() -> dict:
        lib = _lib()
        rows = lib.list()
        return {"templates": rows, "errors": lib.errors}

    @app.post("/api/projects/{name}/template")
    def save_template(name: str, body: TrackTemplateSaveIn) -> dict:
        proj = st().get_project(name)
        try:
            tpl = _lib().save_from_score(body.name, proj.score,
                                         title=body.title, overwrite=body.overwrite)
        except TemplateError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "template": {"name": tpl.name, "title": tpl.title,
                                         "tracks": len(tpl.tracks)}}

    @app.post("/api/projects/{name}/template/apply")
    def apply_template(name: str, body: TrackTemplateApplyIn) -> dict:
        proj = st().get_project(name)
        try:
            tpl = _lib().get(body.name)
        except TemplateError as exc:
            raise HTTPException(400, str(exc)) from exc

        # 预检源引用：缺失/坏源直接拒绝，避免批量执行到一半留下半套轨
        if any(str(t.get("source_id") or "") for t in tpl.tracks):
            try:
                sources = SourceLibrary()
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(400, f"音源库读取失败：{exc}") from exc
            for i, lay in enumerate(tpl.tracks):
                sid = str(lay.get("source_id") or "")
                if not sid:
                    continue
                try:
                    src = sources.get(sid)
                except SourceError as exc:
                    raise HTTPException(400, f"模板第 {i + 1} 轨音源不可用：{exc}") from exc
                if src.missing:
                    raise HTTPException(400, f"模板第 {i + 1} 轨音源缺失：{src.name}（{sid}）")

        batch = EditBatch(label=f"应用轨模板：{tpl.name}")
        for cmd in tpl.commands(start_index=len(proj.score.tracks)):
            batch.add(str(cmd["op"]), track=int(cmd.get("track", 0)),
                      value=cmd.get("value"))
        result = proj.apply_batch(batch, commit_message=f"应用轨模板：{tpl.name}", source="user")
        if result.get("changed"):
            st().bus.publish(name, "diff_applied", {
                **result["diff"], "commit": result.get("commit"), "seq": result.get("seq")})
            st().bus.publish(name, "state_updated", project_state(proj))
            result["saved_at"] = saved_at_of(proj)
        return result
