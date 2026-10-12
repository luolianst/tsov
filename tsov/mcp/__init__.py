"""tsov MCP server（v0.2 批F「MCP 生态」）——外部 agent 经 MCP（stdio）读写 tsov 工程。

- 入口：`tsov mcp`（cli 子命令 → `main(args)`）。
- stdout 协议纪律：mcp 2.x 的 stdio 传输自带 stream-claim（服务期间 fd1→stderr、退出还原，
  见 `mcp/server/stdio.py`）；本包一切日志仍统一走 stderr（`_log`）。
- 接口契约：`docs/MCP接口-契约-2026-10-12.md`（工具/资源清单与安全级）。
"""

from __future__ import annotations

import sys
from pathlib import Path


def _log(msg: str) -> None:
    print(f"[tsov-mcp] {msg}", file=sys.stderr, flush=True)


def main(args=None) -> int:
    """CLI 入口：args = argparse Namespace（root / allow_write / web_guard）。"""
    try:
        import mcp  # noqa: F401  SDK 存在性守卫——缺装时给指路而非裸栈
    except ImportError:
        _log('mcp SDK 未安装：uv pip install "mcp==2.3.0"（或按契约 §7 钉装流程）')
        return 2

    repo_default = Path(__file__).resolve().parents[2]
    raw_root = getattr(args, "root", None) if args is not None else None
    root = Path(raw_root).resolve() if raw_root else repo_default
    allow_write = bool(getattr(args, "allow_write", False))
    web_guard = str(getattr(args, "web_guard", "warn") or "warn")

    # cwd 锚定（预判坑 #3）：宿主 spawn 的 cwd 不定；注册表工具/护栏以仓库根为语境。
    import os

    os.chdir(root)
    from ..env import load_env

    load_env()   # 项目根 .env（LLM 键等；gitignored——照 cli.main 同口径）

    from .server import build_server
    from .sessions import ProjectStore

    store = ProjectStore(root, allow_write=allow_write, web_guard=web_guard)
    server = build_server(store)
    _log(f"stdio 启动：root={root} write={'on' if allow_write else 'off'} web_guard={web_guard}")

    import anyio
    from mcp.server.stdio import stdio_server

    async def _serve() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(_serve)
    return 0
