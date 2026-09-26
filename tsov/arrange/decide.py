"""配器决策（M-V8 E4 段3 · Q14 三段一库之④）：LLM 只选 ID，从不产音符坐标。

- 输出 = 计划（plan）：每角色 × 每段 → 变体 ID（+ 可选 fill ID）＋理由；坐标留给 expand 机械展开
- 校验（绝不「修复」）：role/section/variant/fill 必须来自事实与模式库枚举；重复 (role, section) 丢弃；
  不合形状/请求失败 = 整体重试一次；再不过 → 全量回默认策略，错误记入 stats
- 无 key / LLM 缺席 → 默认计划（default_by_section 兜底）——诚实降级不阻塞
- 首稿定位（Q7 补注）：先给「整体动力与段落对比」，细打磨在后（结合用户意见 + LLM 再迭代）
"""

from __future__ import annotations

import json

from ..llm_client import LLM_MODEL, LlmRequestError, chat_post_json, extract_json_object, resolve_api_key
from .library import PatternLibrary, load_patterns

_SYSTEM_PROMPT = (
    "你是 tsov 的配器编曲助手：根据歌曲结构事实，为每个角色（乐器）在每一段选择「变体 ID」，"
    "拼出一版有段落对比的伴奏初稿。输出单个 JSON 对象，不要任何多余文字。\n"
    "JSON 形状：{\"plan\":[{\"role\":\"<角色>\",\"section\":<段序号>,\"variant\":\"<变体 ID>\","
    "\"fill\":\"<fill ID> 或省略\",\"reason\":\"<≤60字中文理由>\"}, ...],\"overall\":\"<≤120字整体思路>\"}\n"
    "硬规则：\n"
    "1) role / section / variant / fill 只能取自用户消息里的事实枚举——不得编造 ID；\n"
    "2) 每个 (role, section) 至多一条；fill 只能配给 fill 声明的同一角色、通常落在段末小节；\n"
    "3) 尽量覆盖全部 (role × section)；缺的会回默认，但覆盖越全初稿越完整；\n"
    "4) 编配思路：段间对比（主歌收、副歌满、前/间奏留白或留旋律加倍）、声部不打架"
    "（低频让给贝斯与底鼓、中高频分层）、fills 别滥用（段落交界处才有）、别机械重复。\n"
    "这是给用户试听的初稿：优先整体动力与段落对比，细节留给后续打磨。"
)


def _llm_payload(facts: dict, context_md: str) -> dict:
    """压缩事实（LLM 上下文）：结构 + 段表 + 角色目录（含变体）+ fills + 用户偏好。"""
    return {
        "project": facts.get("project"),
        "pack": facts.get("pack"),
        "strength": facts.get("strength"),
        "key": facts.get("key"),
        "meter": {"time_signature": (facts.get("meter") or {}).get("time_signature"),
                  "bars_total": (facts.get("meter") or {}).get("bars_total")},
        "melody": facts.get("melody"),
        "sections": [{k: s.get(k) for k in ("index", "label", "kind", "bars", "energy",
                                            "melody_notes", "synthetic")}
                     for s in (facts.get("sections") or [])],
        "roles": facts.get("roles") or {},
        "fills": facts.get("fills") or {},
        "context_md": (context_md or "")[:6000],
    }


def _variant_map(lib: PatternLibrary) -> dict[str, set[str]]:
    return {role: set((lib.role(role).get("variants") or {}).keys()) for role in lib.role_ids()}


