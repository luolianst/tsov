"""建议引擎（M-V8 E4 段2 · Q3–Q6）：混合双通道。

- 确定性通道（差量型 = 计算）：目标电平差 → volume；混音削波 → 减档；声像碰撞 → 展开
- LLM 通道（模式识别型）：掩蔽/空间/冲突/效果链——数值必须过校验（键 + 范围）
- 理由统一 LLM 写（LLM 缺席/失败 → 公式化理由兜底，诚实降级不阻塞）
- 红线（Q5）：每条建议可溯源（evidence.refs 指向事实键路径）；数值要么来自计算、
  要么过校验；拒绝 = 重试一次，再不过丢弃并记日志——绝不「修复」数值
- 批次落盘走 store；本模块不碰工程 score（应用在 apply.py + 路由的命令层事务）
"""

from __future__ import annotations

import itertools
import json
import time

from ..host.effect import effect_kinds, param_spec
from ..llm_client import LLM_MODEL, LlmRequestError, chat_post_json, extract_json_object, resolve_api_key
from ..presets import load_library
from . import facts as facts_mod
from . import spectrum, store

MIN_LEVEL_MOVE_DB = 1.5       # 电平差小于此不生成建议
MAX_LEVEL_STEP_DB = 12.0      # 单条建议单步调整上限（余量留复测迭代）
CLIP_TARGET_DBFS = -1.0       # 削波处置目标：峰值降到 -1 dBFS
PAN_COLLIDE_MIN_COS = 0.92    # 声像碰撞：频谱余弦相似阈
PAN_COLLIDE_MAX_PAN = 0.12    # 双方都近似居中才展开
PAN_COLLIDE_MAX_REL_SPREAD = 8.0   # 双轨醒目度相当（相对电平差 ≤ 此值）
PAN_SPREAD_TO = 0.35
PAN_MATCH_MIN_NOTES = 8       # 参与碰撞判定的轨至少这么多音符
LLM_MAX_DELTA_DB = 6.0        # LLM 电平建议允许的单步幅度


# ---------------------------------------------------------------------------
# 确定性通道（差量型 = 计算）
# ---------------------------------------------------------------------------


def _vol_after(cur: float, delta_db: float) -> float:
    """按 10^(−Δ/20) 换算音量（Δ>0 = 降），夹到命令层 domain [0, 2]。"""
    return round(min(2.0, max(0.0, float(cur) * (10.0 ** (-float(delta_db) / 20.0)))), 4)


