"""P0 A5：MCP 写面（project 寻址 / rename_track / 外部改动重载 / ok=False → isError）。"""

from __future__ import annotations

import json
import time

from tsov.mcp.sessions import ProjectStore

from unit._cleanup import rmtree_force
from unit.test_mcp import _client, _mk_project, _run, _ws


def test_rename_track_uses_new_name():
    ws = _ws()
    try:
        _mk_project(ws, "demo-a", tracks=2)
        store = ProjectStore(ws, allow_write=True)

        async def case():
            async with _client(store) as s:
                r = await s.call_tool("rename_track", {"project": "demo-a", "track": 0, "new_name": "Lead"})
                assert r.is_error is False, r.content[0].text
                st = json.loads((await s.call_tool("project_status", {"project": "demo-a"})).content[0].text)
                assert st["tracks"][0]["name"] == "Lead"
                return True

        assert _run(case())
    finally:
        rmtree_force(ws)


def test_total_failure_returns_is_error():
    """无改动的写：apply_score ok=False/applied=0 → 工具回执 isError=True。"""
    ws = _ws()
    try:
        _mk_project(ws, "demo-a", tracks=1, tempo=123.0)
        store = ProjectStore(ws, allow_write=True)

        async def case():
            async with _client(store) as s:
                r = await s.call_tool("set_tempo", {"project": "demo-a", "tempo": 123.0})
                assert r.is_error is True
                assert "未写入" in r.content[0].text
                return True

        assert _run(case())
    finally:
        rmtree_force(ws)


def test_store_reloads_after_external_change():
    """MCP 缓存对象在 score.json 被外部改过后必须重载（防旧对象覆盖）。"""
    ws = _ws()
    try:
        root = _mk_project(ws, "demo-a", tracks=1, tempo=123.0)
        store = ProjectStore(ws, allow_write=True)
        p1 = store.get_project("demo-a")
        assert p1.score.title == "测试 demo-a"

        time.sleep(0.01)
        data = json.loads((root / "score.json").read_text(encoding="utf-8"))
        data["title"] = "外部改过的标题"
        (root / "score.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        p2 = store.get_project("demo-a")
        assert p2 is not p1
        assert p2.score.title == "外部改过的标题"

        async def case():
            async with _client(store) as s:
                r = await s.call_tool("set_tempo", {"project": "demo-a", "tempo": 140.0})
                assert r.is_error is False, r.content[0].text
                return True

        assert _run(case())
        after = json.loads((root / "score.json").read_text(encoding="utf-8"))
        assert after["title"] == "外部改过的标题"   # 写操作基于新内容，不覆盖标题
        assert after["tempo"] == 140.0
    finally:
        rmtree_force(ws)


def test_session_tools_still_accept_legacy_name():
    """旧客户端传 name 仍可用（迁移期兼容）。"""
    ws = _ws()
    try:
        _mk_project(ws, "demo-a", tracks=1)
        store = ProjectStore(ws, allow_write=True)

        async def case():
            async with _client(store) as s:
                r = await s.call_tool("project_status", {"name": "demo-a"})
                assert r.is_error is False
                assert json.loads(r.content[0].text)["name"] == "demo-a"
                return True

        assert _run(case())
    finally:
        rmtree_force(ws)
