"""留存域路由：收藏 / 快照窗口 / 留存设置 / 动作日志与撤销（M-V7 D2-D3，ADR-0019；F5 自 tsov/web.py 拆出）。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ...core.score import Score
from ...host.state import ProjectState, global_settings_path, set_global_settings  # M-V7 D2（ADR-0019）：计数/设置
from ..helpers import project_state
from ..models import SettingsIn, WindowJumpIn
from ..state import WebState


def _rounds(entries: list[dict]) -> list[dict]:
    """按对话轮（round）聚合 agent 写动作（对话产物流 A 件）。

    条目顺序即轮内发生顺序；stats 为轮内计数合计；label_counts 便于卡面归纳。
    """
    out: list[dict] = []
    idx: dict[str, int] = {}
    for e in entries:
        if e.get("source") != "agent" or not e.get("tool"):
            continue   # 会话任务条目（tool=None）不进轮聚合
        rk = str(e.get("round") or e.get("session_id") or "-")
        if rk not in idx:
            idx[rk] = len(out)
            out.append({"round": rk, "seqs": [], "label_counts": {}, "stats": {},
                        "ts_first": e.get("ts"), "ts_last": e.get("ts"),
                        "n": 0, "n_stale": 0})
        agg = out[idx[rk]]
        agg["seqs"].append(int(e.get("seq") or 0))
        lbl = str(e.get("label") or e.get("tool") or "")
        if lbl:
            agg["label_counts"][lbl] = agg["label_counts"].get(lbl, 0) + 1
        for k, v in (e.get("stats") or {}).items():
            try:
                agg["stats"][k] = agg["stats"].get(k, 0) + int(v)
            except (TypeError, ValueError):
                continue
        agg["n"] += 1
        if e.get("stale"):
            agg["n_stale"] += 1
        agg["ts_last"] = e.get("ts")
    return out


def register(app: FastAPI) -> None:
    def st() -> WebState:
        return app.state.tsov

    # ---------------- 收藏（修正轮2） ----------------

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
        """动作快照日志（窗口内最近 limit 条；供前端回放与失效置灰）。

        对话产物流（A 件）：附 rounds —— 按对话轮聚合的统计块（向后兼容）。
        """
        proj = st().get_project(name)
        j = proj.journal
        entries = j.entries[-max(1, int(limit)):]
        return {"entries": entries, "rounds": _rounds(entries)}

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
