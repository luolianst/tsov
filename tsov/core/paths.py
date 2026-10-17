"""路径/名字安全（P0 A8）：单段名白名单，防拼接穿越。"""

from __future__ import annotations

_BAD_CHARS = set('/\\:*?"<>|')


def safe_component(value, *, what: str = "名称") -> str:
    """校验「单段名字」（工程名/包名/批次名等）：非空、无路径分隔符、无 ..。"""
    s = str(value or "").strip()
    if not s or len(s) > 128 or s in (".", "..") or ".." in s or any(ch in s for ch in _BAD_CHARS):
        raise ValueError(f"非法{what}：{value!r}")
    return s
