"""E4 段3：配器模式库装载/校验/查询 + 与历史鼓型逐格位对拍。"""

import pytest

from tsov.agent.tools_compose import _DRUM_PATTERNS
from tsov.arrange.library import (available_packs, check_event, load_patterns, parse_token,
                                  token_ok, validate_patterns)

PACKS = ["wotaiko-fast-6-8", "edm-electro-4-4", "pop-band-standard"]


# ---------------------------------------------------------------------------
# 真实三包：装载 + 结构不变量
# ---------------------------------------------------------------------------


def test_available_packs_includes_three():
    packs = available_packs()
    for p in PACKS:
        assert p in packs


@pytest.mark.parametrize("name", PACKS)
def test_pack_loads_and_defaults_valid(name):
    lib = load_patterns(name)
    assert lib.pack == name
    assert lib.grids_per_bar in (12, 16)
    assert lib.description and lib.sources
    for role in lib.role_ids():
        spec = lib.role(role)
        d = spec.get("default") or {}
        assert d, f"{role} 缺 default"
        assert "full" in d, f"{role}.default 缺 full 兜底"
        for kind, vid in d.items():
            assert vid in (spec.get("variants") or {}), f"{role}.default[{kind}]={vid} 引用无效"
        # 每个变体要么有网格事件、要么是旋律跟随（melody_offset）
        for vid, var in (spec.get("variants") or {}).items():
            if var.get("melody_offset") is None:
                assert var.get("events"), f"{role}.{vid} 既非旋律跟随又无事件"
            else:
                assert not var.get("events"), f"{role}.{vid} 旋律跟随不应带事件"


@pytest.mark.parametrize("name", PACKS)
def test_pattern_roles_match_pack_json(name):
    """patterns 角色集合 = 配器预设 instruments 角色集合（同源一致）。"""
    from tsov.presets import load_library

    lib = load_patterns(name)
    pack = load_library().get_arrangement(name)
    pack_roles = {str(i.get("role")) for i in pack.instruments}
    assert set(lib.role_ids()) == pack_roles


def test_wotaiko_drums_parity_with_legacy():
    """e_drums base/energy 与 tools_compose._DRUM_PATTERNS 逐格位一致（升格不漂移）。"""
    lib = load_patterns("wotaiko-fast-6-8")
    for vid in ("base", "energy"):
        legacy = _DRUM_PATTERNS["wotaiko_drums_" + vid]
        events = lib.variant("e_drums", vid)["events"]
        assert len(events) == len(legacy), f"{vid} 事件数不一致：{len(events)} vs {len(legacy)}"
        for k, (ev, lv) in enumerate(zip(events, legacy)):
            grid, ln, vel, tok = ev
            lg, ll, lp, lvel = lv
            assert (grid, ln, tok) == (lg, ll, str(lp)), f"{vid}[{k}] 格位/音高不一致"
            assert vel == pytest.approx(lvel), f"{vid}[{k}] 力度不一致"


def test_catalog_and_fills_shape():
    lib = load_patterns("edm-electro-4-4")
    cat = lib.catalog()
    assert "e_drums" in cat and "synth_bass" in cat
    ids = {v["id"] for v in cat["e_drums"]["variants"]}
    assert {"base", "drop", "build", "breakdown"} <= ids
    fills = lib.fill_catalog()
    assert "snare_roll" in fills
    assert fills["snare_roll"]["role"] == "e_drums"


def test_default_variant_fallback():
    lib = load_patterns("wotaiko-fast-6-8")
    # 未列段型 → full 兜底
    assert lib.default_variant("synth_bass", "unknown-kind") == lib.default_variant("synth_bass", "full")
    # 4/4 包覆盖 16 格
    lib2 = load_patterns("pop-band-standard")
    assert lib2.grids_per_bar == 16
    assert lib2.variant("bass", "drive")["events"][0][0] == 1


def test_fill_events_in_range():
    for name in PACKS:
        lib = load_patterns(name)
        for fid, f in lib.fills.items():
            for k, e in enumerate(f["events"]):
                bo, grid, ln, vel, tok = e
                assert bo == 0, f"{name}.{fid}[{k}] bar_offset 本库应 0（v1）"
                assert 1 <= grid <= lib.grids_per_bar
                assert grid - 1 + ln <= lib.grids_per_bar
                assert 0.0 < vel <= 1.0
                ok, msg = token_ok(tok)
                assert ok, msg


