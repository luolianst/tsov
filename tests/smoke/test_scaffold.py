"""包导入 + CLI 骨架守门（M1 态）。M3 时替换为端到端管线测试。"""

import tsov
from tsov.cli import build_parser


def test_package_imports():
    assert tsov.__version__


def test_cli_subcommands_exist():
    parser = build_parser()
    names = {a.dest: a for a in parser._subparsers._actions if hasattr(a, "choices")}
    assert names
    choices = list(names["command"].choices)
    assert "preprocess" in choices  # M1 可用命令（转码 + 降噪）
    assert "backends" in choices  # 后端清单（DSP 双底座 + 渲染）


def test_cli_preprocess_help():
    import argparse

    try:
        build_parser().parse_args(["preprocess", "--help"])
    except SystemExit as e:
        assert e.code == 0
