"""tsov.arrange —— 自动配器（M7 方向 A：配器 + 节奏）。

单轨旋律 Score → 多轨配器 Score（melody + harmony + bass + drums）。
- harmonize：小节分块 + 音阶 1/4/5 级三和弦选择
- bassline：和弦根音低音轨
- drums：四四拍 pop 基础 pattern
- arrange：组装多轨 Score（原旋律轨 + 新增 3 轨）
"""

from .harmony import Chord, arrange, bassline, drums, harmonize

__all__ = ["arrange", "harmonize", "bassline", "drums", "Chord"]
