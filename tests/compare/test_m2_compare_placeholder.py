"""M2 对比测试占位：crepe_notes vs basic-pitch 双底座对比（ADR-0006）。

M1 只保证 pytest 能跑、骨架就位；真正的对比（统一 schema + 自足指标 + 参考谱精确率/召回率）
由 M2 填充，产出对比报告为本验收物。这里放结构占位与后端元信息测试。
"""

from tsov.dsp.transcribe import BACKENDS


def test_dsp_backends_declared():
    """双底座接口已声明（M2 填充实现），不可少。"""
    assert "crepe_notes" in BACKENDS
    assert "basic-pitch" in BACKENDS


def test_compare_suite_placeholder():
    """M2 验收物 = 对比报告；此处为目录占位，M2 填充真实断言。"""
    assert True
