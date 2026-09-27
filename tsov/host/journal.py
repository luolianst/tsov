"""写入快照日志（双账本 · 弱留存）—— ADR-0019 / M-V7 D2。

- 每个写入动作（用户手势 / agent 工具调用 / 采用结果 / 回滚）存 pre/post 两份快照
  （内容寻址 `sha1[:12]` → `<哈希>.json`；同内容只存一份）。
- **双窗口**（超窗即淘汰 + GC 未被引用快照）：
  - 用户手势：留最新 `USER_WINDOW = 20` 条；
  - agent 动作：留最新 `AGENT_ROUNDS = 5` 个**对话轮**（round = 一次用户消息触发的 agent 会话）。
- **游标** cursor ∈ [0..n]：当前状态在条目序列中的位置——
  cursor==0 = 第一条目之前（= entries[0].pre）；cursor==k = entries[k-1].post。
  `undo()/redo()` = 游标前后移动并返回对应快照（由 Project 落盘，**零 git 操作**）。
  新编辑落在游标之后时，其后条目标 `stale`（分歧分支；`redo` 拒绝越入）。
- 服务重启不丢：`index.json` 持久化 entries + cursor（兼容 pre-D2 裸列表格式）。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path


class ActionJournal:
    """单工程写入快照日志（弱留存窗口，ADR-0019）。"""

    USER_WINDOW = 20      # 用户手势窗口（条）
    AGENT_ROUNDS = 5      # agent 对话轮窗口（轮）

    def __init__(self, root: Path | str):
        self.dir = Path(root) / ".agent-actions"
        self.index_path = self.dir / "index.json"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.entries: list[dict] = []
        self.cursor = 0
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("entries"), list):
                self.entries = [e for e in data["entries"] if isinstance(e, dict)]
                self.cursor = int(data.get("cursor", len(self.entries)))
            elif isinstance(data, list):   # 兼容 pre-D2 的裸列表格式
                self.entries = [e for e in data if isinstance(e, dict)]
                self.cursor = len(self.entries)
        except (OSError, ValueError):
            self.entries = []
        for e in self.entries:          # 老条目补字段（pre-D2 条目视为 agent）
            e.setdefault("source", "agent")
            e.setdefault("round", str(e.get("session_id") or "-"))
            e.setdefault("stale", False)
            e.setdefault("undone", False)
            e.setdefault("stats", {})
        self.cursor = max(0, min(self.cursor, len(self.entries)))
        self._seq = max([int(e.get("seq", 0)) for e in self.entries], default=0)

    # ---- 快照 ----
    def snapshot(self, score: dict | None) -> str | None:
        """存快照（内容寻址）→ hash；非 dict / 缺内容 → None。"""
        if not isinstance(score, dict):
            return None
        data = json.dumps(score, ensure_ascii=False, sort_keys=True).encode("utf-8")
        h = hashlib.sha1(data).hexdigest()[:12]
        p = self.dir / f"{h}.json"
        if not p.is_file():
            p.write_bytes(data)
        return h

    def load(self, h: str | None) -> dict | None:
        if not h:
            return None
        try:
            return json.loads((self.dir / f"{h}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    # ---- 条目 ----
    def append(self, *, source: str = "agent", label: str = "", pre: str | None = None,
               post: str | None = None, tool: str | None = None, args: str | None = None,
               impact: dict | None = None, session_id: str | None = None,
               turn: int | None = None, round: str | None = None,
               summary: str | None = None, stats: dict | None = None) -> dict:
        """追加条目；游标之后若有条目 → 先标 stale（分歧分支）。"""
        for e in self.entries[self.cursor:]:
            e["stale"] = True
        self._seq += 1
        entry = {
            "seq": self._seq, "source": source, "label": label or tool or "",
            "tool": tool, "args": args, "pre": pre, "post": post,
            "impact": impact or ({"text": summary} if summary else {"text": ""}),
            "session_id": session_id, "turn": turn,
            "round": round or (str(session_id) if session_id else None),
            "stats": stats or {},
            "ts": time.time(), "stale": False, "undone": False,
        }
        self.entries.append(entry)
        self.cursor = len(self.entries)
        self._trim()
        self._save()
        return entry

    def get(self, seq: int) -> dict | None:
        for e in self.entries:
            if int(e.get("seq", 0)) == int(seq):
                return e
        return None

    def mark_stale_from(self, seq: int, undone: bool = True) -> None:
        """撤销 #seq → 其后（含自身）动作标记失效（前端置灰的依据）。"""
        for e in self.entries:
            if int(e.get("seq", 0)) >= int(seq):
                e["stale"] = True
                if int(e.get("seq", 0)) == int(seq):
                    e["undone"] = undone
        self._save()

    # ---- 游标（undo/redo/动作撤销） ----
    def can_undo(self) -> bool:
        return bool(self.entries) and self.cursor > 0

    def can_redo(self) -> bool:
        return bool(self.entries) and self.cursor < len(self.entries) \
            and not self.entries[self.cursor].get("stale")

    def snapshot_at(self, cursor: int) -> dict | None:
        """游标位置的状态快照：0 = entries[0].pre；k = entries[k-1].post。"""
        if not self.entries:
            return None
        if cursor <= 0:
            return self.load(self.entries[0].get("pre"))
        return self.load(self.entries[cursor - 1].get("post"))

    def undo(self) -> dict | None:
        """游标后退一步 → 该位置快照；无处可退 → None。"""
        if not self.can_undo():
            return None
        self.cursor -= 1
        self._save()
        return self.snapshot_at(self.cursor)

    def redo(self) -> dict | None:
        """游标前进一步（前方条目已失效则拒绝）→ 该位置快照；无路可进 → None。"""
        if not self.can_redo():
            return None
        self.cursor += 1
        self._save()
        return self.snapshot_at(self.cursor)

    def jump(self, seq: int) -> dict | None:
        """动作级撤销（批B 动作卡）：跳到 #seq 动作前 —— 返回快照；其后（含自身）标失效。"""
        for i, e in enumerate(self.entries):
            if int(e.get("seq", 0)) == int(seq):
                self.cursor = i
                self.mark_stale_from(seq)
                self._save()
                return self.snapshot_at(i)
        return None

    def jump_to_cursor(self, cursor: int) -> dict | None:
        """快照点跳转（M-V7 D3 回滚下拉「最近窗口」段）：游标移到任意位置并返回该点快照。

        - 越界钳制；快照缺失 → 不动游标、返回 None。
        - 从旧点往后编辑时，其后条目由 append() 的分歧逻辑标 stale（与 undo 一致）。
        """
        if not self.entries:
            return None
        k = max(0, min(int(cursor), len(self.entries)))
        snap = self.snapshot_at(k)
        if snap is None:
            return None
        self.cursor = k
        self._save()
        return {"cursor": k, "snapshot": snap}

    def window(self) -> dict:
        """窗口视图（供 GET /window / D3 UI 消费）。"""
        return {
            "entries": self.entries,
            "cursor": self.cursor,
            "can_undo": self.can_undo(),
            "can_redo": self.can_redo(),
            "windows": {"user": self.USER_WINDOW, "agent_rounds": self.AGENT_ROUNDS},
        }

    # ---- 淘汰 / GC ----
    def _trim(self) -> None:
        if not self.entries:
            return
        drop: set[int] = set()
        user_idx = [i for i, e in enumerate(self.entries) if e.get("source") == "user"]
        if len(user_idx) > self.USER_WINDOW:
            drop.update(user_idx[:-self.USER_WINDOW])
        rounds: list[str] = []
        for e in self.entries:
            if e.get("source") == "agent":
                rk = str(e.get("round") or e.get("session_id") or "-")
                if not rounds or rounds[-1] != rk:
                    rounds.append(rk)
        if len(rounds) > self.AGENT_ROUNDS:
            keep = set(rounds[-self.AGENT_ROUNDS:])
            for i, e in enumerate(self.entries):
                if e.get("source") == "agent" and \
                        str(e.get("round") or e.get("session_id") or "-") not in keep:
                    drop.add(i)
        if not drop:
            return
        self.cursor = max(0, self.cursor - sum(1 for i in drop if i < self.cursor))
        dropped = [self.entries[i] for i in sorted(drop)]
        self.entries = [e for i, e in enumerate(self.entries) if i not in drop]
        keep_h = {e.get("pre") for e in self.entries} | {e.get("post") for e in self.entries}
        for e in dropped:
            for h in (e.get("pre"), e.get("post")):
                if h and h not in keep_h:
                    try:
                        (self.dir / f"{h}.json").unlink()
                    except OSError:
                        pass
        self._save()

    def _save(self) -> None:
        try:
            self.index_path.write_text(
                json.dumps({"entries": self.entries, "cursor": self.cursor},
                           ensure_ascii=False, indent=1),
                encoding="utf-8")
        except OSError:
            pass
