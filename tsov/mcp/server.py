"""MCP 服务组装（批F F1+F2）：lowlevel Server + 回调式 handlers。

设计要点（契约 §2 / §5）：
- 只读工具 schema **原样**来自注册表；**写工具工程化**：参数 score_path/output → `name`（工程名），
  经命令层落定（`proj.apply_score`；ADR-0019 快照窗口：可 undo/redo，收藏点走 git）并回执 diff；
  重操作工具保持文件路径语义（护栏：路径须在仓库内）。
- 分级：read 常开；write/heavy 需 `--allow-write`（未开 = 不注册）；play_score/use_skill 不暴露。
- 写互斥：`<工程>/.tsov-lock`（活锁拒绝、陈旧回收）；写前 Web 共存守卫（warn/strict/off）。
- 错误 → CallToolResult(isError=True)（可读文本；不裸崩、不吞类型）。
"""

from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

import mcp_types as types
from mcp.server.lowlevel import Server

from .resources import ResourceHub
from .sessions import McpError, ProjectStore

_NAME_PROP = {"type": "string", "description": "工程名（output/ 下的目录名）"}

# 会话层（MCP 自有工具）：(名, 级[ R 常开 / B 需 --allow-write], 描述, schema)
SESSION_TOOL_DEFS = [
    (
        "list_projects", "R",
        "列出全部 tsov 工程（name/title/tracks/notes/mtime，最近修改在前）。",
        {"type": "object", "properties": {}},
    ),
    (
        "open_project", "R",
        "打开工程（校验存在与防穿越），返回与 project_status 相同的摘要。",
        {"type": "object", "properties": {"name": _NAME_PROP}, "required": ["name"]},
    ),
    (
        "project_status", "R",
        "工程摘要：速度/调号/轨道清单（名/音源/音符数）/音符总数/文件夹。",
        {"type": "object", "properties": {"name": _NAME_PROP}, "required": ["name"]},
    ),
    (
        "list_history", "R",
        "工程 git 历史（commit 摘要行，最近在前）。",
        {
            "type": "object",
            "properties": {"name": _NAME_PROP, "limit": {"type": "integer", "description": "条数（默认 20）"}},
            "required": ["name"],
        },
    ),
    (
        "create_project", "B",
        "从仓库内 score JSON 新建工程（output/<name>/；git init + 基线；与 Web 壳导入口径同源）。",
        {
            "type": "object",
            "properties": {
                "name": _NAME_PROP,
                "source": {"type": "string", "description": "源 score JSON 路径（相对 output/ 或仓库内路径）"},
                "title": {"type": "string", "description": "可选：改写谱面标题"},
            },
            "required": ["name", "source"],
        },
    ),
    (
        "undo", "B",
        "撤销一次编辑（journal 快照窗口游标；零 commit 语义 ADR-0019）。",
        {"type": "object", "properties": {"name": _NAME_PROP}, "required": ["name"]},
    ),
    (
        "redo", "B",
        "重做下一次快照（游标前移）。",
        {"type": "object", "properties": {"name": _NAME_PROP}, "required": ["name"]},
    ),
    (
        "rollback", "B",
        "git 回滚到指定版本（默认 HEAD~1；收藏点 = fav/<时间> 标签名）。",
        {
            "type": "object",
            "properties": {"name": _NAME_PROP, "rev": {"type": "string", "description": "目标 rev（默认 HEAD~1）"}},
            "required": ["name"],
        },
    ),
]

# 注册表工具分级（契约 §2.2 / 附录 §8）
READ_REGISTRY_TOOLS = ("load_score", "list_dir", "read_text", "detect_key")
WRITE_REGISTRY_TOOLS = (
    "edit_score", "set_tempo", "create_track", "write_notes", "duplicate_bars",
    "set_track_mix", "apply_effect", "apply_pattern", "remove_track", "rename_track",
)
HEAVY_REGISTRY_TOOLS = (
    "render_wav", "export_audio", "export_midi", "analyze_levels",
    "voice_to_score", "transcribe", "understand",
)
_LEVEL_TAG = {}
_LEVEL_TAG.update({n: "[只读]" for n in READ_REGISTRY_TOOLS})
_LEVEL_TAG.update({n: "[写]" for n in WRITE_REGISTRY_TOOLS})
_LEVEL_TAG.update({n: "[重操作]" for n in HEAVY_REGISTRY_TOOLS})