# ---------------------------------------------------------------------------
# 校验器：坏数据拦截
# ---------------------------------------------------------------------------


def _mini(**over):
    data = {
        "pack": "test-pack",
        "meter": "4/4",
        "grids_per_bar": 16,
        "energy_tiers": {"1": {"vel_scale": 0.85}, "2": {"vel_scale": 1.0}, "3": {"vel_scale": 1.12}},
        "strengths": {"soft": 0.9, "standard": 1.0, "full": 1.1},
        "roles": {
            "drums": {
                "title": "t", "gm_program": None, "register": None,
                "velocity_range": [70, 127], "pan": 0.0, "level_hint_db": 0,
                "default": {"full": "a"},
                "variants": {"a": {"desc": "d", "energy": 2, "melody_offset": None,
                                   "events": [[1, 2, 0.9, "36"]]}},
            }
        },
    }
    data.update(over)
    return data


def test_mini_valid_passes():
    assert validate_patterns(_mini()) == []


def test_bad_grid_out_of_range():
    d = _mini()
    d["roles"]["drums"]["variants"]["a"]["events"] = [[17, 2, 0.9, "36"]]
    assert any("grid 越界" in p for p in validate_patterns(d))


def test_bad_grid_len_crosses_bar():
    d = _mini()
    d["roles"]["drums"]["variants"]["a"]["events"] = [[15, 3, 0.9, "36"]]
    assert any("越小节" in p for p in validate_patterns(d))


def test_bad_vel_and_token():
    d = _mini()
    d["roles"]["drums"]["variants"]["a"]["events"] = [[1, 2, 1.5, "root-13"]]
    problems = validate_patterns(d)
    assert any("vel 非法" in p for p in problems)
    assert any("token 非法" in p for p in problems)


def test_bad_default_reference():
    d = _mini()
    d["roles"]["drums"]["default"] = {"full": "ghost"}
    assert any("不存在变体" in p for p in validate_patterns(d))


def test_melody_variant_with_events_rejected():
    d = _mini()
    d["roles"]["drums"]["variants"]["m"] = {"desc": "m", "energy": 2, "melody_offset": 0,
                                            "events": [[1, 2, 0.9, "36"]]}
    assert any("不应带 events" in p for p in validate_patterns(d))


def test_grids_per_bar_mismatch():
    d = _mini()
    d["meter"] = "6/8"  # 应为 12 格
    assert any("grids_per_bar 与拍号不一致" in p for p in validate_patterns(d))


def test_missing_tier_keys():
    d = _mini()
    d["energy_tiers"] = {"1": {"vel_scale": 1.0}}
    problems = validate_patterns(d)
    assert any("energy_tiers[2]" in p for p in problems)
    assert any("energy_tiers[3]" in p for p in problems)


def test_fill_bad_role():
    d = _mini()
    d["fills"] = {"f": {"role": "ghost", "desc": "d", "events": [[0, 1, 1, 0.8, "36"]]}}
    assert any("角色不存在" in p for p in validate_patterns(d))


# ---------------------------------------------------------------------------
# token / event 工具
# ---------------------------------------------------------------------------


def test_token_parse():
    assert parse_token("36") == {"kind": "abs", "pitch": 36}
    assert parse_token("chord+12") == {"kind": "chord", "arp_idx": None, "offset": 12}
    assert parse_token("root-12") == {"kind": "root", "arp_idx": None, "offset": -12}
    assert parse_token("arp:2") == {"kind": "arp", "arp_idx": 2, "offset": 0}
    assert parse_token("arp:0+12") == {"kind": "arp", "arp_idx": 0, "offset": 12}
    assert parse_token("octave") == {"kind": "octave", "arp_idx": None, "offset": 0}
    assert parse_token("rest") == {"kind": "rest"}
    with pytest.raises(ValueError):
        parse_token("wut")


def test_token_ok_edges():
    assert token_ok("36")[0]
    assert not token_ok("128")[0]
    assert not token_ok("rest+12")[0]
    assert not token_ok("arp:4")[0]
    assert token_ok("root5")[0]


def test_check_event():
    assert check_event([1, 2, 0.8, "36"], 12) == []
    problems = check_event([13, 1, 0.8, "36"], 12)
    assert any("grid 越界" in p for p in problems)
    problems = check_event([1, 2, 0.0, "36"], 12)
    assert any("vel 非法" in p for p in problems)