def deterministic_suggestions(facts: dict) -> list[dict]:
    """差量型建议（全部数值由计算得出；evidence 指向事实键路径）。"""
    out: list[dict] = []
    levels = facts.get("levels") or {}
    rows = {int(r["index"]): r for r in (levels.get("tracks") or [])}
    structure = {int(t["index"]): t for t in ((facts.get("structure") or {}).get("tracks") or [])}

    # ---- 目标电平差 ----
    targets, pole = facts_mod.target_relatives(facts)
    for i in sorted(targets):
        row, st = rows.get(i), structure.get(i) or {}
        if not row or row.get("rel_db") is None or row.get("silent"):
            continue
        meas = facts_mod.measured_relative(facts, i, pole)
        if meas is None:
            continue
        diff = round(meas - float(targets[i]), 2)
        if abs(diff) < MIN_LEVEL_MOVE_DB:
            continue
        move = max(-MAX_LEVEL_STEP_DB, min(MAX_LEVEL_STEP_DB, diff))
        if abs(move) < MIN_LEVEL_MOVE_DB:
            continue
        vol = float(st.get("volume", 1.0))
        new_vol = _vol_after(vol, move)
        if abs(new_vol - vol) < 5e-4:
            continue
        capped = abs(diff) > MAX_LEVEL_STEP_DB + 1e-9
        out.append({
            "source": "det", "kind": "level", "track": i,
            "title": f"调平「{row['name']}」（{move:+.1f} dB 本步）",
            "values": {"delta_db": round(move, 2), "raw_diff_db": diff,
                       "target_rel_db": round(float(targets[i]), 2), "measured_rel_db": meas,
                       "before_volume": vol, "after_volume": new_vol, "capped": capped},
            "evidence": {"refs": [f"levels.tracks[{i}].rel_db", f"targets.per_track.{i}.level_hint_db",
                                  "levels.anchor"],
                         "text": f"整段相对电平 {meas:+.1f} dB（极轨 {pole}）vs 目标 {float(targets[i]):+.1f} dB"},
        })

    # ---- 混音削波 ----
    peak = levels.get("mix_peak_dbfs")
    if peak is not None and float(peak) > CLIP_TARGET_DBFS:
        loud = [r for r in rows.values() if r.get("rel_db") is not None and not r.get("silent")]
        if loud:
            top = max(loud, key=lambda x: float(x["rel_db"]))
            i = int(top["index"])
            reduce_db = round(float(peak) - CLIP_TARGET_DBFS, 2)
            vol = float((structure.get(i) or {}).get("volume", 1.0))
            new_vol = _vol_after(vol, reduce_db)
            if abs(new_vol - vol) >= 5e-4:
                out.append({
                    "source": "det", "kind": "level", "track": i,
                    "title": f"混音削波：降「{top['name']}」{reduce_db:.1f} dB",
                    "values": {"delta_db": -reduce_db, "raw_diff_db": -reduce_db,
                               "target_rel_db": None, "measured_rel_db": top["rel_db"],
                               "before_volume": vol, "after_volume": new_vol, "capped": False},
                    "evidence": {"refs": ["levels.mix_peak_dbfs"],
                                 "text": f"混音峰值 {float(peak):+.1f} dBFS（目标 ≤ {CLIP_TARGET_DBFS:g}）"},
                })

    # ---- 声像碰撞（纯频谱 + 声像；不依赖 targets） ----
    spec = {int(s["index"]): list(s.get("bands") or [])
            for s in ((facts.get("spectrum") or {}).get("tracks") or [])}
    cand = [r for r in rows.values()
            if not r.get("silent") and r.get("rel_db") is not None
            and int((structure.get(int(r["index"])) or {}).get("notes", 0)) >= PAN_MATCH_MIN_NOTES
            and "drum" not in ((structure.get(int(r["index"])) or {}).get("families") or [])]
    for a, b in itertools.combinations(cand, 2):
        ia, ib = int(a["index"]), int(b["index"])
        pa = float((structure.get(ia) or {}).get("pan", 0.0))
        pb = float((structure.get(ib) or {}).get("pan", 0.0))
        if abs(pa) > PAN_COLLIDE_MAX_PAN or abs(pb) > PAN_COLLIDE_MAX_PAN:
            continue
        if abs(float(a["rel_db"]) - float(b["rel_db"])) > PAN_COLLIDE_MAX_REL_SPREAD:
            continue
        cos = spectrum.cosine(spec.get(ia) or [], spec.get(ib) or [])
        if cos < PAN_COLLIDE_MIN_COS:
            continue
        for i, to in ((ia, -PAN_SPREAD_TO), (ib, PAN_SPREAD_TO)):
            other = rows[ib] if i == ia else rows[ia]
            out.append({
                "source": "det", "kind": "pan", "track": i,
                "title": f"声像展开「{rows[i]['name']}」→ {to:+.2f}",
                "values": {"pan": to, "before_pan": float((structure.get(i) or {}).get("pan", 0.0))},
                "evidence": {"refs": [f"spectrum.tracks[{ia}].bands", f"spectrum.tracks[{ib}].bands",
                                      f"structure.tracks[{i}].pan"],
                             "text": f"与「{other['name']}」频谱余弦 {cos:.2f}（≥{PAN_COLLIDE_MIN_COS}）且双方居中"},
            })
    return out


