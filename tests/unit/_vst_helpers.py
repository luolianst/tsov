"""批L：VST 渲染确定性容忍断言（worker 与进程内两条路共用同一口径）。

实测背景（详见 `output/v02-batchL-verify/`）：
- **起音残渣**：dawdreamer 0.9.0 偶发「音符起音后 ≤~1.5k 样本、幅度 ≤3.2e-05（-90dBFS 级）」微差；
- **收敛微尘**：参数变更序列里前两遍渲染与后续渲染相差 ≤~4e-07（-129dBFS 级，
  约在第 2/3 遍间过渡，此后稳定——2026-10-14 压力循环实测）。

工程口径：**起音窗内 |diff| ≤ 1e-4；起音窗外 |diff| ≤ 1e-6**（典型为 0）。
真故障（错音/时序/串音/外泄）的量级远高于此，仍会被抓。
"""

from __future__ import annotations

import numpy as np


def assert_onset_stable(x, y, onsets, sr: int, tol: float = 1e-4, out_tol: float = 1e-6,
                        pre: int = 128, post: int = 4096) -> None:
    """起音窗内允许 ≤tol；窗外允许 ≤out_tol（契约见模块 docstring）。"""
    d = np.abs(np.asarray(x, dtype=np.float64) - np.asarray(y, dtype=np.float64))
    win = np.zeros(d.shape[0], dtype=bool)
    for t in onsets:
        win[max(0, int(t * sr) - pre): min(len(d), int(t * sr) + post)] = True
    if (~win).any():
        assert float(d[~win].max()) <= out_tol, f"起音窗外微差超限：{float(d[~win].max()):.2e}"
    if win.any():
        assert float(d[win].max()) <= tol, f"起音窗内微差超限：{float(d[win].max()):.2e}"
