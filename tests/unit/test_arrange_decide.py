"""E4 段3：配器决策（tsov/arrange/decide.py）单测——LLM 只选 ID + 校验 + 降级。"""

import json

from tsov.arrange.decide import decide, default_plan
from tsov.arrange.library import load_patterns

PACK = "wotaiko-fast-6-8"


def _facts():
    return {
        "project": "t", "pack": PACK, "strength": "standard", "key": "D major",
        "meter": {"time_signature": "6/8", "bars_total": 4},
        "melody": {"track": 0, "name": "melody", "notes": 12, "register": [60, 72], "mean_midi": 66},
        "sections": [
            {"index": 0, "label": "前奏", "kind": "intro", "bars": 2, "energy": 0.5,
             "melody_notes": 4, "synthetic": False, "start": 0.0, "end": 3.0,
             "start_bar": 1, "end_bar": 2},
            {"index": 1, "label": "副歌", "kind": "chorus", "bars": 2, "energy": 1.0,
             "melody_notes": 8, "synthetic": False, "start": 3.0, "end": 6.0,
             "start_bar": 3, "end_bar": 4},
        ],
        "roles": {}, "fills": {},
    }


def _lib():
    return load_patterns(PACK)


# ---------------------------------------------------------------------------
# 默认策略 / 无 key 降级
# ---------------------------------------------------------------------------


def test_no_key_returns_default_plan():
    lib = _lib()
    plan = decide(_facts(), key="", library=lib)
    assert plan["stats"]["source"] == "default"
    assert plan["stats"]["errors"]
    roles = plan["roles"]
    assert set(roles) == set(lib.role_ids())
    # 每角色 × 每段都有默认（三包均带 full 兜底）
    for role in lib.role_ids():
        for s in ("0", "1"):
            assert s in roles[role], f"{role} 缺段 {s}"
            assert roles[role][s]["source"] == "default"
    # 抽查：e_drums 前奏=sparse（intro 默认）、副歌=energy（chorus 默认）
    assert roles["e_drums"]["0"]["variant"] == "sparse"
    assert roles["e_drums"]["1"]["variant"] == "energy"


def test_default_variant_matches_library():
    lib = _lib()
    plan = default_plan(lib, _facts())
    assert plan["roles"]["synth_bass"]["1"]["variant"] == lib.default_variant("synth_bass", "chorus")


# ---------------------------------------------------------------------------
# LLM 通道
# ---------------------------------------------------------------------------


def test_llm_rows_applied_mixed():
    lib = _lib()

    def post(payload: dict) -> str:
        assert payload["pack"] == PACK and "roles" in payload and "sections" in payload
        return json.dumps({"plan": [
            {"role": "e_drums", "section": 0, "variant": "sparse", "reason": "前奏收"},
            {"role": "e_drums", "section": 1, "variant": "energy", "fill": "toms_down", "reason": "副歌推满"},
        ], "overall": "先收后放"}, ensure_ascii=False)

    plan = decide(_facts(), post=post, library=lib)
    assert plan["stats"]["source"] == "mixed"
    assert plan["stats"]["llm_rows"] == 2
    assert plan["overall"] == "先收后放"
    ed = plan["roles"]["e_drums"]
    assert ed["0"]["variant"] == "sparse" and ed["0"]["source"] == "llm"
    assert ed["1"]["fill"] == "toms_down"
    # 未覆盖角色 → 默认
    assert plan["roles"]["piano"]["0"]["source"] == "default"


def test_llm_full_coverage():
    lib = _lib()
    varmap = {r: sorted((lib.role(r).get("variants") or {}).keys()) for r in lib.role_ids()}

    def post(_p):
        rows = [{"role": r, "section": s, "variant": varmap[r][0], "reason": "x"}
                for r in varmap for s in (0, 1)]
        return json.dumps({"plan": rows})

    plan = decide(_facts(), post=post, library=lib)
    assert plan["stats"]["source"] == "llm"
    assert plan["stats"]["llm_rows"] == sum(len(v) for v in plan["roles"].values())


# ---------------------------------------------------------------------------
# 校验：丢弃 + 重试
# ---------------------------------------------------------------------------


def test_bad_variant_dropped_then_retry_succeeds():
    lib = _lib()
    calls = {"n": 0}

    def post(_p):
        calls["n"] += 1
        if calls["n"] == 1:
            return json.dumps({"plan": [{"role": "e_drums", "section": 0, "variant": "ghost", "reason": "x"}]})
        return json.dumps({"plan": [{"role": "e_drums", "section": 0, "variant": "sparse", "reason": "ok"}]})

    plan = decide(_facts(), post=post, library=lib)
    assert calls["n"] == 2
    assert plan["stats"]["attempts"] == 2
    assert plan["roles"]["e_drums"]["0"]["variant"] == "sparse"


def test_shape_error_retries_once():
    lib = _lib()
    calls = {"n": 0}

    def post(_p):
        calls["n"] += 1
        return "这不是 JSON" if calls["n"] == 1 else json.dumps({"plan": []})

    plan = decide(_facts(), post=post, library=lib)
    assert calls["n"] == 2
    assert plan["stats"]["source"] == "default"  # 空 plan → 全默认
    assert plan["stats"]["attempts"] == 2


def test_both_attempts_fail_keeps_defaults():
    lib = _lib()
    plan = decide(_facts(), post=lambda _p: "nope", library=lib)
    assert plan["stats"]["source"] == "default"
    assert len(plan["stats"]["errors"]) == 2
    assert plan["roles"]["e_drums"]["0"]["variant"] == "sparse"  # 默认仍在


def test_fill_role_mismatch_row_dropped():
    lib = _lib()

    def post(_p):
        return json.dumps({"plan": [
            {"role": "synth_bass", "section": 1, "variant": "drive", "fill": "toms_down", "reason": "x"},
        ]})

    plan = decide(_facts(), post=post, library=lib)
    assert plan["stats"]["llm_rows"] == 0
    assert any("不能配给" in d["why"] for d in plan["stats"]["dropped"])
    # 整行作废 → 该对回默认
    assert plan["roles"]["synth_bass"]["1"]["source"] == "default"


def test_duplicate_pairs_dropped():
    lib = _lib()

    def post(_p):
        return json.dumps({"plan": [
            {"role": "e_drums", "section": 0, "variant": "sparse", "reason": "a"},
            {"role": "e_drums", "section": 0, "variant": "base", "reason": "b"},
        ]})

    plan = decide(_facts(), post=post, library=lib)
    assert plan["stats"]["llm_rows"] == 1
    assert plan["roles"]["e_drums"]["0"]["variant"] == "sparse"
    assert any("重复" in d["why"] for d in plan["stats"]["dropped"])


def test_unknown_section_dropped():
    lib = _lib()

    def post(_p):
        return json.dumps({"plan": [{"role": "e_drums", "section": 9, "variant": "base", "reason": "x"}]})

    plan = decide(_facts(), post=post, library=lib)
    assert plan["stats"]["llm_rows"] == 0
    assert any("段不存在" in d["why"] for d in plan["stats"]["dropped"])