# ---------------------------------------------------------------------------
# LLM 通道（模式识别型 = 值必过校验）
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "你是 tsov 的混音调参助手。只根据给定事实提建议，输出单个 JSON 对象，不要任何多余文字。\n"
    "产出两部分：\n"
    "1) reasons：为 deterministic 列表（确定性建议）逐条写中文理由，引用具体数值；\n"
    "2) suggestions：你自己的模式识别型建议（掩蔽/空间拥挤/音色冲突/效果链）——宁缺毋滥，没把握就不写。\n"
    "JSON 形状：{\"reasons\":[{\"i\":0,\"reason\":\"...\"}],\"suggestions\":[...]}\n"
    "suggestion 只允许以下四形（选其一）：\n"
    "- {\"kind\":\"level\",\"track\":N,\"delta_db\":-3.0,...}：delta_db ∈ [-6,6]，负=提升（例：-3.0 = 比现值提高约 3 dB；正数=降低）\n"
    "- {\"kind\":\"pan\",\"track\":N,\"pan\":-0.4,...}：pan ∈ [-1,1]\n"
    "- {\"kind\":\"effect\",\"track\":N,\"preset\":\"<预设名>\",...}：预设从 effect_presets 选\n"
    "- {\"kind\":\"effect\",\"track\":N,\"effect\":{\"type\":\"highpass\",\"params\":{...}},...}："
    "type 从 effect_types 选，params 键必须在 effect_params 对应范围内且数值在区间内\n"
    "- {\"kind\":\"effect\",\"track\":N,\"set_params\":{\"index\":k,\"params\":{...}},...}："
    "改既有链第 k 个效果（见轨 effects 字段），params 同上受 effect_params 约束\n"
    "每条都要 title（≤40字）、reason（引用事实数值）、evidence（写你依据的事实键路径或摘要）。\n"
    "红线：数值越界 / 字段拼错 / 依据不足 = 该条作废；不要输出无法落地的建议。"
)


def _param_check(etype: str, params: dict) -> tuple[dict, str | None]:
    """效果参数校验（键 + 数值范围）→ (规范化 params, 错误文本|None)。"""
    spec = param_spec(etype)
    clean: dict = {}
    for key, val in (params or {}).items():
        if key not in spec:
            return {}, f"{etype}: 未知参数键 {key!r}"
        rng = spec.get(key)
        if rng:
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                return {}, f"{etype}.{key} 非数值：{val!r}"
            v = float(val)
            if not (float(rng[0]) - 1e-9 <= v <= float(rng[1]) + 1e-9):
                return {}, f"{etype}.{key} 越界：{v} 不在 {rng}"
            clean[key] = round(v, 6)
        else:
            clean[key] = val
    return clean, None


