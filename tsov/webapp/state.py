"""Web 全局状态与事件总线（F5：自 tsov/web.py 拆出，行为逐行保留）。

- EventBus / _Subscriber：每工程一组 SSE 订阅者（跨线程 publish → asyncio 队列）
- WebState：单进程单实例；Project 每工程懒加载单例；agent/播放锁
- 工程名校验 / 导入 source 解析（防路径穿越）
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

from fastapi import HTTPException

from ..core.score import Score
from ..host import Project


# ---------------------------------------------------------------------------
# 事件总线：每工程一组 SSE 订阅者（跨线程：工作线程 publish → asyncio 队列）
# ---------------------------------------------------------------------------


class _Subscriber:
    __slots__ = ("loop", "queue")

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.queue: asyncio.Queue = asyncio.Queue()


class EventBus:
    """轻量进程内事件总线：按工程名分发 SSE 事件（docs/05 §三）。"""

    def __init__(self) -> None:
        self._subs: dict[str, list[_Subscriber]] = {}
        self._lock = threading.Lock()

    def subscribe(self, project: str) -> _Subscriber:
        sub = _Subscriber(asyncio.get_running_loop())
        with self._lock:
            self._subs.setdefault(project, []).append(sub)
        return sub

    def unsubscribe(self, project: str, sub: _Subscriber) -> None:
        with self._lock:
            subs = self._subs.get(project, [])
            if sub in subs:
                subs.remove(sub)

    def publish(self, project: str, event: str, data: dict) -> None:
        with self._lock:
            subs = list(self._subs.get(project, []))
        msg = {"event": event, "data": data}
        for sub in subs:
            try:
                sub.loop.call_soon_threadsafe(sub.queue.put_nowait, msg)
            except RuntimeError:  # 订阅者 loop 已关（客户端断开未清理）——丢弃即可
                pass


# ---------------------------------------------------------------------------
# Web 全局状态（单进程单实例；Project 每工程懒加载单例）
# ---------------------------------------------------------------------------


class WebState:
    def __init__(self, output_dir: str | Path = "output") -> None:
        self.output_dir = Path(output_dir)
        self.projects: dict[str, Project] = {}
        self.projects_lock = threading.Lock()
        self.agent_lock = threading.Lock()  # 一次只跑一个 agent 会话
        self.play_lock = threading.Lock()  # 一次只跑一个后端播放
        self.play_stop: threading.Event | None = None  # M-V8 E1：宿主播放停止信号（/play/stop 置位）
        self.agent_stop = threading.Event()  # 用户请求停止当前 agent 会话
        self.chat_sessions: dict[str, dict] = {}  # 工程 → {session_id}（多轮续接同一 JSONL）
        self.bus = EventBus()
        self._engine = None  # HostEngine 懒加载（soundfont 依赖）

    # ---------------- 工程 ----------------

    def project_root(self, name: str) -> Path:
        _validate_project_name(name)
        root = (self.output_dir / name).resolve()
        if self.output_dir.resolve() not in root.parents:
            raise HTTPException(400, f"非法工程名：{name!r}")
        return root

    def project_exists(self, name: str) -> bool:
        try:
            return (self.project_root(name) / "score.json").is_file()
        except HTTPException:
            return False

    def get_project(self, name: str) -> Project:
        """懒加载 Project 单例（Project.open 自动补 git init / 基线提交）。"""
        root = self.project_root(name)
        if not (root / "score.json").is_file():
            raise HTTPException(404, f"工程不存在：{name!r}")
        with self.projects_lock:
            proj = self.projects.get(name)
            if proj is None:
                proj = Project.open(root)
                self.projects[name] = proj
            return proj

    def list_projects(self) -> list[dict]:
        """工程列表（M-V6 批1：带信息版）——name/title/tracks/notes/mtime，按 mtime 倒序（最近修改在前）。

        工程判定不变：output/ 下含 score.json 的目录。坏 score.json 不炸列表（信息留缺省，工程仍可见）。
        """
        if not self.output_dir.is_dir():
            return []
        out: list[dict] = []
        for p in self.output_dir.iterdir():
            sj = p / "score.json"
            if not (p.is_dir() and sj.is_file()):
                continue
            info = {"name": p.name, "title": p.name, "tracks": 0, "notes": 0, "mtime": sj.stat().st_mtime}
            try:
                data = json.loads(sj.read_text(encoding="utf-8"))
                info["title"] = str(data.get("title") or p.name)
                tracks = data.get("tracks") or []
                info["tracks"] = len(tracks)
                info["notes"] = sum(len(t.get("notes") or []) for t in tracks)
            except Exception:  # noqa: BLE001 坏文件不炸列表（信息留缺省）
                pass
            out.append(info)
        out.sort(key=lambda x: (-x["mtime"], x["name"]))
        return out

    def list_import_candidates(self) -> list[dict]:
        """可导入的 score json 候选（M-V6 批1 导入通道）：output/*.json 与 output/*/*.json。

        只列「能解析成 Score」的 json（脚本直出产物如 score_task2.json）；跳过工程自带
        score.json（那本就是工程）与隐藏目录；返回相对路径 + 建议工程名 + 统计信息。
        """
        if not self.output_dir.is_dir():
            return []
        base = self.output_dir.resolve()
        candidates: list[dict] = []
        for pattern in ("*.json", "*/*.json"):
            for p in sorted(self.output_dir.glob(pattern)):
                rel = p.resolve().relative_to(base)
                if any(part.startswith(".") for part in rel.parts):
                    continue
                if p.name == "score.json":
                    continue  # 工程自带谱 = 已是工程
                if len(rel.parts) > 1 and (p.parent / "score.json").is_file():
                    continue  # 工程目录内部的文件不列候选（需导入时手填路径）
                try:
                    score = Score.from_dict(json.loads(p.read_text(encoding="utf-8")))
                except Exception:  # noqa: BLE001 非 Score json → 不是候选
                    continue
                candidates.append({
                    "path": rel.as_posix(),
                    "name_hint": _default_project_name(p),
                    "title": score.title or p.stem,
                    "tracks": len(score.tracks),
                    "notes": sum(len(t.notes) for t in score.tracks),
                    "mtime": p.stat().st_mtime,
                })
        candidates.sort(key=lambda x: (-x["mtime"], x["path"]))
        return candidates

    # ---------------- 宿主引擎 ----------------

    def engine(self):
        if self._engine is None:
            from ..host import HostEngine

            self._engine = HostEngine()
        return self._engine


def _validate_project_name(name: str) -> None:
    """工程名 = 单层目录名（防路径穿越/非法盘符字符）。"""
    if not name or len(name) > 64:
        raise HTTPException(400, "工程名为空或过长（≤64 字符）")
    if name in (".", "..") or ".." in name or "/" in name or ("\\" in name) or name.startswith("."):
        raise HTTPException(400, f"非法工程名：{name!r}")
    if any(ch in name for ch in ':*?"<>|'):
        raise HTTPException(400, f"工程名含非法字符：{name!r}")


def _default_project_name(path: Path) -> str:
    """从导入文件推导建议工程名（M-V6 批1）：score_task2.json → task2；score.json → 目录名。"""
    stem = path.stem
    if stem == "score":
        return path.parent.name or "imported"
    for prefix in ("score_", "score-"):
        if stem.startswith(prefix) and len(stem) > len(prefix):
            return stem[len(prefix):]
    return stem or "imported"


def _resolve_import_source(output_dir: Path, source: str) -> Path:
    """把导入 source 解析为 output/ 内的 json 文件（防路径穿越；目录 → 其 score.json）。"""
    raw = (source or "").strip().strip('"').strip("'")
    if not raw:
        raise HTTPException(400, "source 为空")
    p = Path(raw)
    if not p.is_absolute():
        p = output_dir / p
    base = output_dir.resolve()
    p = p.resolve()
    if p != base and base not in p.parents:
        raise HTTPException(400, f"只允许导入 output/ 内的文件：{source!r}")
    if p.is_dir():
        p = p / "score.json"
    if not p.is_file():
        raise HTTPException(404, f"文件不存在：{source!r}")
    if p.suffix.lower() != ".json":
        raise HTTPException(400, f"只支持 .json 文件：{source!r}")
    return p
