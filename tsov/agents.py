"""工程上下文骨架 agents.md（M-V8 E4 段1 · Q1.5 + Q15 双层）。

- 工程级：`<工程根>/agents.md`——状态摘要（本模块**确定性生成**，TSOV-STATE 自动块）
  + 风格意图（手写）+ 历史与决策（段2 接线）+ 偏好占位（指针 → 全局）
- 全局级：`<仓库根>/agents-user.md`——跨工程偏好（建档；段2 接统计提炼）
- 原则：**写不可推导的，指向可推导的**；装配拼接喂三处（对话/调参/配器），冲突工程覆盖全局。

工程文件为工程伴生元数据（同 chain.json / .tsov-state.json 先例；不碰 score schema）。
写路径 = REST（webapp/routes/agents.py），UI 按钮与外部 agent 同径（ADR-0017）。
"""

from __future__ import annotations

import re
import time
from pathlib import Path

STATE_BEGIN = "<!-- TSOV-STATE:BEGIN -->"
STATE_END = "<!-- TSOV-STATE:END -->"

FILE = "agents.md"
USER_FILE = "agents-user.md"


def user_agents_path() -> Path:
    """全局偏好文件 = 仓库根 agents-user.md（tsov 包上一级；与 tsov-settings.json 同住）。"""
    return Path(__file__).resolve().parents[1] / USER_FILE


def _fmt_time(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts if ts is not None else time.time()))


def _key_text(score) -> str:
    """调性显示：优先系统既有 key_candidates；空则现场 detect（确定性，样本 <4 音不出结论）。"""
    cands = list(getattr(score, "key_candidates", []) or [])
    if not cands:
        try:
            from .core.key import detect_key

            notes = [n for t in (getattr(score, "tracks", []) or []) for n in (t.notes or [])]
            cands = detect_key(notes, top=1)
        except Exception:  # noqa: BLE001 —— 摘要生成不得因检测失败而中断
            cands = []
    if not cands:
        return "—"
    c0 = cands[0]
    key = getattr(c0, "key", None) or (c0.get("key") if isinstance(c0, dict) else None)
    conf = getattr(c0, "confidence", None)
    if conf is None and isinstance(c0, dict):
        conf = c0.get("confidence")
    return f"{key}（置信 {conf}）" if key else "—"


def build_state_block(score, *, name: str, ts: float | None = None) -> str:
    """确定性状态摘要块：同 score → 正文稳定（仅生成时间行变化）。"""
    tracks = list(getattr(score, "tracks", []) or [])
    total_notes = sum(len(t.notes or []) for t in tracks)
    ends = [n.end for t in tracks for n in (t.notes or [])]
    dur = round(max(ends), 1) if ends else 0.0
    tempo = float(getattr(score, "tempo", 120) or 120)
    sig = str(getattr(score, "time_signature", "") or "4/4")
    lines = [
        f"### 状态摘要（自动生成 · {_fmt_time(ts)}）",
        f"- 调性：{_key_text(score)} ｜ 速度：{tempo:g} BPM ｜ 拍号：{sig} ｜ 末音至 {dur:g}s",
        f"- 规模：{len(tracks)} 轨 · {total_notes} 音符",
        "- 编制：",
    ]
    for i, t in enumerate(tracks):
        nm = str(getattr(t, "name", "") or f"轨道 {i + 1}")
        suffix = " · 音频" if str(getattr(t, "kind", "midi") or "midi") == "audio" else ""
        inst = getattr(t, "instrument", None)
        prog = str(getattr(inst, "program", "") or "")
        if prog and prog not in ("0", "piano"):
            suffix += f"（音源 {prog}）"
        lines.append(f"  - {i} {nm}{suffix}")
    return "\n".join(lines)


def _default_skeleton(name: str, block: str) -> str:
    return (
        f"# agents.md — {name} · 工程上下文（人 + LLM 双读）\n\n"
        "> tsov 维护：`TSOV-STATE` 块自动生成（勿手改）；其余区块随时手写/沉淀。\n"
        "> 用户级偏好（跨工程）见：`../../agents-user.md`。\n\n"
        f"{STATE_BEGIN}\n{block}\n{STATE_END}\n\n"
        "## 风格意图\n（手写：想要什么味道 / 参考曲 / 约束——调参与配器决策会读这段）\n\n"
        "## 历史与决策\n（待沉淀：段 2 接线后由 AI 从 journal 提炼关键决策与理由）\n\n"
        "## 偏好（工程特有）\n（工程内约定；跨工程偏好写全局 agents-user.md）\n"
    )


def _user_skeleton() -> str:
    return (
        "# agents-user.md — 用户级偏好（跨工程 · tsov）\n\n"
        "> 人 + LLM 双读的跨工程偏好集：手写为主；「统计」块段 2 接线后由 tsov 从 journal 提炼。\n"
        "> 使用：tsov 在对话 / 调参 / 配器时读本文件 + 各工程 `agents.md`；冲突时**工程覆盖全局**。\n\n"
        "## 偏好\n（手写：混音口味 / 配器口味 / 评审偏好——如「贝斯别太高」「副歌要更炸」「不接受自动应用」）\n\n"
        "## 统计\n（待沉淀：段 2 接线后由 AI 从前述 journal 事件提炼：采纳率 / 常用强度档 / 常改参数）\n"
    )