def validate_llm_suggestions(raw, facts: dict, *, presets_root=None) -> tuple[list[dict], list[dict]]:
    """逐条校验（绝不修复数值）：通过 → 规范化建议；不过 → dropped 记原因。"""
    valid: list[dict] = []
    dropped: list[dict] = []
    structure = {int(t["index"]): t for t in ((facts.get("structure") or {}).get("tracks") or [])}
    levels = {int(r["index"]): r for r in ((facts.get("levels") or {}).get("tracks") or [])}
    lib = load_library(presets_root)
    preset_names = set(lib.effects)
    kind_names = set(effect_kinds()) - {"vst3"}

    def bad(k: int, why: str) -> None:
        dropped.append({"_index": k, "why": why,
                        "raw": raw[k] if isinstance(raw, list) and k < len(raw) else None})

    for k, s in enumerate(raw or []):
        if not isinstance(s, dict):
            bad(k, f"非对象：{type(s).__name__}")
            continue
        kind = str(s.get("kind") or "")
        if kind not in ("level", "pan", "effect"):
            bad(k, f"未知 kind：{kind!r}")
            continue
        try:
            ti = int(s.get("track"))
        except (TypeError, ValueError):
            bad(k, f"track 非法：{s.get('track')!r}")
            continue
        if ti not in structure:
            bad(k, f"track 不存在：{ti}")
            continue
        row, lrow = structure[ti], levels.get(ti) or {}
        if lrow.get("silent") or lrow.get("rms_dbfs") is None:
            bad(k, f"track {ti} 无内容")
            continue
        reason = str(s.get("reason") or "").strip()
        if not reason:
            bad(k, "缺 reason")
            continue
        reason = reason[:600]
        title = str(s.get("title") or "").strip()[:80]
        ev = str(s.get("evidence") or "").strip()[:300]
        cur_vol = float(row.get("volume", 1.0))
        cur_pan = float(row.get("pan", 0.0))

        if kind == "level":
            try:
                delta = float(s.get("delta_db"))
            except (TypeError, ValueError):
                bad(k, f"delta_db 非法：{s.get('delta_db')!r}")
                continue
            if not (-LLM_MAX_DELTA_DB - 1e-9 <= delta <= LLM_MAX_DELTA_DB + 1e-9):
                bad(k, f"delta_db 越界：{delta}（±{LLM_MAX_DELTA_DB:g}）")
                continue
            if abs(delta) < 0.1:
                bad(k, "delta 无实际变化")
                continue
            new_vol = _vol_after(cur_vol, delta)
            if abs(new_vol - cur_vol) < 5e-4:
                bad(k, "音量无实际变化")
                continue
            valid.append({
                "source": "llm", "kind": "level", "track": ti,
                "title": title or f"电平微调「{row['name']}」（{delta:+.1f} dB）",
                "reason": reason, "evidence": {"refs": [], "text": ev},
                "values": {"delta_db": round(delta, 2), "raw_diff_db": None, "target_rel_db": None,
                           "measured_rel_db": lrow.get("rel_db"), "before_volume": cur_vol,
                           "after_volume": new_vol, "capped": False},
            })
        elif kind == "pan":
            try:
                p = float(s.get("pan"))
            except (TypeError, ValueError):
                bad(k, f"pan 非法：{s.get('pan')!r}")
                continue
            if not (-1.0 - 1e-9 <= p <= 1.0 + 1e-9):
                bad(k, f"pan 越界：{p}")
                continue
            if abs(p - cur_pan) < 0.01:
                bad(k, "pan 无实际变化")
                continue
            valid.append({
                "source": "llm", "kind": "pan", "track": ti,
                "title": title or f"声像调整「{row['name']}」→ {p:+.2f}",
                "reason": reason, "evidence": {"refs": [], "text": ev},
                "values": {"pan": round(p, 4), "before_pan": cur_pan},
            })
        else:  # effect
            spec: dict | None = None
            desc = ""
            if s.get("preset"):
                nm = str(s["preset"]).strip()
                if nm not in preset_names:
                    bad(k, f"未知效果预设：{nm!r}")
                    continue
                spec, desc = {"preset": nm}, f"效果预设「{nm}」"
            elif isinstance(s.get("effect"), dict):
                e = s["effect"] or {}
                etype = str(e.get("type") or "").strip()
                if etype not in kind_names:
                    bad(k, f"未知效果类型：{etype!r}")
                    continue
                params = e.get("params") or {}
                if not isinstance(params, dict):
                    bad(k, "effect.params 需为对象")
                    continue
                clean, why = _param_check(etype, params)
                if why:
                    bad(k, why)
                    continue
                spec, desc = {"effect": {"type": etype, "params": clean}}, f"效果「{etype}」"
            elif isinstance(s.get("set_params"), dict):
                sp = s["set_params"] or {}
                try:
                    eidx = int(sp.get("index"))
                except (TypeError, ValueError):
                    bad(k, f"set_params.index 非法：{sp.get('index')!r}")
                    continue
                effs = list(row.get("effects") or [])
                if not (0 <= eidx < len(effs)):
                    bad(k, f"set_params.index 越界：{eidx}（共 {len(effs)} 个效果）")
                    continue
                etype = str((effs[eidx] or {}).get("type") or "")
                params = sp.get("params") or {}
                if not isinstance(params, dict) or not params:
                    bad(k, "set_params.params 需为非空对象")
                    continue
                clean, why = _param_check(etype, params)
                if why:
                    bad(k, why)
                    continue
                spec, desc = {"set_params": {"index": eidx, "params": clean}}, f"改效果「{etype}[{eidx}]」"
            else:
                bad(k, "effect 建议形状不支持（preset / effect / set_params 三选一）")
                continue
            valid.append({
                "source": "llm", "kind": "effect", "track": ti,
                "title": title or f"效果建议：{desc}",
                "reason": reason, "evidence": {"refs": [], "text": ev},
                "effect": spec, "values": {},
            })
    return valid, dropped


