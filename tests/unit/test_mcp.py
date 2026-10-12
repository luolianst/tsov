"""批F F1 单测：MCP 只读面（工具清单/调用/资源/护栏；内存传输）。

说明：本仓未装 pytest-asyncio —— async 用例统一 asyncio.run 直跑。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from tsov.core.notes import Note
from tsov.core.score import Instrument, Score, Track
from tsov.mcp.server import build_server
from tsov.mcp.sessions import ProjectStore

from unit._cleanup import rmtree_force


def _ws() -> Path:
    d = Path("output") / f"mcptest-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _mk_project(ws: Path, name: str, *, tracks: int = 2, tempo: float = 123.0) -> Path:
    """最小工程：<ws>/output/<name>/score.json。"""
    root = ws / "output" / name
    root.mkdir(parents=True, exist_ok=True)
    score = Score(
        title=f"测试 {name}",
        tempo=tempo,
        tracks=[
            Track(
                name=f"t{i}",
                instrument=Instrument(program="piano", volume=1.0, effects=[]),
                notes=[Note(start=0.0, end=0.5, pitch_midi=60 + i, pitch_hz=440.0, velocity=0.8)],
            )
            for i in range(tracks)
        ],
    )
    (root / "score.json").write_text(json.dumps(score.to_dict(), ensure_ascii=False), encoding="utf-8")
    return root


@asynccontextmanager
async def _client(store: ProjectStore):
    """内存传输起 server ↔ 已 initialize 的 ClientSession。"""
    from mcp import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    server = build_server(store)
    async with create_client_server_memory_streams() as (cstreams, sstreams):
        task = asyncio.create_task(
            server.run(sstreams[0], sstreams[1], server.create_initialization_options())
        )
        try:
            async with ClientSession(cstreams[0], cstreams[1]) as session:
                await session.initialize()
                yield session
        finally:
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 收尾吞掉取消/退出噪声
                pass


def _run(coro):
    return asyncio.run(coro)


def test_mcp_tool_surface_readonly():
    """只读面：会话层 + 只读 4；写/重/play_score/use_skill 不注册。"""
    ws = _ws()
    try:
        store = ProjectStore(ws, allow_write=False)

        async def case():
            async with _client(store) as s:
                r = await s.list_tools()
                return {t.name for t in r.tools}

        names = _run(case())
        assert {"list_projects", "open_project", "project_status"} <= names
        assert {"load_score", "list_dir", "read_text", "detect_key"} <= names
        assert not ({"edit_score", "write_notes", "remove_track", "render_wav", "export_audio"}
                    & names)
        assert "play_score" not in names and "use_skill" not in names
    finally:
        rmtree_force(ws)


def test_mcp_tool_surface_allow_write():
    """--allow-write：写 10 + 重 7 补齐；play_score 仍不暴露。"""
    ws = _ws()
    try:
        store = ProjectStore(ws, allow_write=True)

        async def case():
            async with _client(store) as s:
                r = await s.list_tools()
                return {t.name for t in r.tools}

        names = _run(case())
        assert {"edit_score", "set_tempo", "create_track", "write_notes", "duplicate_bars",
                "set_track_mix", "apply_effect", "apply_pattern", "remove_track", "rename_track"} <= names
        assert {"render_wav", "export_audio", "export_midi", "analyze_levels",
                "voice_to_score", "transcribe", "understand"} <= names
        assert "play_score" not in names
    finally:
        rmtree_force(ws)


def test_mcp_session_tools_and_status():
    """会话层：list/open/status 走真实工程；护栏（越界/不存在）→ isError。"""
    ws = _ws()
    try:
        _mk_project(ws, "demo-a", tracks=2, tempo=123.0)
        _mk_project(ws, "demo-b", tracks=3, tempo=140.0)
        store = ProjectStore(ws)

        async def case():
            async with _client(store) as s:
                r1 = await s.call_tool("list_projects", {})
                lst = json.loads(r1.content[0].text)
                names = {x["name"] for x in lst}
                assert {"demo-a", "demo-b"} <= names
                a = next(x for x in lst if x["name"] == "demo-a")
                assert a["tracks"] == 2 and a["notes"] == 2

                r2 = await s.call_tool("project_status", {"name": "demo-a"})
                st = json.loads(r2.content[0].text)
                assert st["tempo"] == 123.0 and st["notes_total"] == 2 and len(st["tracks"]) == 2
                assert st["tracks"][0]["name"] == "t0"

                r3 = await s.call_tool("open_project", {"name": "demo-b"})
                assert not r3.is_error and json.loads(r3.content[0].text)["tempo"] == 140.0

                r4 = await s.call_tool("project_status", {"name": "../evil"})
                assert r4.is_error and ("非法" in r4.content[0].text or "越界" in r4.content[0].text)

                r5 = await s.call_tool("project_status", {"name": "not-exist"})
                assert r5.is_error and "不存在" in r5.content[0].text

                r6 = await s.call_tool("edit_score", {"score_path": "x"})
                assert r6.is_error and "不可用" in r6.content[0].text

                return True

        assert _run(case())
    finally:
        rmtree_force(ws)


def test_mcp_resources():
    """资源：projects（静态）+ summary/score（模板）；读回可解析；未知 URI 报错。"""
    ws = _ws()
    try:
        root = _mk_project(ws, "demo-a", tracks=2)
        store = ProjectStore(ws)

        async def case():
            async with _client(store) as s:
                rs = await s.list_resources()
                assert any(str(r.uri) == "tsov://projects" for r in rs.resources)

                rt = await s.list_resource_templates()
                tmpl = {t.uri_template for t in rt.resource_templates}
                assert "tsov://projects/{name}/summary" in tmpl
                assert "tsov://projects/{name}/score" in tmpl

                r1 = await s.read_resource("tsov://projects")
                lst = json.loads(r1.contents[0].text)
                assert any(x["name"] == "demo-a" for x in lst)

                r2 = await s.read_resource("tsov://projects/demo-a/summary")
                assert json.loads(r2.contents[0].text)["tempo"] == 123.0

                r3 = await s.read_resource("tsov://projects/demo-a/score")
                assert len(json.loads(r3.contents[0].text)["tracks"]) == 2

                err = None
                try:
                    await s.read_resource("tsov://nope")
                except Exception as e:  # noqa: BLE001 服务端错误 → 客户端异常
                    err = e
                assert err is not None and "未知资源" in str(err)

                return True

        assert _run(case())
    finally:
        rmtree_force(ws)


def test_mcp_registry_read_tool():
    """注册表只读工具：load_score 经 MCP 返回同源摘要文本。"""
    ws = _ws()
    try:
        root = _mk_project(ws, "demo-a", tracks=2)
        store = ProjectStore(ws)

        async def case():
            async with _client(store) as s:
                r = await s.call_tool("load_score", {"score_path": str(root / "score.json")})
                assert not r.is_error, r.content[0].text
                text = r.content[0].text
                assert "tracks=2" in text and "测试 demo-a" in text

                r2 = await s.call_tool("load_score", {"score_path": str(root / "nope.json")})
                assert r2.is_error and "FileNotFoundError" in r2.content[0].text

                return True

        assert _run(case())
    finally:
        rmtree_force(ws)
def test_mcp_surface_f2_session_tools():
    """F2：list_history 常开；create_project/undo/redo/rollback 随 --allow-write 出现。"""
    ws = _ws()
    try:
        async def names(allow):
            store = ProjectStore(ws, allow_write=allow)
            async with _client(store) as s:
                r = await s.list_tools()
                return {t.name for t in r.tools}

        ro = _run(names(False))
        assert "list_history" in ro
        assert not ({"create_project", "undo", "redo", "rollback"} & ro)
        rw = _run(names(True))
        assert {"create_project", "undo", "redo", "rollback", "list_history"} <= rw
    finally:
        rmtree_force(ws)


def test_mcp_write_flow_undo_redo(monkeypatch):
    """写面全链：write_notes（工程名语义）→ 落盘 → undo → redo（快照窗口）。"""
    from tsov.mcp import sessions as S

    monkeypatch.setattr(S, "_web_alive", lambda: False)   # 确定性：忽略本机 8790 状态
    ws = _ws()
    try:
        _mk_project(ws, "demo-a", tracks=2)   # 初始 2 音（每轨 1）
        store = ProjectStore(ws, allow_write=True)

        async def case():
            async with _client(store) as s:
                r = await s.call_tool(
                    "write_notes",
                    {
                        "name": "demo-a", "track": 0, "mode": "append",
                        "notes": [
                            {"bar": 1, "grid": 1, "len": 2, "pitch_midi": 64},
                            {"bar": 1, "grid": 3, "len": 2, "note": "E4"},
                        ],
                    },
                )
                assert not r.is_error, r.content[0].text
                payload = json.loads(r.content[0].text)
                assert payload["ok"] and payload["project"] == "demo-a"

                st = json.loads((await s.call_tool("project_status", {"name": "demo-a"})).content[0].text)
                assert st["notes_total"] == 4

                u = json.loads((await s.call_tool("undo", {"name": "demo-a"})).content[0].text)
                assert u["ok"]
                st2 = json.loads((await s.call_tool("project_status", {"name": "demo-a"})).content[0].text)
                assert st2["notes_total"] == 2

                rd = json.loads((await s.call_tool("redo", {"name": "demo-a"})).content[0].text)
                assert rd["ok"]
                st3 = json.loads((await s.call_tool("project_status", {"name": "demo-a"})).content[0].text)
                assert st3["notes_total"] == 4
                return True

        assert _run(case())
    finally:
        rmtree_force(ws)


def test_mcp_write_schema_name_addressing(monkeypatch):
    """写工具 schema：score_path/output 剥掉、name 必填；只读工具 schema 不动。"""
    ws = _ws()
    try:
        store = ProjectStore(ws, allow_write=True)

        async def case():
            async with _client(store) as s:
                r = await s.list_tools()
                by = {t.name: t for t in r.tools}
                w = by["write_notes"].input_schema
                assert "score_path" not in w["properties"] and "name" in w["properties"]
                assert w["required"] == ["name", "notes"]
                # 只读面原样（load_score 保持 score_path 必填）
                lw = by["load_score"].input_schema
                assert "score_path" in lw["properties"]
                return True

        assert _run(case())
    finally:
        rmtree_force(ws)


def test_mcp_write_lock_alive_and_stale(monkeypatch):
    """写锁：活锁拒绝（报 pid）；陈旧锁回收；写毕释放。"""
    from tsov.mcp import sessions as S

    monkeypatch.setattr(S, "_web_alive", lambda: False)
    import subprocess
    import sys

    ws = _ws()
    holder = None
    try:
        _mk_project(ws, "demo-a", tracks=1)
        store = ProjectStore(ws, allow_write=True)
        lockf = ws / "output" / "demo-a" / ".tsov-lock"
        args = {"name": "demo-a", "notes": [{"bar": 1, "grid": 1, "len": 1, "pitch_midi": 60}]}

        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        lockf.write_text(json.dumps({"pid": holder.pid, "ts": 0, "source": "test"}), encoding="utf-8")

        async def case_locked():
            async with _client(store) as s:
                return await s.call_tool("write_notes", args)

        r1 = _run(case_locked())
        assert r1.is_error and "被占用" in r1.content[0].text

        lockf.write_text(json.dumps({"pid": 999999, "ts": 0, "source": "ghost"}), encoding="utf-8")

        async def case_stale():
            async with _client(store) as s:
                return await s.call_tool("write_notes", args)

        r2 = _run(case_stale())
        assert not r2.is_error, r2.content[0].text
        assert not lockf.exists()   # 写毕释放
    finally:
        if holder:
            holder.terminate()
        rmtree_force(ws)


def test_mcp_web_guard(monkeypatch):
    """Web 共存守卫：warn 附提示不拒；strict 直接拒绝；off 关。"""
    from tsov.mcp import sessions as S

    monkeypatch.setattr(S, "_web_alive", lambda: True)
    ws = _ws()
    try:
        _mk_project(ws, "demo-a", tracks=1)
        args = {"name": "demo-a", "notes": [{"bar": 1, "grid": 1, "len": 1, "pitch_midi": 60}]}

        async def call(store):
            async with _client(store) as s:
                return await s.call_tool("write_notes", args)

        rw = _run(call(ProjectStore(ws, allow_write=True, web_guard="warn")))
        assert not rw.is_error and "⚠" in rw.content[0].text

        rs = _run(call(ProjectStore(ws, allow_write=True, web_guard="strict")))
        assert rs.is_error and "strict" in rs.content[0].text

        monkeypatch.setattr(S, "_web_alive", lambda: False)
        ro = _run(call(ProjectStore(ws, allow_write=True, web_guard="warn")))
        assert not ro.is_error and "⚠" not in ro.content[0].text
    finally:
        rmtree_force(ws)


def test_mcp_create_project_rollback_history(monkeypatch):
    """建工程（git 基线）→ 写一笔（未提交）→ rollback HEAD 丢弃 → 历史可读。"""
    from tsov.mcp import sessions as S

    monkeypatch.setattr(S, "_web_alive", lambda: False)
    ws = _ws()
    try:
        _mk_project(ws, "src-p", tracks=2)
        store = ProjectStore(ws, allow_write=True)

        async def case():
            async with _client(store) as s:
                r = await s.call_tool(
                    "create_project", {"name": "mcp-roll", "source": "output/src-p/score.json"}
                )
                assert not r.is_error, r.content[0].text
                assert json.loads(r.content[0].text)["ok"]

                h = json.loads((await s.call_tool("list_history", {"name": "mcp-roll"})).content[0].text)
                assert len(h["history"]) >= 1   # 基线 commit

                r2 = await s.call_tool(
                    "write_notes",
                    {"name": "mcp-roll", "track": 0, "mode": "append",
                     "notes": [{"bar": 1, "grid": 1, "len": 2, "pitch_midi": 67}]},
                )
                assert not r2.is_error, r2.content[0].text
                st = json.loads((await s.call_tool("project_status", {"name": "mcp-roll"})).content[0].text)
                assert st["notes_total"] == 3

                rb = json.loads((await s.call_tool("rollback", {"name": "mcp-roll", "rev": "HEAD"})).content[0].text)
                assert rb["ok"] is True
                st2 = json.loads((await s.call_tool("project_status", {"name": "mcp-roll"})).content[0].text)
                assert st2["notes_total"] == 2

                dbl = await s.call_tool("create_project", {"name": "mcp-roll", "source": "output/src-p/score.json"})
                assert dbl.is_error and "已存在" in dbl.content[0].text
                return True

        assert _run(case())
    finally:
        rmtree_force(ws)
