"""极简 .env 加载（不引 python-dotenv）。

- 从项目根 `.env` 读 KEY=VALUE；**已存在的环境变量不覆盖**（env 优先于 .env）
- .env 已被 .gitignore 排除（密钥不入库，AGENTS.md 硬约束）
"""

from __future__ import annotations

import os
from pathlib import Path

_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def load_env(path: str | Path | None = None) -> None:
    """把 .env 里的 KEY=VALUE 注入 os.environ（不覆盖已有值）。"""
    p = Path(path) if path else _ENV_PATH
    if not p.is_file():
        return
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