def validate_plan(raw, lib: PatternLibrary, facts: dict) -> tuple[list[dict], list[dict]]:
    """逐条校验计划行（绝不修复）：通过 → 规范化；不过 → dropped 记原因。"""
    valid: list[dict] = []
    dropped: list[dict] = []
    sec_ids = {int(s["index"]) for s in (facts.get("sections") or [])}
    varmap = _variant_map(lib)
    seen: set[tuple[str, int]] = set()

    def bad(k: int, why: str, row=None) -> None:
        dropped.append({"_index": k, "why": why, "raw": row if row is not None else None})

    for k, row in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(row, dict):
            bad(k, f"非对象：{type(row).__name__}")
            continue
        role = str(row.get("role") or "").strip()
        if role not in varmap:
            bad(k, f"未知角色：{role!r}", row)
            continue
        try:
            sec = int(row.get("section"))
        except (TypeError, ValueError):
            bad(k, f"section 非法：{row.get('section')!r}", row)
            continue
        if sec not in sec_ids:
            bad(k, f"段不存在：{sec}", row)
            continue
        key = (role, sec)
        if key in seen:
            bad(k, f"重复的 (role, section)：{key}", row)
            continue
        vid = str(row.get("variant") or "").strip()
        if vid not in varmap[role]:
            bad(k, f"{role} 无此变体：{vid!r}", row)
            continue
        fill = row.get("fill")
        fill_id = None
        if fill not in (None, "", False):
            fill_id = str(fill).strip()
            fdef = lib.fills.get(fill_id)
            if fdef is None:
                bad(k, f"fill 不存在：{fill_id!r}", row)
                continue
            if str(fdef.get("role")) != role:
                bad(k, f"fill {fill_id!r} 属于 {fdef.get('role')!r}，不能配给 {role!r}", row)
                continue
        reason = str(row.get("reason") or "").strip()[:600]
        seen.add(key)
        valid.append({"role": role, "section": sec, "variant": vid, "fill": fill_id, "reason": reason})
    return valid, dropped


def default_plan(lib: PatternLibrary, facts: dict) -> dict:
    """全默认计划：每角色 × 每段 = default_by_section（无默认则跳过）。"""
    roles: dict = {}
    for role in lib.role_ids():
        secs = {}
        for s in (facts.get("sections") or []):
            vid = lib.default_variant(role, str(s.get("kind") or "full"))
            if vid:
                secs[str(int(s["index"]))] = {"variant": vid, "fill": None, "reason": "默认策略",
                                              "source": "default"}
        roles[role] = secs
    return {"roles": roles, "overall": "", "stats": {"source": "default", "llm_rows": 0,
                                                     "dropped": [], "errors": [], "attempts": 0}}


def decide(facts: dict, *, key: str | None = None, context_md: str = "", post=None,
           library: PatternLibrary | None = None) -> dict:
    """事实 → LLM 选 ID → 校验合并 → 计划（失败/缺席全量回默认）。

    post 注入：``post(payload_dict) -> str``（返回模型文本；测试用）。
    """
    lib = library or load_patterns(str(facts.get("pack") or ""))
    base = default_plan(lib, facts)
    key = key if key is not None else resolve_api_key()
    if not key and post is None:
        base["stats"]["errors"] = ["无 API key——跳过 LLM 通道（全量默认策略）"]
        return base

    payload = _llm_payload(facts, context_md)

    def _post(p: dict) -> str:
        if post is not None:
            return str(post(p))
        resp = chat_post_json({"model": LLM_MODEL,
                               "messages": [{"role": "system", "content": _SYSTEM_PROMPT},
                                            {"role": "user", "content": json.dumps(p, ensure_ascii=False)}],
                               "temperature": 0.3},
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
        if not isinstance(data, dict) or not isinstance(data.get("plan"), list):
            errors.append(f"第{attempt}次响应不合形状（需 {{plan:[...]}} 对象）")
            continue
        valid, dropped = validate_plan(data.get("plan") or [], lib, facts)
        if dropped and attempt == 1:
            whys = "；".join(str(d.get("why")) for d in dropped[:3])
            errors.append(f"第1次含 {len(dropped)} 条违规（{whys}）——整体拒绝，重试一次")
            continue
        if dropped:
            errors.append(f"第2次仍含 {len(dropped)} 条违规——已丢弃（见 dropped）")
        # 合并：默认打底 → LLM 行覆盖
        roles = {k: {kk: dict(vv) for kk, vv in v.items()} for k, v in base["roles"].items()}
        for r in valid:
            roles.setdefault(r["role"], {})[str(r["section"])] = {
                "variant": r["variant"], "fill": r["fill"],
                "reason": r["reason"] or "LLM 选择", "source": "llm"}
        n_llm = len(valid)
        n_pairs = sum(len(v) for v in roles.values())
        source = "llm" if n_llm >= n_pairs else ("mixed" if n_llm else "default")
        return {"roles": roles, "overall": str(data.get("overall") or "").strip()[:400],
                "stats": {"source": source, "llm_rows": n_llm, "dropped": dropped,
                          "errors": errors, "attempts": attempt}}
    base["stats"]["errors"] = errors
    base["stats"]["attempts"] = 2
    return base