def _llm_payload(facts: dict, det: list[dict], context_md: str, *, presets_root=None) -> dict:
    """压缩事实（LLM 上下文）：结构 + 电平 + 频谱 + 目标 + 既有效果；不含逐段全量表。"""
    structure = facts.get("structure") or {}
    compact_tracks = [{k: t.get(k) for k in ("index", "name", "kind", "notes", "register",
                                             "program", "volume", "pan", "effects", "families")}
                      for t in structure.get("tracks") or []]
    lib = load_library(presets_root)
    return {
        "project": facts.get("project"),
        "pack": facts.get("pack"),
        "structure": {"tempo": structure.get("tempo"), "time_signature": structure.get("time_signature"),
                      "key": structure.get("key"), "sections": structure.get("sections"),
                      "tracks": compact_tracks},
        "levels": {"anchor": (facts.get("levels") or {}).get("anchor"),
                   "mix_peak_dbfs": (facts.get("levels") or {}).get("mix_peak_dbfs"),
                   "clipping": (facts.get("levels") or {}).get("clipping"),
                   "tracks": [{k: r.get(k) for k in ("index", "name", "rms_dbfs", "loudest_win_dbfs",
                                                     "rel_db", "silent")}
                              for r in ((facts.get("levels") or {}).get("tracks") or [])]},
        "spectrum": (facts.get("spectrum") or {}),
        "targets": {"source": (facts.get("targets") or {}).get("source"),
                    "per_track": (facts.get("targets") or {}).get("per_track")},
        "deterministic": [{"i": k, "kind": s.get("kind"), "track": s.get("track"),
                           "title": s.get("title"), "values": s.get("values")}
                          for k, s in enumerate(det)],
        "context_md": (context_md or "")[:6000],
        "effect_presets": sorted(lib.effects),
        "effect_types": [k for k in effect_kinds() if k != "vst3"],
        "effect_params": {k: param_spec(k) for k in effect_kinds() if k != "vst3"},
    }


