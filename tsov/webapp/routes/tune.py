"""调参域路由（M-V8 E4 段2 · Q2–Q6）：analyze / suggest / apply / preview / discard / get / file。

- analyze：事实包（只读；pack 可选 = 风格包 level_hint 目标，缺省通用平衡口径）
- suggest：双通道建议 → 批次 <工程>/tune/<ts>.json（进暂存区 producer=tune，人批才落地）
- apply：勾选批量（缺省全选）→ before 对拍 → 命令层事务 EditBatch → after 对拍 → 应用小结
- preview：单条建议在副本上试跑渲染（工程零改动；A/B 试听）
- 记账：tune/events.jsonl 结构化事件 + agents.md 历史（LLM 提炼，失败降级公式文本）
  + agents-user.md 统计块（确定性累计）
- SSE：diff_applied / state_updated / tune_updated（另一通道的 UI 也刷新）
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from ... import agents as agents_mod
from ... import staging
from ...host import EditBatch
from ...host.cache import StemStore
from ...tune import apply as apply_mod
from ...tune import compare as compare_mod
from ...tune import measure, store
from ...tune import suggest as suggest_mod
from ...tune.facts import build_facts
from ..helpers import project_state
from ..state import WebState

_FILE_MEDIA = {"wav": "audio/wav", "flac": "audio/flac", "mp3": "audio/mpeg",
               "mid": "audio/midi", "json": "application/json"}


def _context_text(proj) -> str:
    """装配喂 LLM 的上下文：工程 agents.md + 全局 agents-user.md（工程在前=优先）。"""
    parts = []
    pr = proj.root / agents_mod.FILE
    if pr.is_file():
        try:
            parts.append("# 工程上下文（agents.md）\n" + pr.read_text(encoding="utf-8")[:5000])
        except OSError:
            pass
    up = agents_mod.user_agents_path()
    if up.is_file():
        try:
            parts.append("# 用户级偏好（agents-user.md）\n" + up.read_text(encoding="utf-8")[:3000])
        except OSError:
            pass
    return "\n\n".join(parts)


def _distill_history(batch: dict, sels: list[dict], report: dict) -> str | None:
    """LLM 从建议+对拍提炼 1–3 句决策日志；失败/无 key → None（走公式兜底）。"""
    from ...llm_client import LLM_MODEL, chat_post_json, resolve_api_key

    key = resolve_api_key()
    if not key:
        return None
    brief = {
        "batch_ts": batch.get("batch_ts"),
        "pack": batch.get("pack"),
        "applied": [{"id": s.get("id"), "kind": s.get("kind"), "track": s.get("track"),
                     "title": s.get("title"), "reason": str(s.get("reason"))[:200]} for s in sels],
        "report": [it.get("text") for it in (report.get("items") or [])],
    }
    try:
        resp = chat_post_json({
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": (
                    "你是录音棚助理。把这次调参应用的决策压缩成 1–3 句中文日志"
                    "（将写入工程 agents.md 的「历史与决策」区）：改了什么、为什么、结果如何。"
                    "只输出日志文本，不要标题、不要列表符号、不要引号。")},
                {"role": "user", "content": json.dumps(brief, ensure_ascii=False)},
            ],
            "temperature": 0.3,
        }, api_key=key)
        text = str(resp["choices"][0]["message"]["content"]).strip()
        return text[:300] if text else None
    except Exception:  # noqa: BLE001 —— 提炼失败不阻塞应用
        return None


def _formula_history(sels: list[dict], report: dict) -> str:
    kinds: dict[str, int] = {}
    for s in sels:
        k = str(s.get("kind") or "?")
        kinds[k] = kinds.get(k, 0) + 1
    kind_txt = "、".join(f"{k}×{v}" for k, v in kinds.items()) or "无"
    verdict = "；".join(str(it.get("text")) for it in (report.get("items") or [])[:3])
    return f"应用 {len(sels)} 条建议（{kind_txt}）。对拍：{verdict}"


def _render_mix(engine, proj, score, out: Path, cache=None) -> None:
    """渲染混音到 wav（对拍 before/after 与预览共用）。"""
    session = engine.load(score, base_dir=proj.root)
    try:
        engine.render(session, out_wav=str(out), stereo=True,
                      cache=cache or StemStore(proj.root))
    finally:
        session.close()


def register(app: FastAPI) -> None:
    def st() -> WebState:
        return app.state.tsov

    def _get_batch_or_404(proj, ts) -> dict:
        try:
            return store.load_batch(proj.root, ts)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    # ---------------- 事实 / 建议 ----------------

    @app.post("/api/projects/{name}/tune/analyze")
    def tune_analyze(name: str, body: dict | None = None) -> dict:
        """事实包（只读）：结构摘要 + 电平（全曲+逐段）+ 粗频谱 + 目标。"""
        body = body or {}
        proj = st().get_project(name)
        pack = body.get("pack") or None
        try:
            facts = build_facts(proj.root, proj.score, name=proj.name, pack=pack)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True, "project": name, "facts": facts}

    @app.post("/api/projects/{name}/tune/suggest")
    def tune_suggest(name: str, body: dict | None = None) -> dict:
        """生成建议批次（双通道）→ <工程>/tune/<ts>.json（暂存区 producer=tune）。"""
        body = body or {}
        proj = st().get_project(name)
        pack = body.get("pack") or None
        context = str(body.get("context") or "") or _context_text(proj)
        try:
            batch = suggest_mod.generate_batch(proj.root, proj.score, name=proj.name,
                                               pack=pack, context_md=context)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        st().bus.publish(name, "tune_updated", {"batch_ts": batch["batch_ts"], "state": "pending",
                                                "n": len(batch.get("suggestions") or [])})
        return {"ok": True, "project": name, "batch": batch}

    @app.get("/api/projects/{name}/tune/list")
    def tune_list(name: str) -> dict:
        proj = st().get_project(name)
        return {"ok": True, "project": name, "batches": store.list_batches(proj.root)}

    @app.get("/api/projects/{name}/tune/{batch_ts}")
    def tune_get(name: str, batch_ts: str) -> dict:
        proj = st().get_project(name)
        return {"ok": True, "project": name, "batch": _get_batch_or_404(proj, batch_ts)}

    # ---------------- 应用 / 试听 / 丢弃 ----------------

    @app.post("/api/projects/{name}/tune/apply")
    def tune_apply(name: str, body: dict | None = None) -> dict:
        """勾选批量应用（缺省全选）→ 命令层事务 + 自动对拍 + 记账（真一键）。"""
        body = body or {}
        proj = st().get_project(name)
        ts = str(body.get("batch_ts") or "")
        batch = _get_batch_or_404(proj, ts)
        try:
            commands, meta = apply_mod.batch_commands(batch, proj.score, ids=body.get("ids"))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if not commands:
            raise HTTPException(400, "没有可应用的命令（勾选集为空或建议无效）")

        cache = StemStore(proj.root)
        pack = batch.get("pack")
        facts_before = build_facts(proj.root, proj.score, name=proj.name, pack=pack, cache=cache)
        bdir = store.batch_dir(proj.root, ts)
        before_wav, after_wav = bdir / "before.wav", bdir / "after.wav"
        try:
            _render_mix(st().engine(), proj, proj.score, before_wav, cache)
        except Exception:  # noqa: BLE001 —— 试听渲染失败不阻塞应用
            pass
        lufs_before = measure.measure_lufs(before_wav) if before_wav.is_file() else None

        eb = EditBatch(label=f"调参应用（{ts}）")
        for c in commands:
            eb.add(str(c.get("op")), track=int(c.get("track", 0)), value=c.get("value"))
        result = proj.apply_batch(eb, commit_message=f"调参应用：{ts}（{meta['n']} 条建议）",
                                  source="user")
        if result.get("applied", 0) == 0:
            raise HTTPException(409, "全部命令被拒：" + "；".join(result.get("errors") or [])[:400])
        st().bus.publish(name, "diff_applied", {**(result.get("diff") or {}),
                                                "commit": result.get("commit"),
                                                "seq": result.get("seq")})
        st().bus.publish(name, "state_updated", project_state(proj))

        facts_after = build_facts(proj.root, proj.score, name=proj.name, pack=pack, cache=cache)
        try:
            _render_mix(st().engine(), proj, proj.score, after_wav, cache)
        except Exception:  # noqa: BLE001
            pass
        lufs_after = measure.measure_lufs(after_wav) if after_wav.is_file() else None
        files = {"before": before_wav.name if before_wav.is_file() else None,
                 "after": after_wav.name if after_wav.is_file() else None}
        report = compare_mod.build_report(facts_before, facts_after, applied_ids=meta["ids"],
                                          lufs_before=lufs_before, lufs_after=lufs_after,
                                          files=files)
        batch = store.update_batch(proj.root, ts, state="adopted", applied=meta["ids"],
                                   report=report, decided_at=time.time())
        try:
            staging.set_state(proj.root, f"tune:{ts}", "adopted", note=f"应用 {meta['n']} 条建议")
        except ValueError:
            pass
        store.append_event(proj.root, "apply",
                           {"batch_ts": ts, "ids": meta["ids"], "by_kind": meta["by_kind"],
                            "applied": result.get("applied"),
                            "errors": result.get("errors") or [],
                            "report_status": report.get("status")})

        # 记账：agents.md 历史（LLM 提炼→公式兜底）+ 全局统计（确定性累计）；失败不阻塞
        sels = [s for s in (batch.get("suggestions") or [])
                if str(s.get("id")) in set(meta["ids"])]
        try:
            if not (proj.root / agents_mod.FILE).is_file():
                agents_mod.sync_project_agents(proj.root, proj.score, name=proj.name)
            hist = _distill_history(batch, sels, report) or _formula_history(sels, report)
            agents_mod.append_history(proj.root, f"{time.strftime('%Y-%m-%d %H:%M')} · "
                                                  f"调参应用（{ts}）——{hist}")
            agents_mod.update_user_stats(delta=meta["by_kind"])
        except Exception:  # noqa: BLE001
            pass
        st().bus.publish(name, "tune_updated", {"batch_ts": ts, "state": "adopted", "n": meta["n"]})
        return {"ok": True, "project": name, "batch_ts": ts, "n": meta["n"],
                "applied": meta["ids"], "commands": len(commands),
                "errors": result.get("errors") or [], "report": report}

    @app.post("/api/projects/{name}/tune/preview")
    def tune_preview(name: str, body: dict | None = None) -> dict:
        """单条建议在副本上试跑渲染（A/B 试听；工程零改动）。body: {batch_ts, id}。"""
        body = body or {}
        proj = st().get_project(name)
        ts = str(body.get("batch_ts") or "")
        batch = _get_batch_or_404(proj, ts)
        sid = str(body.get("id") or "")
        s = next((x for x in (batch.get("suggestions") or []) if str(x.get("id")) == sid), None)
        if s is None:
            raise HTTPException(404, f"建议不存在：{sid!r}")
        try:
            new_score = apply_mod.preview_score(proj.score, s)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        out = store.batch_dir(proj.root, ts) / f"preview-{sid}.wav"
        _render_mix(st().engine(), proj, new_score, out)
        vals = dict(s.get("values") or {})
        if s.get("kind") == "level":
            cur = float(getattr(proj.score.tracks[int(s["track"])].instrument, "volume", 1.0) or 0.0)
            vals["before_volume"] = cur
            try:
                from ...tune.suggest import _vol_after

                vals["after_volume"] = _vol_after(cur, float(vals.get("delta_db")))
            except (TypeError, ValueError):
                pass
        store.append_event(proj.root, "preview", {"batch_ts": ts, "id": sid})
        return {"ok": True, "project": name, "batch_ts": ts, "id": sid,
                "file": out.name,
                "url": f"/api/projects/{name}/tune/{ts}/file?file={out.name}",
                "values": vals}

    @app.post("/api/projects/{name}/tune/discard")
    def tune_discard(name: str, body: dict | None = None) -> dict:
        """丢弃批次（真一键）——仅记处置：不动工程、不删文件。body: {batch_ts}。"""
        body = body or {}
        proj = st().get_project(name)
        ts = str(body.get("batch_ts") or "")
        _get_batch_or_404(proj, ts)
        rec = staging.set_state(proj.root, f"tune:{ts}", "discarded")
        store.update_batch(proj.root, ts, state="discarded", decided_at=time.time())
        store.append_event(proj.root, "discard", {"batch_ts": ts})
        st().bus.publish(name, "tune_updated", {"batch_ts": ts, "state": "discarded", "n": 0})
        return {"ok": True, "project": name, "batch_ts": ts, "record": rec}

    @app.get("/api/projects/{name}/tune/{batch_ts}/file")
    def tune_file(name: str, batch_ts: str, file: str):
        """批次音频（before/after/preview）试听；限批次目录内，防穿越。"""
        proj = st().get_project(name)
        _get_batch_or_404(proj, batch_ts)
        base = store.batch_dir(proj.root, batch_ts).resolve()
        path = (base / file).resolve()
        if path != base and base not in path.parents:
            raise HTTPException(400, f"非法产物路径：{file!r}")
        if not path.is_file():
            raise HTTPException(404, f"文件不存在：{file}")
        media = _FILE_MEDIA.get(path.suffix.lower().lstrip("."), "application/octet-stream")
        return FileResponse(str(path), media_type=media, filename=path.name)