# 写工具工程化：剥掉文件路径参数（score_path/output/path），换工程名 name
_STRIP_PROPS = ("score_path", "output", "path")


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _tsov_version() -> str:
    try:
        from .. import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001
        return "0"


def _write_schema(spec) -> dict:
    """写工具 MCP schema：注册表 schema 去掉文件路径参数、加 name（其余原样）。"""
    params = copy.deepcopy(spec.parameters or {})
    props = params.setdefault("properties", {})
    for k in _STRIP_PROPS:
        props.pop(k, None)
    req = [r for r in (params.get("required") or []) if r not in _STRIP_PROPS]
    props["name"] = _NAME_PROP
    params["required"] = ["name", *req]
    return params


def build_server(store: ProjectStore) -> Server:
    """组装 MCP server（按 store 的开关决定注册面）。"""
    from ..agent.tools import build_default_registry
    from ..core.score import Score

    registry = build_default_registry()
    enabled: list[str] = list(READ_REGISTRY_TOOLS)
    if store.allow_write:
        enabled += list(WRITE_REGISTRY_TOOLS) + list(HEAVY_REGISTRY_TOOLS)
    resources = ResourceHub(store)
    session_defs = [d for d in SESSION_TOOL_DEFS if d[1] == "R" or store.allow_write]

    # ---- tools ----

    async def on_list_tools(ctx, params):  # noqa: ARG001
        tools = [
            types.Tool(name=nm, description=desc, input_schema=schema)
            for nm, _lv, desc, schema in session_defs
        ]
        for nm in enabled:
            spec = registry.get(nm)
            schema = (
                _write_schema(spec)
                if nm in WRITE_REGISTRY_TOOLS
                else (spec.parameters or {"type": "object", "properties": {}})
            )
            tools.append(
                types.Tool(
                    name=nm,
                    description=f"{_LEVEL_TAG.get(nm, '')} {spec.description}".strip(),
                    input_schema=schema,
                )
            )
        return types.ListToolsResult(tools=tools)

    # ---- 会话工具 ----

    def _call_session_tool(name: str, args: dict) -> str:
        if name == "list_projects":
            return _dump(store.list_projects())
        nm = str((args or {}).get("name") or "")

        if name in ("open_project", "project_status"):
            store.get_project(nm)  # 打开即校验
            return _dump(store.project_status(nm))

        if name == "list_history":
            proj = store.get_project(nm)
            limit = int((args or {}).get("limit") or 20)
            return _dump({"project": proj.name, "history": proj.log(limit)})

        if name == "create_project":
            return _dump(
                store.create_project(
                    nm,
                    str((args or {}).get("source") or ""),
                    title=(args or {}).get("title"),
                )
            )

        if name in ("undo", "redo", "rollback"):
            proj = store.get_project(nm)
            note = store.web_guard_note()
            with store.write_lock(nm):
                if name == "undo":
                    ok = bool(proj.undo())
                elif name == "redo":
                    ok = bool(proj.redo())
                else:
                    ok = bool(proj.rollback(str((args or {}).get("rev") or "HEAD~1")))
                state = {"ok": ok, "can_undo": proj.can_undo, "can_redo": proj.can_redo}
            text = _dump({"project": nm, "action": name, **state})
            if note:
                text += "\n\n" + note
            return text

        raise McpError(f"未知会话工具：{name}")

    # ---- 写工具（工程名语义 → 命令层落定） ----

    def _call_write(name: str, args: dict) -> str:
        args = dict(args)
        proj_name = str(args.pop("name", "") or "").strip()
        if not proj_name:
            raise McpError(f"{name} 需要 name（工程名）")
        proj = store.get_project(proj_name)
        spec = registry.get(name)
        note = store.web_guard_note()
        with store.write_lock(proj_name):
            with tempfile.TemporaryDirectory(prefix="tsov-mcp-") as td:
                work_in = Path(td) / "in.json"
                work_out = Path(td) / "out.json"
                work_in.write_text(
                    json.dumps(proj.score.to_dict(), ensure_ascii=False), encoding="utf-8"
                )
                hargs = {**args, "score_path": str(work_in), "output": str(work_out)}
                hargs.pop("path", None)
                spec.handler(hargs)
                outp = work_out if work_out.is_file() else work_in
                if not outp.is_file():
                    raise McpError(f"{name} 未产出谱文件（handler 契约异常）")
                new_score = Score.from_dict(json.loads(outp.read_text(encoding="utf-8")))
            res = proj.apply_score(
                new_score, commit_message=f"MCP：{name}（{proj_name}）", source="agent"
            )
        payload = {
            "project": proj_name,
            "tool": name,
            "ok": res.get("ok"),
            "applied": res.get("applied"),
            "errors": res.get("errors"),
            "commit": res.get("commit"),
            "seq": res.get("seq"),
            "diff": res.get("diff"),
        }
        text = _dump(payload)
        if note:
            text += "\n\n" + note
        return text

    # ---- 重操作工具（文件路径语义 + 仓库护栏） ----

    def _call_heavy(name: str, args: dict) -> str:
        repo = store.repo_root
        for key in ("score_path", "output", "audio", "path", "out", "wav"):
            v = (args or {}).get(key)
            if not v:
                continue
            p = Path(str(v))
            full = (p if p.is_absolute() else (repo / p)).resolve()
            if full != repo and repo not in full.parents:
                raise McpError(f"重操作路径越出仓库范围（{key}）：{v!r}")
        note = store.web_guard_note()
        out = registry.get(name).handler(dict(args or {}))
        text = out if isinstance(out, str) else _dump(out)
        if note:
            text += "\n\n" + note
        return text

    # ---- 总入口 ----

    session_names = {d[0] for d in session_defs}

    async def on_call_tool(ctx, params):  # noqa: ARG001
        name = str(params.name)
        args = dict(params.arguments or {})
        try:
            if name in session_names:
                text = _call_session_tool(name, args)
            elif name in WRITE_REGISTRY_TOOLS and name in enabled:
                text = _call_write(name, args)
            elif name in HEAVY_REGISTRY_TOOLS and name in enabled:
                text = _call_heavy(name, args)
            elif name in READ_REGISTRY_TOOLS:
                out = registry.get(name).handler(args)
                text = out if isinstance(out, str) else _dump(out)
            else:
                raise McpError(f"工具不可用：{name}（未注册——写/重操作需 --allow-write；见契约 §2）")
        except McpError as e:
            return types.CallToolResult(content=[types.TextContent(type="text", text=str(e))], is_error=True)
        except Exception as e:  # noqa: BLE001 工具异常 → isError（保类型与信息）
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"{type(e).__name__}: {e}")], is_error=True
            )
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=False)

    # ---- resources ----

    async def on_list_resources(ctx, params):  # noqa: ARG001
        return types.ListResourcesResult(resources=resources.list_static())

    async def on_list_resource_templates(ctx, params):  # noqa: ARG001
        return types.ListResourceTemplatesResult(resource_templates=resources.list_templates())

    async def on_read_resource(ctx, params):  # noqa: ARG001
        return resources.read(str(params.uri))

    # ---- 组装 ----

    return Server(
        "tsov",
        version=_tsov_version(),
        instructions=(
            "tsov（the shape of voice）MCP 服务：读写本机 tsov 工程的工具与资源。"
            "工程 = 仓库 output/ 下含 score.json 的目录。接口为 Experimental（契约："
            "docs/MCP接口-契约-2026-10-12.md）。写操作默认关闭（--allow-write 开启）；"
            "写工具以工程名（name）寻址，改动经命令层落定（快照窗口可 undo/redo）。"
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        on_list_resources=on_list_resources,
        on_list_resource_templates=on_list_resource_templates,
        on_read_resource=on_read_resource,
    )