def llm_suggestions(facts: dict, det: list[dict], *, context_md: str = "", key: str | None = None,
                    presets_root=None, post=None) -> dict:
    """LLM 通道：一次调用（不合形状/请求失败 → 重试一次，再不过丢通道并记日志）。

    post 注入：``post(payload_dict) -> str``（返回模型文本；测试用）。
    """
    key = key if key is not None else resolve_api_key()
    if not key and post is None:
        return {"suggestions": [], "reasons": {}, "dropped": [],
                "errors": ["无 API key——跳过 LLM 通道（只走确定性通道）"], "attempts": 0}

    payload = _llm_payload(facts, det, context_md, presets_root=presets_root)
    messages = [{"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]

    def _post(p: dict) -> str:
        if post is not None:
            return str(post(p))
        resp = chat_post_json({"model": LLM_MODEL, "messages": messages, "temperature": 0.2},
                              api_key=key or "")
        return str(resp["choices"][0]["message"]["content"])

    errors: list[str] = []
    dropped: list[dict] = []
    for attempt in (1, 2):
        try:
            text = _post(payload)
        except (LlmRequestError, Exception) as e:  # noqa: BLE001 —— 网络/协议层失败统一重试一次
            errors.append(f"第{attempt}次请求失败：{type(e).__name__}: {e}")
            continue
        data = extract_json_object(text)
        if (not isinstance(data, dict) or not isinstance(data.get("suggestions"), list)
                or not isinstance(data.get("reasons"), list)):
            errors.append(f"第{attempt}次响应不合形状（需 {{suggestions:[...], reasons:[...]}} 对象）")
            continue
        valid, dropped = validate_llm_suggestions(data.get("suggestions") or [], facts,
                                                  presets_root=presets_root)
        reasons: dict[int, str] = {}
        for item in data.get("reasons") or []:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("i"))
            except (TypeError, ValueError):
                continue
            rs = str(item.get("reason") or "").strip()
            if rs:
                reasons[idx] = rs[:600]
        return {"suggestions": valid, "reasons": reasons, "dropped": dropped,
                "errors": errors, "attempts": attempt}
    return {"suggestions": [], "reasons": {}, "dropped": dropped, "errors": errors, "attempts": 2}


# ---------------------------------------------------------------------------
# 合并 + 批次
# ---------------------------------------------------------------------------


def _formula_reason(s: dict) -> str:
    """确定性建议的公式化理由（LLM 缺席时的诚实兜底）。"""
    v = s.get("values") or {}
    if s.get("kind") == "level":
        cap = "（本步封顶 ±12 dB，余量留复测迭代）" if v.get("capped") else ""
        tgt = f"，目标 {v['target_rel_db']:+.1f} dB" if v.get("target_rel_db") is not None else ""
        raw = v.get("raw_diff_db")
        raw_txt = f"（差 {raw:+.1f} dB）" if isinstance(raw, (int, float)) else ""
        meas = v.get("measured_rel_db")
        meas_txt = f"{meas:+.1f} dB" if isinstance(meas, (int, float)) else "（无测量）"
        return (f"事实：相对电平 {meas_txt}{tgt}{raw_txt}"
                f"→ 按 10^(−Δ/20) 换算音量 {v.get('before_volume')}→{v.get('after_volume')}{cap}。")
    if s.get("kind") == "pan":
        return (f"事实：与另一轨频谱高度重合且双方居中 → 展开到 {v.get('pan'):+.2f}"
                f"（原 {v.get('before_pan'):+.2f}）。")
    return "（公式化理由不可用）"


def merge_suggestions(det: list[dict], llm: dict) -> list[dict]:
    """合并双通道 → 统一 id（t1…）；确定性建议补理由（LLM 优先，公式兜底）。"""
    reasons = llm.get("reasons") or {}
    for i, s in enumerate(det):
        r = reasons.get(i, reasons.get(str(i)))
        s["reason"] = str(r).strip() if isinstance(r, str) and str(r).strip() else _formula_reason(s)
    final = list(det) + [dict(x) for x in (llm.get("suggestions") or [])]
    for n, s in enumerate(final, 1):
        s["id"] = f"t{n}"
    return final


def generate_batch(proj_root, score, *, name: str | None = None, pack: str | None = None,
                   context_md: str = "", key: str | None = None, presets_root=None,
                   renderer=None, facts: dict | None = None, post=None) -> dict:
    """事实 → 双通道建议 → 合并 → 批次落盘 + 记账事件 → 批次 dict（含 facts 快照）。"""
    facts = facts or facts_mod.build_facts(proj_root, score, name=name, pack=pack,
                                           presets_root=presets_root, renderer=renderer)
    det = deterministic_suggestions(facts)
    llm = llm_suggestions(facts, det, context_md=context_md, key=key,
                          presets_root=presets_root, post=post)
    suggestions = merge_suggestions(det, llm)
    ts = store.new_batch_ts(proj_root)
    batch = {
        "batch_ts": ts,
        "project": facts.get("project"),
        "pack": pack,
        "created_at": time.time(),
        "state": "pending",
        "suggestions": suggestions,
        "facts": facts,
        "stats": {"det": len(det), "llm": len(llm.get("suggestions") or []),
                  "dropped": len(llm.get("dropped") or []),
                  "llm_errors": [str(e) for e in (llm.get("errors") or [])],
                  "llm_attempts": int(llm.get("attempts") or 0)},
    }
    store.save_batch(proj_root, batch)
    store.append_event(proj_root, "suggest", {"batch_ts": ts, "n": len(suggestions),
                                              "stats": batch["stats"]})
    return batch