def sync_project_agents(proj_root, score, *, name: str | None = None, ts: float | None = None) -> dict:
    """写/更新工程 agents.md 的状态块；保留其余内容（含手写区）。

    - 文件不在 → 建全套骨架（状态块 + 风格意图 + 历史与决策 + 偏好占位）
    - 文件在且含标记 → 只换标记区间
    - 文件在但无标记（用户自建）→ 自动块插在首个标题行之后
    """
    root = Path(proj_root)
    path = root / FILE
    nm = str(name or root.name)
    block = build_state_block(score, name=nm, ts=ts)
    created = not path.is_file()
    if created:
        text = _default_skeleton(nm, block)
    else:
        text = path.read_text(encoding="utf-8")
        if STATE_BEGIN in text and STATE_END in text:
            pre, _, rest = text.partition(STATE_BEGIN)
            _, _, post = rest.partition(STATE_END)
            text = f"{pre}{STATE_BEGIN}\n{block}\n{STATE_END}{post}"
        else:
            lines = text.split("\n")
            at = 1 if lines and lines[0].startswith("#") else 0
            lines.insert(at, f"\n{STATE_BEGIN}\n{block}\n{STATE_END}\n")
            text = "\n".join(lines)
    path.write_text(text, encoding="utf-8")
    return {"path": str(path), "file": FILE, "created": created, "updated": not created}


def ensure_user_agents(*, path=None) -> dict:
    """全局 agents-user.md 建档（幂等）。"""
    p = Path(path) if path else user_agents_path()
    created = not p.is_file()
    if created:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_user_skeleton(), encoding="utf-8")
    return {"path": str(p), "file": USER_FILE, "created": created}


# ---------------------------------------------------------------------------
# 段2 接线：历史与决策（AI 提炼入口）+ 全局统计块（确定性累计）
# ---------------------------------------------------------------------------

HISTORY_HEADING = "## 历史与决策"
HISTORY_PLACEHOLDER = "（待沉淀：段 2 接线后由 AI 从 journal 提炼关键决策与理由）"
STATS_PLACEHOLDER = "（待沉淀：段 2 接线后由 AI 从前述 journal 事件提炼：采纳率 / 常用强度档 / 常改参数）"
STATS_BEGIN = "<!-- TSOV-STATS:BEGIN -->"
STATS_END = "<!-- TSOV-STATS:END -->"

_STATS_RE = re.compile(r"累计采纳建议：(\d+) 条（电平 (\d+) / 声像 (\d+) / 效果 (\d+)）")


def append_history(proj_root, entry: str, *, ts: float | None = None) -> dict:
    """在工程 agents.md「## 历史与决策」区追加一条（首条替换占位行；无区块 → 建区块）。

    文件不存在 → ValueError（调用方先 sync_project_agents）。
    """
    path = Path(proj_root) / FILE
    if not path.is_file():
        raise ValueError("工程 agents.md 不存在（先 sync）")
    line = str(entry or "").strip().replace("\n", " ")
    if not line:
        raise ValueError("历史条目为空")
    block = f"- {line}"
    text = path.read_text(encoding="utf-8")
    if HISTORY_PLACEHOLDER in text:
        text = text.replace(HISTORY_PLACEHOLDER, block)
    else:
        idx = text.find(HISTORY_HEADING)
        if idx == -1:
            text = text.rstrip("\n") + f"\n\n{HISTORY_HEADING}\n{block}\n"
        else:
            eol = text.find("\n", idx)
            text = text + "\n" + block + "\n" if eol == -1 else (
                text[: eol + 1] + "\n" + block + "\n" + text[eol + 1:])
    path.write_text(text, encoding="utf-8")
    return {"path": str(path), "appended": True}


def update_user_stats(*, delta: dict | None = None, path=None, ts: float | None = None) -> dict:
    """更新全局 agents-user.md「统计」块（确定性累计）：累计采纳建议数（按类型）。

    delta = {"level": n, "pan": n, "effect": n}（增量）；块不存在 → 用占位行位置建块。
    返回累计数 {path, total, level, pan, effect, last}。
    """
    p = Path(path) if path else user_agents_path()
    if not p.is_file():
        ensure_user_agents(path=p)
    text = p.read_text(encoding="utf-8")
    d = {k: int((delta or {}).get(k) or 0) for k in ("level", "pan", "effect")}
    total = lv = pn = ef = 0
    m = _STATS_RE.search(text)
    if m:
        total, lv, pn, ef = (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
    total += sum(d.values())
    lv += d["level"]
    pn += d["pan"]
    ef += d["effect"]
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts if ts is not None else time.time()))
    block = (f"{STATS_BEGIN}\n"
             f"- 累计采纳建议：{total} 条（电平 {lv} / 声像 {pn} / 效果 {ef}）｜最近：{stamp}\n"
             f"{STATS_END}")
    if STATS_BEGIN in text and STATS_END in text:
        pre, _, rest = text.partition(STATS_BEGIN)
        _, _, post = rest.partition(STATS_END)
        text = f"{pre}{block}{post}"
    elif STATS_PLACEHOLDER in text:
        text = text.replace(STATS_PLACEHOLDER, block)
    else:
        text = text.rstrip("\n") + f"\n{block}\n"
    p.write_text(text, encoding="utf-8")
    return {"path": str(p), "total": total, "level": lv, "pan": pn, "effect": ef, "last": stamp}
