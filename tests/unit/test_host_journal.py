"""M-V7 D2（ADR-0019）：快照窗口 journal 单测——游标 / 双窗口淘汰 / GC / 持久化 / 动作回跳。

约定：不用 pytest tmp_path（已知坑 81）——手动 output/<uuid> 目录 + rmtree_force 清理。
"""

from __future__ import annotations

import uuid
from pathlib import Path

from tsov.host.journal import ActionJournal

from unit._cleanup import rmtree_force


def _ws():
    d = Path("output") / f"jtest-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_cursor_undo_redo_and_persistence():
    """游标模型：0 = entries[0].pre；k = entries[k-1].post；undo/redo 前后走 + 持久化。"""
    ws = _ws()
    try:
        j = ActionJournal(ws)
        h0 = j.snapshot({"step": 0})
        h1 = j.snapshot({"step": 1})
        h2 = j.snapshot({"step": 2})
        j.append(source="user", label="+1", pre=h0, post=h1)
        j.append(source="user", label="+2", pre=h1, post=h2)
        assert j.cursor == 2 and j.can_undo() and not j.can_redo()

        assert j.undo()["step"] == 1          # cursor 2 → 1（= entries[0].post）
        assert j.undo()["step"] == 0          # cursor 1 → 0（= entries[0].pre）
        assert j.undo() is None and not j.can_undo()
        assert j.redo()["step"] == 1
        assert j.redo()["step"] == 2
        assert j.redo() is None

        # 持久化（服务重启不丢）：游标 + 条目读回
        j2 = ActionJournal(ws)
        assert j2.cursor == 2 and len(j2.entries) == 2
        assert j2.undo()["step"] == 1

        # 新编辑落在游标之后 → 分歧分支标 stale；redo 拒绝越入
        j2.append(source="user", label="+9", pre=h1, post=h2)
        assert j2.entries[1]["stale"] is True
        assert not j2.can_redo()

        # 动作级回跳（jump）：恢复 pre + 其后（含自身）标失效
        assert j2.jump(2)["step"] == 1
        e2 = j2.get(2)
        assert e2["stale"] is True and e2["undone"] is True
        assert j2.jump(999) is None
    finally:
        rmtree_force(ws)


def test_dual_windows_and_gc():
    """双窗口：用户手势留最新 20 条；agent 动作留最新 5 个对话轮；超窗快照 GC。"""
    ws = _ws()
    try:
        j = ActionJournal(ws)
        first_pre = None
        for i in range(25):                    # 25 条用户手势
            pre = j.snapshot({"i": i, "k": "pre"})
            post = j.snapshot({"i": i, "k": "post"})
            if i == 0:
                first_pre = pre
            j.append(source="user", label=f"g{i}", pre=pre, post=post)

        users = [e for e in j.entries if e["source"] == "user"]
        assert len(users) == ActionJournal.USER_WINDOW == 20
        assert users[0]["label"] == "g5"                    # 最旧 5 条被淘汰
        assert not (j.dir / f"{first_pre}.json").is_file()  # 被淘汰条目的快照已 GC
        assert j.cursor == len(j.entries)

        for r in range(8):                     # 8 个对话轮，各 1 条 agent 动作
            j.append(source="agent", label="tool", session_id=f"s{r}", turn=1, round=f"r{r}",
                     pre=j.snapshot({"r": r, "k": "pre"}), post=j.snapshot({"r": r, "k": "post"}))

        rounds = [e["round"] for e in j.entries if e["source"] == "agent"]
        assert len(set(rounds)) == ActionJournal.AGENT_ROUNDS == 5
        assert set(rounds) == {f"r{i}" for i in range(3, 8)}   # 只留最新 5 轮
        assert len([e for e in j.entries if e["source"] == "user"]) == 20  # 用户窗口不受影响
        # 窗口视图
        w = j.window()
        assert w["windows"] == {"user": 20, "agent_rounds": 5}
        assert w["can_undo"] and not w["can_redo"]
    finally:
        rmtree_force(ws)


def test_jump_to_cursor():
    """快照点跳转（M-V7 D3）：任意位置恢复 + 越界钳制 + 缺失不动游标。"""
    ws = _ws()
    try:
        j = ActionJournal(ws)
        h0 = j.snapshot({"step": 0})
        h1 = j.snapshot({"step": 1})
        j.append(source="user", label="a", pre=h0, post=h1)
        j.append(source="user", label="b", pre=h1, post=j.snapshot({"step": 2}))
        r = j.jump_to_cursor(0)
        assert r["cursor"] == 0 and r["snapshot"]["step"] == 0
        r = j.jump_to_cursor(2)
        assert r["cursor"] == 2 and r["snapshot"]["step"] == 2
        assert j.jump_to_cursor(99)["cursor"] == 2          # 越界钳制到尾
        assert ActionJournal(ws).cursor == 2                 # 持久化
    finally:
        rmtree_force(ws)


def test_legacy_index_migration():
    """pre-D2 裸列表格式 index.json → 可读（老条目视为 agent，游标置尾）。"""
    ws = _ws()
    try:
        j = ActionJournal(ws)
        h = j.snapshot({"a": 1})
        j.append(source="agent", label="旧动作", tool="set_tempo", session_id="s", turn=1,
                 pre=h, post=h, impact={"text": "x"})
        # 手工改回裸列表格式（模拟 pre-D2 落盘）
        import json
        (j.dir / "index.json").write_text(
            json.dumps(j.entries, ensure_ascii=False), encoding="utf-8")
        j2 = ActionJournal(ws)
        assert len(j2.entries) == 1 and j2.entries[0]["source"] == "agent"
        assert j2.cursor == 1
    finally:
        rmtree_force(ws)
