"""madmom 兼容性补丁重放脚本（ADR-0007 决策 3）。

madmom 0.16.1（2020）在 py3.12 + numpy<2 下的两处运行时不兼容：
1. `from collections import MutableSequence` → `from collections.abc`（py3.10 起 collections 不再导出）
2. `np.float`/`np.int`/`np.bool`/`np.object` 别名 → 内建 `float`/`int`/`bool`/`object`（numpy 1.24 移除）

用法：`python scripts/patch_madmom.py <madmom包目录，默认 tsov/.venv/Lib/site-packages/madmom>`
uv sync 重装 madmom 后需重跑一次（补丁是 site-packages 内的就地修改，uv 重装会覆盖）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PATCHES = [
    # (旧字符串, 新字符串, 说明)
    ("from collections import MutableSequence", "from collections.abc import MutableSequence", "collections.MutableSequence → collections.abc"),
]


def patch_np_aliases(text: str) -> tuple[str, int]:
    """替换 np.float/np.int/np.bool/np.object 别名（numpy 1.24 移除）。返回 (新文本, 替换数)。"""
    import re

    count = 0
    for alias, builtin in (("float", "float"), ("int", "int"), ("bool", "bool"), ("object", "object")):
        pattern = rf"np\.{alias}\b"
        text, n = re.subn(pattern, builtin, text)
        count += n
    return text, count


def patch_file(path: Path) -> int:
    """对单个 .py 文件应用补丁，返回改动数。"""
    original = path.read_text(encoding="utf-8")
    text = original
    changes = 0
    for old, new, desc in PATCHES:
        if old in text:
            text = text.replace(old, new)
            changes += 1
            print(f"  [{desc}] {path.name}")
    text, n = patch_np_aliases(text)
    changes += n
    if changes and text != original:
        path.write_text(text, encoding="utf-8")
        if n:
            print(f"  [np 别名 x{n}] {path.name}")
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description="madmom py3.12/numpy<2 兼容补丁重放")
    parser.add_argument("target", nargs="?", default="tsov/.venv/Lib/site-packages/madmom")
    args = parser.parse_args()

    target = Path(args.target)
    if not target.is_dir():
        print(f"madmom 目录不存在：{target}", file=sys.stderr)
        return 1

    total = 0
    for py in sorted(target.rglob("*.py")):
        total += patch_file(py)
    print(f"完成，共 {total} 处补丁。若为 0，可能已补过或路径不对。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
