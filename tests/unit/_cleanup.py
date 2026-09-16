"""测试工作目录清理（handoff 坑 81 衍生，M-V6 批1 实测根因修正）。

**根因**：fixture 创建的工程目录会被 git init（`Project.create`）——Windows 上
`.git/objects` 松散对象文件带**只读属性**，`shutil.rmtree(ignore_errors=True)`
对它们抛 WinError 5 且被静默吞掉 → 残物逐次堆积（实测 wstest-* 212 个 / webtest-* 100 个）。

`rmtree_force`：失败回调 chmod +S_IWRITE 后重试删除；整体重试数轮兜底瞬时锁。
"""

from __future__ import annotations

import os
import shutil
import stat
import time
from pathlib import Path


def _force_remove(func, path, exc) -> None:
    """rmtree 单项失败回调：加写权限后重试（只读属性 = .git/objects 的常见状态）。"""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def rmtree_force(path, attempts: int = 3, delay: float = 0.4) -> None:
    """删除目录（含只读文件）；整体失败重试 attempts 轮（瞬时锁兜底）。"""
    p = Path(path)
    for _ in range(max(1, attempts)):
        shutil.rmtree(p, onexc=_force_remove)   # py3.12+（tsov/.venv = 3.12）
        if not p.exists():
            return
        time.sleep(delay)
