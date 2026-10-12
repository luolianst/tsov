"""MCP 工程会话层（批F F1-2）：列表/打开/摘要 + 防穿越。

写锁与 Web 共存守卫留 F2 接入（契约 §5）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from ..host import Project


class McpError(Exception):
    """MCP 工具层可读错误（→ isError 文本；不吞信息）。"""


class ProjectStore:
    """工程域 = <repo>/output 下含 score.json 的目录（口径同 Web 壳 WebState）。"""

    def __init__(self, repo_root, *, allow_write: bool = False, web_guard: str = "warn"):
        self.repo_root = Path(repo_root).resolve()
        self.output_dir = self.repo_root / "output"
        self.allow_write = bool(allow_write)
        self.web_guard = web_guard if web_guard in ("warn", "strict", "off") else "warn"
        self._projects: dict[str, Project] = {}
        self._lock = threading.Lock()

    # ---- 名称 → 路径（防穿越） ----

    def project_root(self, name: str) -> Path:
        nm = str(name or "").strip()
        if not nm or nm in (".", "..") or ("/" in nm) or ("\\" in nm):
            raise McpError(f"非法工程名：{name!r}")
        root = self.output_dir / nm
        if root.resolve().parent != self.output_dir.resolve():
            raise McpError(f"工程名越界：{name!r}")
        if not (root / "score.json").is_file():
            raise McpError(f"工程不存在或缺 score.json：{nm}")
        return root

    # ---- 列表（口径同 WebState.list_projects：坏文件不炸） ----

    def list_projects(self) -> list[dict]:
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

    # ---- 打开（懒加载单例）/ 摘要 ----

    def get_project(self, name: str) -> Project:
        nm = str(name or "").strip()
        root = self.project_root(nm)
        with self._lock:
            proj = self._projects.get(nm)
            if proj is None:
                try:
                    proj = Project.open(root)
                except Exception as e:  # noqa: BLE001 载入失败 → 可读错误
                    raise McpError(f"工程打开失败：{nm}（{type(e).__name__}: {e}）") from e
                self._projects[nm] = proj
            return proj

    def project_status(self, name: str) -> dict:
        """结构化摘要：速度/调号/轨道清单/音符总数/文件夹（agent 友好）。"""
        proj = self.get_project(name)
        score = proj.score
        return {
            "name": proj.name,
            "title": str(getattr(score, "title", "") or proj.name),
            "tempo": float(getattr(score, "tempo", 120.0) or 120.0),
            "keys": [str(k.key) for k in (getattr(score, "key_candidates", None) or [])],
            "tracks": [
                {
                    "index": i,
                    "name": t.name,
                    "kind": str(getattr(t, "kind", "midi") or "midi"),
                    "program": t.instrument.program,
                    "notes": len(t.notes),
                }
                for i, t in enumerate(score.tracks)
            ],
            "notes_total": sum(len(t.notes) for t in score.tracks),
            "folders": dict(getattr(score, "folders", None) or {}),
            "root": str(proj.root),
        }

    # ---- 建工程（F2；与 Web 壳导入口径同源：source 须在仓库内、Score 校验） ----

    def create_project(self, name: str, source: str, title: str | None = None) -> dict:
        nm = str(name or "").strip()
        if not nm or nm in (".", "..") or ("/" in nm) or ("\\" in nm):
            raise McpError(f"非法工程名：{name!r}")
        root = self.output_dir / nm
        if root.resolve().parent != self.output_dir.resolve():
            raise McpError(f"工程名越界：{name!r}")
        if (root / "score.json").is_file():
            raise McpError(f"工程已存在：{nm}")
        rel = str(source or "").strip()
        if not rel:
            raise McpError("create_project 需要 source（score JSON 路径）")
        p = Path(rel)
        src = (p if p.is_absolute() else (self.repo_root / p)).resolve()
        if src != self.repo_root and self.repo_root not in src.parents:
            raise McpError(f"source 越出仓库范围：{rel!r}")
        if not src.is_file():
            raise McpError(f"source 不存在：{rel!r}")
        try:
            from ..core.score import Score

            data = json.loads(src.read_text(encoding="utf-8"))
            score = Score.from_dict(data)
        except Exception as e:  # noqa: BLE001 载入失败 → 可读错误
            raise McpError(f"source 不是合法 Score JSON：{type(e).__name__}: {e}") from e
        if title:
            score.title = str(title)
        proj = Project.create(nm, score, parent=self.output_dir)
        with self._lock:
            self._projects[nm] = proj
        try:
            rel_out = src.resolve().relative_to(self.repo_root.resolve()).as_posix()
        except ValueError:
            rel_out = str(src)
        return {
            "ok": True,
            "name": nm,
            "source": rel_out,
            "tracks": len(score.tracks),
            "notes_total": sum(len(t.notes) for t in score.tracks),
        }

    # ---- 写锁（F2；契约 §5 定稿案：<工程>/.tsov-lock） ----

    def write_lock(self, name: str):
        """写操作互斥（MCP↔MCP；活锁拒绝、陈旧回收）。用法：with store.write_lock('工程名'): ..."""
        from contextlib import contextmanager

        root = self.project_root(name)
        lf = root / ".tsov-lock"
        me = os.getpid()

        @contextmanager
        def _hold():
            if lf.exists():
                try:
                    info = json.loads(lf.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001 坏锁文件 → 当陈旧处理
                    info = {}
                pid = int(info.get("pid") or 0)
                if pid and pid != me and _pid_alive(pid):
                    raise McpError(
                        f"工程被占用：锁 pid={pid}（source={info.get('source', '?')}，"
                        f"自 {info.get('ts', '?')}）——稍后重试或待其退出"
                    )
            lf.write_text(
                json.dumps({"pid": me, "ts": time.time(), "source": "mcp"}, ensure_ascii=False),
                encoding="utf-8",
            )
            try:
                yield
            finally:
                try:
                    cur = json.loads(lf.read_text(encoding="utf-8"))
                    if int(cur.get("pid") or 0) == me:
                        lf.unlink()
                except Exception:  # noqa: BLE001 收尾失败不掩业务结果
                    pass

        return _hold()

    # ---- Web 共存守卫（F2；契约 §5：默认 warn / strict 拒绝 / off 关） ----

    def web_guard_note(self) -> str | None:
        """写前探测 Web 壳（8790）；存活 → 返回警告文本（strict 则抛错）。"""
        if self.web_guard == "off":
            return None
        if not _web_alive():
            return None
        msg = ("⚠ Web 壳（127.0.0.1:8790）正在运行：若同时有人在网页端编辑同一工程，"
               "两边可能相互覆盖——建议分开操作（本提示不影响本次执行）。")
        if self.web_guard == "strict":
            raise McpError(f"Web 壳在运行（--web-guard strict 拒绝写操作）：{msg}")
        return msg


def _pid_alive(pid: int) -> bool:
    """进程存活检测（Windows 走 OpenProcess；POSIX 走 kill 0）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        try:
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        except Exception:  # noqa: BLE001
            return False
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _web_alive() -> bool:
    """探测 8790 /api/health（0.3s 超时；强制绕过环境代理）。"""
    import urllib.request

    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open("http://127.0.0.1:8790/api/health", timeout=0.3) as resp:
            return 200 <= int(getattr(resp, "status", 0)) < 300
    except Exception:  # noqa: BLE001 不在跑 / 非 2xx / 超时 → 视为无
        return False
