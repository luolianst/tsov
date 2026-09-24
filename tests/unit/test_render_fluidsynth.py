"""fluidsynth 加载幂等回归（2026-09-24 热修）。

背景：_load_fluidsynth 原实现每次调用都向 PATH 前置 dll_dir——Web 长驻进程反复渲染
把 PATH 涨到 32767 上限后，进程环境块超限导致该进程所有 CreateProcess 报 WinError 8
（web 全瘫，事故档 output/web-incident-2026-09-24/）。修复 = 模块级缓存 + PATH 不重复
添加；本测试锁死该行为（防回归）。
"""

from __future__ import annotations

import os

import pytest

from tsov.render import fluidsynth_backend as fb


def test_load_fluidsynth_is_idempotent():
    """多次调用：dll_dir 在 PATH 中最多出现一次；缓存命中后 PATH 零变化。"""
    d = fb._dll_dir()
    if not d:
        pytest.skip("vendor/fluidsynth/bin 不存在（DLL 未下载）")

    # ① 清缓存走「首次加载」路径：第一次调用后 dll_dir 至多一份，第二次调用 PATH 零变化
    fb._FLUID = None
    fb._load_fluidsynth()
    mid = os.environ.get("PATH", "")
    fb._load_fluidsynth()
    after = os.environ.get("PATH", "")
    assert after == mid, "缓存命中后 PATH 不应再变化"

    # ② 连续调用 5 次后 dll_dir 计数仍 ≤ 1（原实现会线性膨胀）
    for _ in range(5):
        fb._load_fluidsynth()
    final = os.environ.get("PATH", "")
    assert final.split(os.pathsep).count(d) <= 1, (
        f"dll_dir 重复注入 PATH：{final.split(os.pathsep).count(d)} 次"
    )
