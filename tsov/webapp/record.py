"""全局单录音状态（M-V8 E2 段 3；F5 自 tsov/web.py 拆出）。

端点（routes/audio.py）与测试共用同一 `_recorder` 对象（web.py 兼容再导出=同一对象）。
"""

from __future__ import annotations

import threading


class _Recorder:
    """全局单录音（一次一个；线程 + 停止事件 + 报告）。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.stop_event: threading.Event | None = None
        self.report: dict | None = None
        self.error: str | None = None
        self.project: str | None = None
        self.started_at: float | None = None


_recorder = _Recorder()
