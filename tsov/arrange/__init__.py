"""tsov.arrange —— 自动配器（M7 方向 A：配器 + 节奏；E4 段3：三段一库管线）。

两条路径：
- 旧（M7）：单轨旋律 Score → 多轨配器 Score（harmonize/bassline/drums/arrange；4/4 pop）。
- 新（E4 段3 · Q14 三段一库）：模式库（library）→ 事实（facts）→ 决策（decide，LLM 只选 ID）
  → 展开（expand，坐标全机械）→ 命令层采纳。支持 6/8 与 4/4；散文规格进 patterns.json。
"""

from .harmony import Chord, arrange, bassline, drums, harmonize, harmonize_bars
from .library import PatternLibrary, available_packs, load_patterns

__all__ = [
    "arrange", "harmonize", "harmonize_bars", "bassline", "drums", "Chord",
    "PatternLibrary", "available_packs", "load_patterns",
]
