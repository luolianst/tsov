"""agent 会话线程（F5 自 tsov/web.py 拆出；行为逐行保留）。

- _stream_chat：流式 LLM 调用（OpenAI 兼容 SSE；思考/正文增量推 agent_delta）
  批B P20/P23：静默心跳 agent_wait / 工具参数进度 tool_args / 中途失败重试 agent_retry（一次）/
  读超时 90s / 终止轮次落痕 / 时延日志（output/agent-sessions/_stream-timing.jsonl）
- _run_agent_session：一次对话框会话（后台线程；编辑结果采用进工程）
- _retention_tick：轮末留存钩子（ADR-0019 / M-V7 D2）

运行期可调点经 `config` 动态读取；测试打桩点 = 本模块 `_stream_chat`（旧 tsov.web 打桩点作废）。
"""

from __future__ import annotations

import datetime
import json
import threading
import time
from pathlib import Path

from ..core.score import Score
from ..host.state import ProjectState
from ..web_actions import (READ_TOOLS, SCORE_WRITING_TOOLS, ActionJournal,
                           impact_of, summarize_args, tool_label, tool_stats)
from . import config
from .helpers import project_state
from .state import WebState


class _StopRequested(Exception):
    """用户请求停止（agent_stop 置位时流式读取中断抛出）。"""


