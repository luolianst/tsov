"""DSP 转录后端注册表插件模式（照搬 tsov/render/backend.py 套路，ADR-0005 决策 22）。

换转录模型 = 加一个文件（继承 TranscribeBackend，定义 name）+ import 触发自动注册。
pipeline / CLI / eval 零改动，只认 get_backend / list_backends。

M4 定案：game（成品歌）+ rmvpe（哼唱）双后端（CREPE/basic-pitch 已弃用删除）。
"""

from __future__ import annotations

import abc

from ...core.notes import Voice

_REGISTRY: dict[str, "TranscribeBackend"] = {}


class TranscribeBackend(abc.ABC):
    """转录后端适配器基类：音频 wav → Voice 原始层（ADR-0005 schema）。"""

    name: str = "abstract"

    @abc.abstractmethod
    def transcribe(self, audio_path: str, **params) -> Voice:
        """音频 → Voice（Note 列表 + bpm + segments + 溯源）。"""

    @classmethod
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.name != "abstract":
            _REGISTRY[cls.name] = cls()


def get_backend(name: str) -> TranscribeBackend:
    if name not in _REGISTRY:
        raise ValueError(f"未知转录后端：{name!r}（可选 {list_backends()}）")
    return _REGISTRY[name]


def list_backends() -> list[str]:
    return sorted(_REGISTRY)


from . import game  # noqa: E402,F401  导入即注册（成品歌底座）
from . import rmvpe  # noqa: E402,F401  导入即注册（哼唱底座）

__all__ = ["TranscribeBackend", "get_backend", "list_backends", "game", "rmvpe"]
