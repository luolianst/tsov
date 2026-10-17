"""渲染任务调度（P0 A1）：每工程单飞 + 同输入合并。

- 同一工程的渲染串行执行（避免三个入口同时写同一 wav / 截断覆盖）
- 相同输入（谱面内容摘要 + 输出路径）在最近一次完成后直接复用结果（合并请求）
- 结果缓存仅进程内；进程重启丢失无碍（下次重渲）
"""

from __future__ import annotations

import threading


class RenderJobs:
    """按工程名的渲染锁 + 最近完成结果缓存。"""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._done: dict[str, dict] = {}

    def lock(self, name: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(name, threading.Lock())

    def get_done(self, name: str, key: tuple):
        """命中返回上次结果 dict；未命中/输入不同返回 None。"""
        with self._guard:
            rec = self._done.get(name)
        if rec is not None and rec["key"] == key:
            return rec["payload"]
        return None

    def put_done(self, name: str, key: tuple, payload: dict) -> None:
        with self._guard:
            self._done[name] = {"key": key, "payload": payload}

    def forget(self, name: str) -> None:
        """工程删除/回滚后清缓存（下次必重渲）。"""
        with self._guard:
            self._done.pop(name, None)
            self._locks.pop(name, None)
