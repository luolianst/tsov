"""M-V7 D2：工程状态文件（计数 / 设置优先级 / 时间戳）单测。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from tsov.host.state import ProjectState, set_global_settings

from unit._cleanup import rmtree_force


def _ws():
    d = Path("output") / f"sstest-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_settings_precedence(monkeypatch):
    """优先级：工程档 > 全局档 > env > 默认；深合并不打掉兄弟字段。"""
    ws = _ws()
    try:
        g = ws / "gsettings.json"
        root = ws / "proj"
        root.mkdir()

        ps = ProjectState(root, global_path=g)
        assert ps.settings()["auto_favorite_iters"] == 40            # 默认
        assert ps.settings()["timed_favorite"] == {"enabled": False, "interval_min": 30}

        monkeypatch.setenv("TSOV_AUTOFAV_ITERS", "17")               # env 覆盖默认
        assert ProjectState(root, global_path=g).settings()["auto_favorite_iters"] == 17

        set_global_settings({"auto_favorite_iters": 21,
                             "timed_favorite": {"enabled": True}}, path=g)   # 全局档覆盖 env
        s = ProjectState(root, global_path=g).settings()
        assert s["auto_favorite_iters"] == 21 and s["timed_favorite"]["enabled"] is True

        ps2 = ProjectState(root, global_path=g)                       # 工程档覆盖全局档
        ps2.set_project_settings({"auto_favorite_iters": 5,
                                  "timed_favorite": {"interval_min": 5}})
        s2 = ProjectState(root, global_path=g).settings()
        assert s2["auto_favorite_iters"] == 5
        assert s2["timed_favorite"]["enabled"] is True                # 深合并：全局的 enabled 保留
        assert s2["timed_favorite"]["interval_min"] == 5
    finally:
        rmtree_force(ws)


def test_counter_git_point_and_timed_stamp():
    """计数：累计 / 持久化 / HEAD 变化自动归零；定时档时间戳持久。"""
    ws = _ws()
    try:
        g = ws / "gsettings.json"
        root = ws / "proj"
        root.mkdir()

        ps = ProjectState(root, global_path=g)
        assert ps.counter == 0
        ps.add_turns(3)
        ps.add_turns(-5)                                              # 负数钳 0
        assert ps.counter == 0
        ps.add_turns(4)
        ps.save()
        assert ProjectState(root, global_path=g).counter == 4         # 持久化

        ps2 = ProjectState(root, global_path=g)
        assert ps2.sync_git_point("aaa") is False                     # 首次记录 → 不归零
        assert ps2.counter == 4
        ps2.save()
        ps3 = ProjectState(root, global_path=g)
        assert ps3.sync_git_point("bbb") is True                      # HEAD 变化 → 归零
        assert ps3.counter == 0 and ps3.last_commit == "bbb"

        ps3.mark_timed_check(1000.0)
        ps3.save()
        assert ProjectState(root, global_path=g).last_timed_check == 1000.0
    finally:
        rmtree_force(ws)
