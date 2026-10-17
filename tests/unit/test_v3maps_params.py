"""P0 A4：v3-maps 重名参数寻址（名称#下标）+ 表结构校验。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from tsov.host.plugin_maps import load_map, resolve_param_name, validate_map
from tsov.host.vst_worker import apply_params

from unit._cleanup import rmtree_force

PLUGIN = "Fake.vst3"


@pytest.fixture()
def maps():
    d = Path("output") / f"v3map-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    doc = {
        "plugin": "Fake", "param_count": 4,
        "params": [
            {"index": 0, "name": "message", "default": 0.0, "discrete": False, "labels": None},
            {"index": 1, "name": "message", "default": 0.0, "discrete": False, "labels": None},
            {"index": 2, "name": "Cutoff", "default": 0.0, "discrete": False, "labels": None},
            {"index": 3, "name": "Wave", "default": 0.0, "discrete": True, "labels": ["a", "b"]},
        ],
    }
    (d / "fake.json").write_text(json.dumps(doc), encoding="utf-8")
    try:
        yield d
    finally:
        rmtree_force(d)


def test_bare_duplicate_name_is_rejected(maps):
    key, err = resolve_param_name(PLUGIN, "message", out_dir=maps)
    assert key is None
    assert "重名" in err and "message#<下标>" in err


def test_duplicate_name_with_index_resolves(maps):
    assert resolve_param_name(PLUGIN, "message#1", out_dir=maps) == ("message#1", None)
    key, err = resolve_param_name(PLUGIN, "message#9", out_dir=maps)
    assert key is None and "不存在" in err


def test_numeric_index_resolves_to_disambiguated_key(maps):
    assert resolve_param_name(PLUGIN, "0", out_dir=maps) == ("message#0", None)
    assert resolve_param_name(PLUGIN, "2", out_dir=maps) == ("Cutoff", None)


def test_unique_name_and_alias(maps):
    assert resolve_param_name(PLUGIN, "Cutoff", out_dir=maps) == ("Cutoff", None)


def test_validate_map_rejects_bad_schema(maps):
    bad = {"param_count": 2, "params": [{"index": 1, "name": ""}]}
    errs = validate_map(bad)
    assert any("param_count" in e for e in errs)
    assert any("index" in e for e in errs)
    assert any("name" in e for e in errs)


def test_load_map_refuses_corrupt_table(maps):
    (maps / "bad.json").write_text("{not json", encoding="utf-8")
    assert load_map("bad.vst3", out_dir=maps) is None
    (maps / "bad.json").write_text(json.dumps({"param_count": 2, "params": []}), encoding="utf-8")
    assert load_map("bad.vst3", out_dir=maps) is None   # 结构非法 → 当无表


class _Proc:
    def __init__(self):
        self.desc = [
            {"index": 0, "name": "message"},
            {"index": 1, "name": "message"},
            {"index": 2, "name": "Cutoff"},
            {"index": 3, "name": "Wave"},
        ]
        self.sets: list[tuple[int, float]] = []

    def get_parameters_description(self):
        return self.desc

    def set_parameter(self, idx, value):
        self.sets.append((idx, value))


def test_apply_params_targets_duplicate_by_compound_key():
    p = _Proc()
    apply_params(p, {"message#1": 0.7}, strict=True)
    assert p.sets == [(1, 0.7)]


def test_apply_params_numeric_out_of_collapsed_names():
    """names 因重名塌缩后 len 变小，纯下标仍应可用。"""
    p = _Proc()
    apply_params(p, {"3": 0.25}, strict=True)
    assert p.sets == [(3, 0.25)]


def test_apply_params_bare_duplicate_name_rejected_in_strict():
    p = _Proc()
    with pytest.raises(ValueError, match="参数名重名"):
        apply_params(p, {"message": 0.5}, strict=True)
    assert p.sets == []


def test_apply_params_compound_name_mismatch_rejected():
    p = _Proc()
    with pytest.raises(ValueError, match="不匹配"):
        apply_params(p, {"Wave#1": 0.5}, strict=True)
