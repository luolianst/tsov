"""工程（M-V1，ADR-0015；M-V7 D2 双账本）：Score 真相 + 快照窗口 undo/redo + git 收藏版本 + 工程摘要。

- 工程根 `output/<name>/`：`score.json` 是真相；git 只管**收藏**（Score JSON + MIDI 入库，WAV 不入库）
- 弱留存 = 快照窗口（`host/journal.py`：用户手势 20 步 / agent 对话 5 轮；游标 undo/redo，零 commit）
- 强留存 = `favorite()`：**commit + tag 二连**（有改动才 commit）——ADR-0019 修订 ADR-0015「每轮一 commit」
- summary() = 工程摘要（LLM 上下文节流，docs/04 §四；供 agent 工具与 Web UI 共用）
"""

from __future__ import annotations

import copy
import json
import subprocess
import time
from pathlib import Path

from ..analysis.dataset import midi_to_note_name
from ..analysis.key import detect_key  # M-V2.4：命令层编辑后调性同步（与 edit_score 共用实现）
from ..core.score import Score
from .command import EditBatch
from .diff import diff_notes
from .journal import ActionJournal

SUMMARY_NOTE_CAP = 80  # 摘要里每轨最多列出的音符数


class Project:
    """一个音乐工程：Score + 历史 + git。"""

    def __init__(self, score: Score, root: Path, name: str = "untitled"):
        self.score = score
        self.root = Path(root)
        self.name = name
        self._journal: ActionJournal | None = None   # 快照窗口（懒建，M-V7 D2）
        self.root.mkdir(parents=True, exist_ok=True)
        self._ensure_git()
        if not (self.root / "score.json").exists():
            self.save()
        if not self.log(1):
            # 基线提交：任何时刻都能 rollback 回初始状态
            self.commit(f"init: {name}")

    # ------------------------------------------------------------------
    # 载入/落盘
    # ------------------------------------------------------------------

    @classmethod
    def create(cls, name: str, score: Score, parent: str | Path = "output") -> "Project":
        """新建工程：output/<name>/（git init + 初始 score.json 落盘）。"""
        return cls(score=score, root=Path(parent) / name, name=name)

    @classmethod
    def open(cls, root: str | Path) -> "Project":
        """打开已有工程目录（读 score.json）。"""
        root = Path(root)
        score = Score.from_dict(json.loads((root / "score.json").read_text(encoding="utf-8")))
        return cls(score=score, root=root, name=root.name)

    def save(self) -> Path:
        path = self.root / "score.json"
        path.write_text(json.dumps(self.score.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    # ------------------------------------------------------------------
    # 编辑事务 + 历史
    # ------------------------------------------------------------------

    def apply_batch(self, batch: EditBatch, commit_message: str | None = None, *,
                    source: str = "user", record: bool = True) -> dict:
        """应用一批编辑命令（事务）→ 结果 dict（diff/错误/窗口 seq）。

        - 全部命令被拒绝时：返回 {ok: False, applied: 0, errors}，工程不变
        - 部分应用：合法命令生效并落盘，errors 列出被拒命令
        - M-V7 D2（ADR-0019）：**不再 commit**；改为记快照窗口条目
          （source=user/agent；record=False 供窗口自身操作静默）
        """
        new_score, result = batch.apply(self.score)
        if result.applied == 0:
            return {
                "ok": False,
                "applied": 0,
                "errors": result.errors,
                "diff": {"added": [], "removed": [], "changed": [], "summary": "无改动", "total": 0},
                "commit": None,
                "seq": None,
            }

        pre_dict = self.score.to_dict()
        old_notes = self.score.tracks[0].notes if self.score.tracks else []
        new_notes = new_score.tracks[0].notes if new_score.tracks else []
        note_diff = diff_notes(old_notes, new_notes)

        self.score = new_score
        # M-V2.4（审计修复）：命令层编辑后重算调性标签——原 bug：/batch transpose 后 key_candidates 陈旧
        self.score.key_candidates = detect_key(self.score.tracks[0].notes if self.score.tracks else [])
        self.save()

        message = commit_message or batch.label or "编辑"
        seq = None
        if record:
            entry = self.journal.append(
                source=source, label=message,
                pre=self.journal.snapshot(pre_dict),
                post=self.journal.snapshot(self.score.to_dict()),
                summary=note_diff.summary(),
            )
            seq = entry["seq"]

        return {
            "ok": result.ok,
            "applied": result.applied,
            "errors": result.errors,
            "diff": note_diff.to_dict(),
            "commit": None,
            "seq": seq,
        }

    @property
    def journal(self) -> ActionJournal:
        """快照窗口（懒建；随工程根持久化，服务重启不丢）。"""
        if self._journal is None:
            self._journal = ActionJournal(self.root)
        return self._journal

    @property
    def can_undo(self) -> bool:
        return self.journal.can_undo()

    @property
    def can_redo(self) -> bool:
        return self.journal.can_redo()

    def apply_score(self, new_score: Score, commit_message: str = "整谱替换", *,
                    source: str = "user", record: bool = True) -> dict:
        """整谱落定（agent 高层语义编辑结果的采用入口，与 apply_batch 同协议）。

        - 与 apply_batch 的区别：不经命令动词，直接用新 Score 替换（edit_score/style 等
          高层工具的输出是完整新谱，拆回命令动词既冗余又易错——ADR-0015 决策 4 预留此路径）
        - M-V7 D2（ADR-0019）：**不再 commit**；改记快照窗口条目
        - 无实际改动时：返回 {ok: False, applied: 0, ...}，工程不变、不产生空条目
        """
        old_notes = self.score.tracks[0].notes if self.score.tracks else []
        new_notes = new_score.tracks[0].notes if new_score.tracks else []
        note_diff = diff_notes(old_notes, new_notes)
        if note_diff.total == 0 and self.score.to_dict() == new_score.to_dict():
            return {
                "ok": False,
                "applied": 0,
                "errors": ["新旧谱一致，无改动"],
                "diff": note_diff.to_dict(),
                "commit": None,
                "seq": None,
            }

        pre_dict = self.score.to_dict()
        # 深拷贝隔离外部引用（调用方可能继续使用 new_score）
        self.score = copy.deepcopy(new_score)
        self.save()
        seq = None
        if record:
            seq = self._journal_append_apply(pre_dict, commit_message, source, note_diff.summary())
        return {
            "ok": True,
            "applied": 1,
            "errors": [],
            "diff": note_diff.to_dict(),
            "commit": None,
            "seq": seq,
        }

    def _journal_append_apply(self, pre_dict: dict, label: str, source: str, summary: str = "") -> int | None:
        """记窗口条目；**去重**：若窗口末条 post 已是该状态（agent 工具链已记录），只返回其 seq。

        典型场景：agent 会话里 edit_score 工具已记 `S0→S_final`；采用（apply_score）同行
        会再记 `S0→S_final` —— 重复条目会让"撤销一步"落在中间态（实测）。去重后一步到位。
        """
        j = self.journal
        post_hash = j.snapshot(self.score.to_dict())
        if j.cursor == len(j.entries) and j.entries:
            last = j.entries[-1]
            if last.get("post") == post_hash:
                return int(last.get("seq", 0)) or None
        return j.append(source=source, label=label,
                        pre=j.snapshot(pre_dict), post=post_hash,
                        summary=summary).get("seq")

    def undo(self) -> bool:
        """窗口游标后退一步（零 commit）；恢复失败/无路可退返回 False。"""
        snap = self.journal.undo()
        if snap is None:
            return False
        try:
            self.score = Score.from_dict(snap)
        except Exception:  # noqa: BLE001 快照损坏 → 游标回位
            self.journal.redo()
            return False
        self.save()
        return True

    def redo(self) -> bool:
        """窗口游标前进一步（零 commit）；无路可进/前方已失效返回 False。"""
        snap = self.journal.redo()
        if snap is None:
            return False
        try:
            self.score = Score.from_dict(snap)
        except Exception:  # noqa: BLE001 快照损坏 → 游标回位
            self.journal.undo()
            return False
        self.save()
        return True

    # ------------------------------------------------------------------
    # git 版本
    # ------------------------------------------------------------------

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def _ensure_git(self) -> None:
        if not (self.root / ".git").exists():
            self._git("init", "-q")
        gitignore = self.root / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(
                "# 音频产物与运行时状态不入库（工程版本只存 Score JSON + MIDI）\n"
                "*.wav\n*.mp3\n*.flac\n*.m4a\n"
                ".agent-actions/\n.tsov-state.json\nagent-edited-score.json\n",
                encoding="utf-8")

    def commit(self, message: str) -> str | None:
        """git add -A + commit；返回短 hash（失败返回 None）。"""
        if not (self.root / ".git").exists():
            return None
        self._git("add", "-A")
        proc = self._git("commit", "-m", message)
        if proc.returncode == 0:
            short = self._git("rev-parse", "--short", "HEAD")
            return short.stdout.strip() or None
        # 无改动可提交（commit 返回非零）不算失败
        return None

    def log(self, n: int = 20) -> list[str]:
        if not (self.root / ".git").exists():
            return []
        proc = self._git("log", "--oneline", f"-{int(n)}")
        return [ln for ln in proc.stdout.strip().splitlines() if ln]

    def score_at(self, rev: str = "HEAD") -> Score | None:
        """读取任意 git 版本的 Score（git show <rev>:score.json），不改变工作区/HEAD。

        用于 A/B 对比试听（议题 ④）：版本树左槽只读参考，禁止旧版分叉编辑。
        版本不存在/损坏 → 返回 None。
        """
        if not (self.root / ".git").exists():
            return None
        proc = self._git("show", f"{rev}:score.json")
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        try:
            return Score.from_dict(json.loads(proc.stdout))
        except Exception:  # noqa: BLE001 版本内容损坏
            return None

    def rollback(self, rev: str = "HEAD~1") -> bool:
        """回滚到指定版本（只动 score.json；M-V7 D2：记快照窗口条目，零 commit）。"""
        if not (self.root / ".git").exists() or not self.log():
            return False
        pre_dict = self.score.to_dict()
        proc = self._git("checkout", rev, "--", "score.json")
        if proc.returncode != 0:
            return False
        try:
            self.score = Score.from_dict(json.loads((self.root / "score.json").read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001 检出损坏 → 还原
            self._git("checkout", "HEAD", "--", "score.json")
            return False
        self.journal.append(
            source="user", label=f"回滚到 {rev}",
            pre=self.journal.snapshot(pre_dict),
            post=self.journal.snapshot(self.score.to_dict()),
            summary=f"回滚 {rev}",
        )
        return True

    def jump_window(self, cursor: int) -> bool:
        """快照点跳转（M-V7 D3 回滚下拉「最近窗口」段）：恢复到窗口内任意位置，零 commit。"""
        r = self.journal.jump_to_cursor(int(cursor))
        if not r or not r.get("snapshot"):
            return False
        try:
            self.score = Score.from_dict(r["snapshot"])
        except Exception:  # noqa: BLE001 快照损坏
            return False
        self.save()
        return True

    def delete_favorite(self, tag: str) -> bool:
        """删除收藏 tag（只删标签，不动提交；M-V7 D3）。"""
        if not (self.root / ".git").exists() or not str(tag).startswith("fav/"):
            return False
        proc = self._git("tag", "-d", str(tag))
        return proc.returncode == 0

    # ------------------------------------------------------------------
    # 工程摘要（LLM 上下文节流）
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 收藏（修正轮2：强留存 = git tag；Q44 版本留存机制落地后可演进）
    # ------------------------------------------------------------------

    def favorite(self, name: str | None = None, *, auto: bool = False, timed: bool = False) -> dict:
        """收藏当前版本 = **commit（有改动才提交）+ tag 二连**（ADR-0019 强留存）。

        tag 形式：`fav/<时间戳>[-auto | -time | -标签]`。
        """
        if not (self.root / ".git").exists():
            return {"ok": False, "error": "工程无 git 仓库"}
        label = "".join(ch for ch in str(name or "") if (ch.isalnum() or ch in "-_"))[:32]
        if auto:
            suffix, message = "-auto", f"自动收藏：{label or '迭代收藏'}"
        elif timed:
            suffix, message = "-time", f"定时收藏：{label or '定时'}"
        else:
            suffix = ("-" + label) if label else ""
            message = f"收藏：{label or '手动'}"
        base_tag = "fav/" + time.strftime("%Y%m%d-%H%M%S") + suffix
        tag, n = base_tag, 2
        while (self._git("tag", "--list", tag).stdout or "").strip():   # 同秒连收 → 加序号防撞
            tag = f"{base_tag}-{n}"
            n += 1
        committed = self.commit(message) if self.is_dirty() else None   # 有改动才 commit，防空收
        proc = self._git("tag", tag)
        if proc.returncode != 0:
            return {"ok": False, "error": (proc.stderr or "git tag 失败").strip()}
        return {"ok": True, "tag": tag, "committed": committed}

    def is_dirty(self) -> bool:
        """工作区是否有未提交改动（收藏/自动收藏"防空收"判据）。"""
        if not (self.root / ".git").exists():
            return False
        proc = self._git("status", "--porcelain")
        return bool(proc.stdout.strip())

    def head_hash(self) -> str | None:
        """HEAD 短 hash（计数器对齐"上次 git 点"用）。"""
        if not (self.root / ".git").exists():
            return None
        proc = self._git("rev-parse", "--short", "HEAD")
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    def favorites(self) -> list[dict]:
        """收藏列表（fav/* tag + 指向 commit 的缩略信息；新→旧）。"""
        if not (self.root / ".git").exists():
            return []
        proc = self._git("tag", "--list", "fav/*")
        tags = [t.strip() for t in (proc.stdout or "").splitlines() if t.strip()]
        out: list[dict] = []
        for t in sorted(tags, reverse=True):
            info = self._git("log", "-1", "--format=%h %s", t)
            out.append({"tag": t, "info": (info.stdout or "").strip()})
        return out

    def summary(self, cap: int = SUMMARY_NOTE_CAP) -> str:
        score = self.score
        lines = [
            f"project={self.name} title={score.title or '(无标题)'} tempo={score.tempo} "
            f"keys={[k.key for k in score.key_candidates]} tracks={len(score.tracks)}"
        ]
        for ti, track in enumerate(score.tracks):
            lines.append(f"track[{ti}] name={track.name!r} program={track.instrument.program!r} "
                         f"volume={track.instrument.volume} notes={len(track.notes)}")
            for i, n in enumerate(track.notes):
                if i >= cap:
                    lines.append(f"  …共 {len(track.notes)} 音，其余省略")
                    break
                lines.append(
                    f"  [{i}] {midi_to_note_name(n.pitch_midi)}({int(n.pitch_midi)}) "
                    f"start={n.start:.3f} end={n.end:.3f} vel={n.velocity:.2f}"
                )
        return "\n".join(lines)