def _read_json_safe(path: Path) -> dict | None:
    """读 JSON（批B：动作快照用）；文件不存在/损坏 → None（快照失败不阻塞 agent 会话）。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _load_session_messages(jsonl_path: Path) -> list[dict]:
    """读会话 JSONL → wire 消息列表（role/content/tool_calls/tool_call_id）。"""
    msgs: list[dict] = []
    if jsonl_path.is_file():
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            msgs.append({k: entry[k] for k in ("role", "content", "tool_calls", "tool_call_id") if k in entry})
    return msgs


# ---------------------------------------------------------------------------
# 批B（P20/P23）：流式调用治理——心跳 / 参数进度 / 超时 / 重试 / 时延日志
# ---------------------------------------------------------------------------

_READ_TIMEOUT_S = 90.0     # P23：读超时（原 300s；心跳 15s 可见化 + 重试一次兜底）
_WAIT_HEARTBEAT_S = 15.0   # P20：静默心跳阈值（agent_wait 事件）
_TOOL_ARGS_STEP = 400      # P20：工具参数进度步长（字）


class _StreamMidFail(Exception):
    """流中途失败（已有增量）——可重试一次（P23）。"""

    def __init__(self, err: Exception):
        super().__init__(str(err))
        self.err = err


class _StreamTicker:
    """P20：流等待心跳——静默 ≥interval 推 agent_wait（每 interval 一条；增量 touch 复位）。"""

    def __init__(self, bus, project: str, session_id: str, interval: float | None = None):
        self.bus = bus
        self.project = project
        self.session_id = session_id
        self.interval = float(interval or _WAIT_HEARTBEAT_S)
        self.phase = "waiting"          # waiting / streaming（首增量后翻转）
        self._last = time.monotonic()
        self._stop = threading.Event()
        self._th = threading.Thread(target=self._run, daemon=True, name="tsov-stream-heartbeat")

    def start(self) -> None:
        self._th.start()

    def touch(self) -> None:
        """收到增量——静默计时复位。"""
        self._last = time.monotonic()

    def _run(self) -> None:
        while not self._stop.is_set():
            wait_s = max(0.05, min(1.0, self.interval / 3.0))
            if self._stop.wait(wait_s):
                return
            silent = time.monotonic() - self._last
            if silent >= self.interval:
                try:
                    self.bus.publish(self.project, "agent_wait",
                                     {"session_id": self.session_id,
                                      "silent_s": int(round(silent)), "phase": self.phase})
                except Exception:  # noqa: BLE001 心跳失败静默
                    pass
                self._last = time.monotonic()

    def stop(self) -> None:
        self._stop.set()


def _append_stream_timing(session_id: str, timing: dict, t0: float) -> None:
    """P23 层③：时延日志（首字节/最大静默/尝试数/错误）——攒数据再定是否绕代理。失败静默。"""
    try:
        path = Path(config.AGENT_SESSION_DIR) / "_stream-timing.jsonl"
        row = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "session_id": session_id,
            "attempts": timing.get("attempts"),
            "first_delta_s": timing.get("first_delta_s"),
            "max_gap_s": timing.get("max_gap_s"),
            "total_s": round(time.monotonic() - t0, 2),
            "deltas": timing.get("deltas"),
            "error": timing.get("error"),
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 日志失败不阻塞
        pass


def _stream_once(bus, project: str, session_id: str, messages: list[dict],
                 tools: list[dict] | None, stop_event: threading.Event,
                 ticker: "_StreamTicker", timing: dict) -> dict:
    """单次流式尝试（自原 _stream_chat 主体迁出；语义逐行保留 + P20 增量治理）。"""
    import requests

    from ..agent.llm import _json_decision
    from ..llm_client import resolve_api_key, resolve_endpoint, resolve_model  # F4：常量迁 llm_client；E2：动态解析（设置档）

    api_key = resolve_api_key()
    if not api_key:
        raise RuntimeError("缺少 LLM key（TSOV_LLM_API_KEY / DEEPSEEK_API_KEY，agent 会话无法启动）——可在 WebUI ⚙ 设置 → 对话 / LLM 里填写")

    payload = {"model": resolve_model(), "messages": messages, "temperature": 0.2, "stream": True}
    if tools:
        payload["tools"] = tools
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    deltas = 0
    last_ts = time.monotonic()
    t_start = last_ts

    def _mark() -> None:   # P20：统一增量记账（心跳复位 + 时延统计）
        nonlocal deltas, last_ts
        deltas += 1
        timing["deltas"] = (timing.get("deltas") or 0) + 1
        now = time.monotonic()
        if timing.get("first_delta_s") is None:
            timing["first_delta_s"] = round(now - t_start, 2)
        gap = now - last_ts
        if gap > (timing.get("max_gap_s") or 0.0):
            timing["max_gap_s"] = round(gap, 2)
        last_ts = now
        ticker.touch()

    try:
        resp = requests.post(resolve_endpoint(), json=payload, headers=headers,
                             timeout=(10, _READ_TIMEOUT_S), stream=True)
        resp.raise_for_status()
        content_acc: list[str] = []
        tool_acc: dict[int, dict] = {}
        for raw in resp.iter_lines(decode_unicode=True):
            if stop_event.is_set():
                raise _StopRequested()
            if not raw or not raw.startswith("data:"):
                continue
            data = raw[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            rc = delta.get("reasoning_content")
            if rc:
                _mark()
                ticker.phase = "streaming"
                bus.publish(project, "agent_delta", {"session_id": session_id, "kind": "thinking", "text": rc})
            piece = delta.get("content")
            if piece:
                _mark()
                ticker.phase = "streaming"
                content_acc.append(piece)
                bus.publish(project, "agent_delta", {"session_id": session_id, "kind": "content", "text": piece})
            for tcd in delta.get("tool_calls") or []:
                idx = int(tcd.get("index", 0))
                slot = tool_acc.setdefault(idx, {"id": "", "name": "", "args": "", "pub_len": 0, "pub_ts": 0.0})
                if tcd.get("id"):
                    slot["id"] = tcd["id"]
                fn = tcd.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]
                    _mark()
                    # P20：工具参数生成进度（节流：+400 字 或 每 1s）
                    now = time.monotonic()
                    n = len(slot["args"])
                    if n - slot["pub_len"] >= _TOOL_ARGS_STEP or (now - slot["pub_ts"] >= 1.0 and n > slot["pub_len"]):
                        slot["pub_len"] = n
                        slot["pub_ts"] = now
                        bus.publish(project, "agent_delta",
                                    {"session_id": session_id, "kind": "tool_args",
                                     "tool": slot["name"] or "?", "chars": n})
    except _StopRequested:
        raise
    except Exception as e:  # noqa: BLE001 连接/协议失败
        if deltas == 0:
            from ..agent.llm import chat as llm_chat

            return llm_chat(messages, tools=tools)   # 零增量 → 非流式回退（既有语义；失败原样抛出）
        raise _StreamMidFail(e)

    content = "".join(content_acc)
    tool_calls: list[dict] = []
    for idx in sorted(tool_acc):
        slot = tool_acc[idx]
        if not slot["name"]:
            continue
        try:
            args = json.loads(slot["args"] or "{}")
        except json.JSONDecodeError:
            args = {}
        tool_calls.append({"id": slot["id"] or f"call-{idx + 1}", "name": slot["name"],
                           "arguments": args if isinstance(args, dict) else {}})

    if not content and not tool_calls:
        from ..agent.llm import chat as llm_chat

        return llm_chat(messages, tools=tools)  # 空流（思考型模型正文可能为空）→ 非流式兜底

    if not tool_calls:
        decision = _json_decision(content)
        if decision.get("tool"):
            name = str(decision["tool"])
            args = decision.get("arguments") if isinstance(decision.get("arguments"), dict) else {}
            tool_calls.append({"id": "json-1", "name": name, "arguments": args})

    wire = [{"id": tc["id"], "type": "function",
             "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}}
            for tc in tool_calls]
    message: dict = {"role": "assistant", "content": content}
    if wire:
        message["tool_calls"] = wire
    return {"content": content, "tool_calls": tool_calls, "message": message}


def _stream_chat(bus, project: str, session_id: str, messages: list[dict],
                 tools: list[dict] | None, stop_event: threading.Event) -> dict:
    """流式 LLM 调用（批B P20/P23 治理版）。

    - 心跳：静默 ≥15s 推 agent_wait；工具参数生成每 +400 字/1s 推 agent_delta(tool_args)。
    - 重试：流中途失败（已有增量）→ 推 agent_retry（前端重置流式卡）→ 重发一次；再失败抛错。
    - deltas==0 的失败沿用非流式回退（既有语义，不占重试额度）。
    - 每次调用结束（含异常）写一行时延日志（output/agent-sessions/_stream-timing.jsonl）。
    """
    ticker = _StreamTicker(bus, project, session_id)
    ticker.start()
    t0 = time.monotonic()
    timing: dict = {"attempts": 0, "first_delta_s": None, "max_gap_s": 0.0, "deltas": 0, "error": None}
    try:
        for attempt in (1, 2):
            timing["attempts"] = attempt
            try:
                return _stream_once(bus, project, session_id, messages, tools, stop_event, ticker, timing)
            except _StopRequested:
                timing["error"] = "stopped"
                raise
            except _StreamMidFail as f:
                if attempt == 1:
                    bus.publish(project, "agent_retry",
                                {"session_id": session_id, "reason": f"{type(f.err).__name__}: {f.err}"})
                    ticker.touch()
                    continue
                timing["error"] = f"{type(f.err).__name__}: {f.err}"
                raise RuntimeError(f"流式 LLM 中途失败：{type(f.err).__name__}: {f.err}") from f.err
            except Exception as e:  # noqa: BLE001 其余（回退失败 / key 缺失等）原样抛出（记 timing）
                timing["error"] = f"{type(e).__name__}: {e}"
                raise
        raise RuntimeError("流式 LLM 调用未产生结果")   # 理论不可达
    finally:
        ticker.stop()
        _append_stream_timing(session_id, timing, t0)


def _retention_tick(state: WebState, project_name: str, *, agent_turns: int = 0) -> None:
    """留存钩子（ADR-0019 / M-V7 D2）：迭代计数 → 阈值自动收藏；定时档惰性检查。

    - 调用点：agent 轮末（finally，正常/停止/异常都过）+ 用户写操作（/batch 等）。
    - 计数口径：自上次 git 点以来的 agent 迭代轮数（手势不计入）；HEAD 变化 → 自动归零。
    - 自动收藏：计数 ≥ 阈值且有改动 → commit + `fav/<ts>-auto`；无改动保留计数（防空收）。
    - 定时档：默认关；开启后（惰性检查）距上次检查 ≥ 间隔且有改动 → `fav/<ts>-time`。
    - 失败静默（由调用方兜底），本函数内不抛。
    """
    proj = state.get_project(project_name)
    ps = ProjectState(proj.root)
    ps.sync_git_point(proj.head_hash())          # 任何 HEAD 变化（收藏/回滚…）→ 计数归零
    if agent_turns:
        ps.add_turns(int(agent_turns))

    settings = ps.settings()
    threshold = max(1, int(settings.get("auto_favorite_iters", 40)))
    if ps.counter >= threshold and proj.is_dirty():
        iters = ps.counter
        r = proj.favorite(auto=True)
        if r.get("ok"):
            ps.reset_counter()
            ps.data["last_commit"] = proj.head_hash() or ps.last_commit
            state.bus.publish(project_name, "favorite",
                              {"tag": r["tag"], "source": "auto", "iters": iters})

    tf = settings.get("timed_favorite") or {}
    if bool(tf.get("enabled")):
        interval = max(60.0, float(tf.get("interval_min", 30)) * 60.0)
        now = time.time()
        if now - ps.last_timed_check >= interval:
            ps.mark_timed_check(now)
            if proj.is_dirty():
                r = proj.favorite(timed=True)
                if r.get("ok"):
                    ps.reset_counter()
                    ps.data["last_commit"] = proj.head_hash() or ps.last_commit
                    state.bus.publish(project_name, "favorite", {"tag": r["tag"], "source": "time"})
    ps.save()


def _run_agent_session(state: WebState, project_name: str, task: str, session_id: str, base_rev: str = "HEAD",
                       annotations: list[dict] | None = None, selection: dict | None = None,
                       user_actions: list[str] | None = None) -> None:
    """一次对话框 agent 会话（后台线程跑；agent_lock 由调用方获取，本函数 finally 释放）。

    base_rev（议题 ④）：编辑目标版本标识，注入 brief；A 路下始终 = 用户当前工作版本（HEAD），
    仅用于让 LLM 明确「我改的是哪个版本」，不改变工程采纳逻辑。

    M-V3（交互闭环）：annotations = 用户人工标注（确定性先行应用，ADR-0009 最高优先级、不走 LLM），
    失败则拒绝整条消息（谱不变）；selection = 当前选区（注入 brief 供指代）。
    """
    from ..agent import AgentSession, build_default_registry
    from ..agent.prompt import build_system_prompt
    from ..agent.skills import SkillLibrary

    bus = state.bus
    proj = state.get_project(project_name)
    root = proj.root
    edited_path = root / config.EDITED_SCORE_NAME
    journal = proj.journal   # 快照窗口——与工程共享单例（防双实例全量覆盖丢账）
    round_key = f"{session_id}:{int(time.time())}"   # 本对话轮标识（agent 窗口淘汰口径）
    # 清掉上一轮残留——只采用「本轮」的编辑结果
    try:
        edited_path.unlink()
    except FileNotFoundError:
        pass

    # ---- M-V3：人工标注确定性先行（最高优先级；ADR-0009「不走 LLM」）----
    # 失败（非法标注 / 空结果守卫）→ 拒绝整条消息、谱不变；成功 → 落盘 + 快照窗口条目（M-V7 D2：零 commit）
    ann_note = ""
    if annotations:
        try:
            from ..analysis.edit import edit_score as _edit_annotations

            ann_result = _edit_annotations(proj.score, annotations=annotations, llm=False)
            if ann_result.error:
                bus.publish(project_name, "agent_error",
                            {"session_id": session_id, "error": f"人工标注非法（拒绝）：{ann_result.error}"})
                state.agent_lock.release()
                return
            adopted = proj.apply_score(ann_result.new_score, f"人工标注（{len(annotations)} 条）")
            if adopted["ok"]:
                bus.publish(project_name, "diff_applied", {**adopted["diff"], "commit": adopted["commit"], "seq": adopted.get("seq")})
                bus.publish(project_name, "state_updated", project_state(proj))
                ann_note = (
                    f"- 用户人工标注已确定性应用（{adopted['diff']['summary']}，快照 #{adopted.get('seq')}）："
                    + "；".join(ann_result.diff_summary[:6])
                    + "\n  这些是用户精确指定的修改：请在结果中保留、不得回退\n"
                )
        except Exception as e:  # noqa: BLE001 标注应用异常 → 拒绝消息（不泄漏锁）
            bus.publish(project_name, "agent_error",
                        {"session_id": session_id, "error": f"人工标注应用失败：{type(e).__name__}: {e}"})
            state.agent_lock.release()
            return

    score_path = root / "score.json"
    render_path = root / config.RENDER_WAV_NAME
    sel_note = ""
    if selection and selection.get("indices"):
        idxs = "、".join(str(i) for i in list(selection["indices"])[:24])
        sel_note = f"- 用户当前选区：track {selection.get('track', 0)} 音符 [{idxs}]（用户说「这里/这段」时指的就是它）\n"
    ua_note = ""
    if user_actions:
        ua_note = ("- 用户手动操作（自上次对话以来，命令层已落盘；用户说\"我刚改了什么\"时以这些为准）："
                   + "；".join(str(x) for x in list(user_actions)[:10]) + "\n")
    brief = (
        f"{task}\n\n"
        f"【工程上下文】\n"
        f"- 当前工程名：{proj.name}；工程 score 路径：{score_path}\n"
        f"- 编辑目标版本：{base_rev}（工程 git 版本标识；请勿自行 git 回滚/切分支，版本切换由宿主负责）\n"
        f"{ann_note}{sel_note}{ua_note}"
        f"- 改谱：用 edit_score 工具（score_path 用上面的工程 score 路径，feedback 写用户的修改要求），"
        f"工具会把新谱自动落盘为同目录的 {config.EDITED_SCORE_NAME}\n"
        f"- 改完谱自查：用 load_score 读 {config.EDITED_SCORE_NAME}，检查全部音高是否属于目标调式音阶"
        f"（如 D 多利亚 = D E F G A B C）；不纯（含调外音）就再用 edit_score 修一轮，直到纯净\n"
        f"- 自查通过后再用 render_wav 渲染新谱试听（score_path={edited_path}，out_wav={render_path}）\n"
        f"- 完成后用中文一句话汇报改动要点（含调式与音阶名）"
    )

    skills_lib = SkillLibrary()   # skill 机制（M-V4）：目录进系统提示，正文按需 use_skill 加载
    jsonl_path = Path(config.AGENT_SESSION_DIR) / f"{session_id}.jsonl"
    session = AgentSession(task=task, session_dir=config.AGENT_SESSION_DIR, session_id=session_id)
    if jsonl_path.is_file():
        session.messages = _load_session_messages(jsonl_path)  # 续接：同一 JSONL 多轮上下文
    else:
        session.add("system", build_system_prompt(skills_lib))

    # 首轮带工程上下文；后续轮只发用户消息（上下文已在会话历史里）
    if any(m.get("role") == "user" for m in session.messages):
        user_msg = task
    else:
        user_msg = brief
    session.add("user", user_msg)

    tool_count = 0
    turns = 0
    stopped = False
    try:
        registry = build_default_registry(skills=skills_lib)

        answer = ""
        for turn in range(1, config.AGENT_MAX_TURNS + 1):
            turns = turn
            if state.agent_stop.is_set():
                stopped = True
                break
            out = _stream_chat(bus, project_name, session_id, session.api_messages,
                               registry.openai_tools(), state.agent_stop)
            bus.publish(
                project_name,
                "agent_turn",
                {
                    "session_id": session_id,
                    "turn": turn,
                    "content": out["content"] or "",
                    "tool_calls": [{"name": tc["name"], "arguments": tc["arguments"],
                                    "id": tc.get("id")} for tc in out["tool_calls"]],
                },
            )
            if not out["tool_calls"]:
                session.add("assistant", out["content"])
                answer = out["content"]
                break
            session.add("assistant", out["content"], tool_calls=out["message"].get("tool_calls"))
            for tc in out["tool_calls"]:
                if state.agent_stop.is_set():
                    stopped = True
                    break
                tool_name = tc["name"]
                targs = tc["arguments"] if isinstance(tc["arguments"], dict) else {}
                # 批B（ADR-0017）：动作前快照——scratch 不存在 = 轮首，取工程当前谱
                pre_snap = _read_json_safe(edited_path) or proj.score.to_dict()
                pre_hash = journal.snapshot(pre_snap)
                try:
                    observation = registry.get(tool_name).handler(tc["arguments"])
                except Exception as e:  # noqa: BLE001 工具出错也作为观测回喂（与 AgentLoop 同策略）
                    observation = f"工具 {tool_name} 错误: {type(e).__name__}: {e}"
                tool_count += 1
                # 批B：动作后快照 → 影响范围（"改了哪些轨/几个音/哪个参数"）+ 动作日志（可撤销）
                post_snap = _read_json_safe(edited_path)
                post_hash = journal.snapshot(post_snap)
                impact = impact_of(pre_snap, post_snap) if (post_snap and tool_name in SCORE_WRITING_TOOLS) else None
                tstats = tool_stats(tool_name, targs, impact) if tool_name in SCORE_WRITING_TOOLS else {}
                entry = None
                if impact is not None and impact.get("text") and tool_name in SCORE_WRITING_TOOLS:
                    entry = journal.append(source="agent", label=tool_label(tool_name),
                                           session_id=session_id, turn=turn, round=round_key,
                                           tool=tool_name, args=summarize_args(tool_name, targs),
                                           pre=pre_hash, post=post_hash, impact=impact,
                                           stats=tstats)
                bus.publish(
                    project_name,
                    "agent_tool",
                    {
                        "session_id": session_id, "tool": tool_name, "observation": observation,
                        # ---- 动作卡 v2 字段（批B B1-1；旧字段保留 = SSE 兼容）----
                        "tool_call_id": tc.get("id"),
                        "turn": turn,
                        "round": round_key,     # 对话产物流 B 件：轮聚合键
                        "stats": tstats,        # 对话产物流 B 件：结构化计数
                        "action_id": f"{session_id}:{turn}:{tc.get('id') or tool_count}",
                        "label": tool_label(tool_name),
                        "summary": summarize_args(tool_name, targs),
                        "impact": impact,
                        "read_only": tool_name in READ_TOOLS,
                        "seq": entry["seq"] if entry else None,
                        "undoable": bool(entry),
                    },
                )
                session.add("tool", observation, tool_call_id=tc["id"])
        else:
            answer = f"已达最大轮次（{config.AGENT_MAX_TURNS}）未形成最终回答；会话已落盘。"
        if stopped:
            answer = "会话已被用户停止（已落盘的修改保留，未完成的轮次中断）。"
            try:   # 批B P23：终止轮次落痕（append-only；下一轮 LLM 亦可见中断说明）
                session.add("system", f"[轮次终止] 原因=用户停止；已完成轮次={turns}；工具调用={tool_count}；中断轮不产生采用")
            except Exception:  # noqa: BLE001
                pass

        # 采用编辑结果：agent-edited-score.json 存在且与现谱有差异 → 一个 commit + 三色 diff
        adopted: dict | None = None
        if edited_path.is_file():
            try:
                new_score = Score.from_dict(json.loads(edited_path.read_text(encoding="utf-8")))
                result = proj.apply_score(new_score, f"agent：{task[:40]}", source="agent")
                adopted = result if result["ok"] else None
            except Exception as e:  # noqa: BLE001 坏文件不阻塞回答
                bus.publish(
                    project_name,
                    "agent_tool",
                    {"session_id": session_id, "tool": "apply_score",
                     "observation": f"编辑结果采用失败：{type(e).__name__}: {e}"},
                )
            if adopted:
                bus.publish(project_name, "diff_applied", {**adopted["diff"], "commit": adopted["commit"], "seq": adopted.get("seq")})
                bus.publish(project_name, "state_updated", project_state(proj))

        bus.publish(
            project_name,
            "agent_answer",
            {
                "session_id": session_id,
                "content": answer,
                "turns": turns,
                "tool_calls_made": tool_count,
                "adopted": bool(adopted),
                "stopped": stopped,
            },
        )
    except _StopRequested:
        try:   # 批B P23：终止轮次落痕（流式中断）
            session.add("system", f"[轮次终止] 原因=用户停止（流式中断）；已完成轮次={turns}；工具调用={tool_count}；中断轮不产生采用")
        except Exception:  # noqa: BLE001
            pass
        bus.publish(
            project_name,
            "agent_answer",
            {"session_id": session_id, "content": "会话已被用户停止（流式中断）。",
             "turns": turns, "tool_calls_made": tool_count, "adopted": False, "stopped": True},
        )
    except Exception as e:  # noqa: BLE001 LLM/致命错误 → agent_error 事件（锁在 finally 释放）
        try:   # 批B P23：终止轮次落痕（异常）
            session.add("system", f"[轮次终止] 原因={type(e).__name__}: {e}；已完成轮次={turns}；工具调用={tool_count}；中断轮不产生采用")
        except Exception:  # noqa: BLE001
            pass
        bus.publish(project_name, "agent_error", {"session_id": session_id, "error": f"{type(e).__name__}: {e}"})
    finally:
        # M-V7 D2（ADR-0019）：轮末留存钩子——迭代计数 + 阈值自动收藏 + 定时档惰性（失败静默）
        try:
            _retention_tick(state, project_name, agent_turns=turns)
        except Exception:  # noqa: BLE001 留存钩子失败不影响主流程
            pass
        state.agent_lock.release()
