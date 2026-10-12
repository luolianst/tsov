"""MCP 资源（批F F1-4）：projects / summary / score 模板。

`tsov://projects/{name}/render.png` 留 F3（卷帘 PNG 组件需新建）。
"""

from __future__ import annotations

import json
from urllib.parse import unquote

import mcp_types as types

from .sessions import McpError, ProjectStore

PROJECTS_URI = "tsov://projects"
_PROJ_PREFIX = "tsov://projects/"


class ResourceHub:
    """资源注册与读取（URI 一律原样返回；name 段 percent-decode）。"""

    def __init__(self, store: ProjectStore):
        self.store = store

    # ---- 列举 ----

    def list_static(self) -> list[types.Resource]:
        return [
            types.Resource(
                name="projects",
                uri=PROJECTS_URI,
                mime_type="application/json",
                description="tsov 工程列表（name/title/tracks/notes/mtime）",
            )
        ]

    def list_templates(self) -> list[types.ResourceTemplate]:
        return [
            types.ResourceTemplate(
                name="project-summary",
                uri_template=_PROJ_PREFIX + "{name}/summary",
                mime_type="application/json",
                description="工程摘要（速度/调号/轨道清单/音符总数/文件夹）",
            ),
            types.ResourceTemplate(
                name="project-score",
                uri_template=_PROJ_PREFIX + "{name}/score",
                mime_type="application/json",
                description="score.json 全文（大谱注意体量）",
            ),
        ]

    # ---- 读取 ----

    def read(self, uri: str) -> types.ReadResourceResult:
        if uri == PROJECTS_URI:
            return self._text(uri, self.store.list_projects())
        if uri.startswith(_PROJ_PREFIX):
            rest = uri[len(_PROJ_PREFIX):]
            name_part, sep, kind = rest.rpartition("/")
            if sep:
                name = unquote(name_part)
                if kind == "summary":
                    return self._text(uri, self.store.project_status(name))
                if kind == "score":
                    proj = self.store.get_project(name)
                    return self._text(uri, proj.score.to_dict())
        raise McpError(f"未知资源 URI：{uri}")

    # ---- 工具 ----

    def _text(self, uri: str, obj) -> types.ReadResourceResult:
        payload = json.dumps(obj, ensure_ascii=False, default=str)
        return types.ReadResourceResult(
            contents=[types.TextResourceContents(uri=uri, mime_type="application/json", text=payload)]
        )
