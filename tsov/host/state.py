"""工程状态文件（ADR-0019 / M-V7 D2）：迭代计数 + 留存设置 + 时间戳。

- 落盘：工程内 `.tsov-state.json`（**不进 git**；服务重启不丢）。
- 设置读取优先级（更具体者优先）：
  **工程档 `.tsov-state.json` > 全局档 `<仓库根>/tsov-settings.json` > env > 内置默认**。
- 计数器口径：自上次 git 点以来的 **agent 迭代轮数**（同 `AGENT_MAX_TURNS` 口径；
  用户手势不计入）；任何 HEAD 变化（收藏/回滚等）→ 自动归零。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

FILE = ".tsov-state.json"
GLOBAL_FILE = "tsov-settings.json"

DEFAULTS: dict = {
    "auto_favorite_iters": 40,
    "timed_favorite": {"enabled": False, "interval_min": 30},
}

ENV_ITERS = "TSOV_AUTOFAV_ITERS"
ENV_TIME = "TSOV_AUTOFAV_TIME"          # "1"/"on"/"true" 开
ENV_TIME_MIN = "TSOV_AUTOFAV_TIME_MIN"


def global_settings_path() -> Path:
    """全局档路径 = 仓库根 `tsov-settings.json`（tsov 包目录的上一级）；TSOV_SETTINGS_PATH 可覆盖。"""
    env = (os.environ.get("TSOV_SETTINGS_PATH") or "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / GLOBAL_FILE


def _deep_merge(base: dict, patch: dict | None) -> dict:
    if not isinstance(patch, dict):
        return base
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _read_json(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


class ProjectState:
    """单工程状态：计数器 / 设置 / 时间戳（工程档 + 全局档 + env 合成）。"""

    def __init__(self, root: Path | str, *, global_path: Path | str | None = None):
        self.root = Path(root)
        self.path = self.root / FILE
        self.global_path = Path(global_path) if global_path else global_settings_path()
        d = _read_json(self.path)
        self.data: dict = d if isinstance(d, dict) else {}

    # ---- 持久化 ----
    def save(self) -> None:
        try:
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
        except OSError:
            pass

    # ---- 计数器 ----
    @property
    def counter(self) -> int:
        try:
            return max(0, int(self.data.get("iter_counter", 0)))
        except (TypeError, ValueError):
            return 0

    def add_turns(self, n: int) -> int:
        self.data["iter_counter"] = max(0, self.counter + int(n))
        return self.data["iter_counter"]

    def reset_counter(self) -> None:
        self.data["iter_counter"] = 0

    @property
    def last_commit(self) -> str:
        return str(self.data.get("last_commit") or "")

    def sync_git_point(self, head: str | None) -> bool:
        """HEAD 变化 → 计数器归零（任何收藏/回滚等 commit 都算新起点）。返回是否归零。"""
        h = str(head or "")
        moved = bool(self.last_commit) and bool(h) and self.last_commit != h
        if moved:
            self.data["iter_counter"] = 0
        if h:
            self.data["last_commit"] = h
        return moved

    # ---- 定时档时间戳 ----
    @property
    def last_timed_check(self) -> float:
        try:
            return float(self.data.get("last_timed_check", 0.0))
        except (TypeError, ValueError):
            return 0.0

    def mark_timed_check(self, ts: float | None = None) -> None:
        self.data["last_timed_check"] = float(ts if ts is not None else time.time())

    # ---- 设置（工程档 > 全局档 > env > 默认） ----
    def settings(self) -> dict:
        merged = json.loads(json.dumps(DEFAULTS))

        # env（服务器级默认）
        env_iters = os.environ.get(ENV_ITERS)
        if env_iters is not None:
            try:
                merged["auto_favorite_iters"] = int(env_iters)
            except ValueError:
                pass
        env_time = (os.environ.get(ENV_TIME) or "").strip().lower()
        if env_time in ("1", "on", "true", "yes"):
            merged["timed_favorite"]["enabled"] = True
        env_min = os.environ.get(ENV_TIME_MIN)
        if env_min is not None:
            try:
                merged["timed_favorite"]["interval_min"] = float(env_min)
            except ValueError:
                pass

        # 全局档 → 工程档（更具体者覆盖）
        _deep_merge(merged, _read_json(self.global_path))
        _deep_merge(merged, self.data.get("settings") if isinstance(self.data.get("settings"), dict) else {})
        return merged

    def set_project_settings(self, patch: dict) -> dict:
        """写工程档设置（浅进深合并到 data['settings']）。返回合并后的 settings。"""
        cur = self.data.get("settings")
        if not isinstance(cur, dict):
            cur = {}
            self.data["settings"] = cur
        _deep_merge(cur, patch or {})
        self.save()
        return self.settings()


def set_global_settings(patch: dict, *, path: Path | str | None = None) -> dict:
    """写全局档（仓库根 tsov-settings.json）。返回合并后的全局档内容。"""
    p = Path(path) if path else global_settings_path()
    cur = _read_json(p)
    _deep_merge(cur, patch or {})
    try:
        p.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    return cur
