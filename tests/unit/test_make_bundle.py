"""make_bundle（网盘一键包打包器）纯函数单测 —— E3 网盘包（2026-10-04）。

只测不落盘/不拷贝的逻辑（版本读取、过滤规则、目录白名单）；全流程 GB 级实测走人工验收（output/netdisk-bundle-2026-10-04/）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_make_bundle():
    spec = importlib.util.spec_from_file_location("make_bundle_mod", ROOT / "scripts" / "make_bundle.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_read_version_matches_package():
    import tsov

    mod = _load_make_bundle()
    assert mod.read_version() == tsov.__version__


def test_ignore_rules():
    mod = _load_make_bundle()
    # 普通源码目录：缓存 + 禁品都排除
    out = mod._ignore(r"C:\b\tsov", [".env", "main.py", "__pycache__", "x.pyc"])
    assert ".env" in out and "__pycache__" in out and "x.pyc" in out and "main.py" not in out
    # venv 内部：保留 pyc（启动更快），但 .env 仍永远排除
    keep = mod._ignore(r"C:\b\tsov\.venv\Lib\site-packages", ["__pycache__", "x.pyc", ".env"])
    assert "x.pyc" not in keep and "__pycache__" not in keep
    assert ".env" in keep
    # GAME venv 同理
    keep2 = mod._ignore(r"C:\b\vendor\GAME\.venv\Lib\site-packages", ["x.pyc"])
    assert "x.pyc" not in keep2


def test_whitelists():
    mod = _load_make_bundle()
    for d in ("tsov", "vendor", "scripts", "docs", "presets"):
        assert d in mod.ROOT_DIRS
    assert ".env.example" in mod.ROOT_FILES
    assert ".env" not in mod.ROOT_FILES
