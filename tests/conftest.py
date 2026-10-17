"""测试全局夹具（D1）：工作目录固定到仓库根 + 共享路径常量。

原因：大量既有用例以 ``output/`` / ``vendor/`` 相对路径访问资产——从别的 cwd 跑会假红。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session", autouse=True)
def _chdir_repo_root():
    old = Path.cwd()
    os.chdir(REPO_ROOT)
    try:
        yield
    finally:
        os.chdir(old)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT
