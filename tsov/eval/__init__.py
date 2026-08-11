"""tsov.eval —— 评估指标（FR-09 落地，ADR-0006）。

接口先行，M2 填充实现：
- self_contained.py：自足指标（不依赖外部真值）——哼唱素材无真值时先用
- reference.py：参考谱对比（音符级精确率/召回率）——M2 定胜负主指标
铁律：参考谱真值必须独立于候选底座（禁止 crepe_notes / basic-pitch 自扒）。
"""

from .reference import ReferenceMetrics, compare_to_reference
from .self_contained import SelfContainedMetrics, compute_self_contained

__all__ = [
    "SelfContainedMetrics",
    "compute_self_contained",
    "ReferenceMetrics",
    "compare_to_reference",
]
