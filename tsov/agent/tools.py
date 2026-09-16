"""tsov 领域工具集（ADR-0012）：agentloop 的工具链 = 现有 CLI 能力的函数化。

工具清单：load_score / edit_score / set_tempo / render_wav / play_score / transcribe / understand / list_dir / use_skill，
外加作曲/工程工具链（tools_compose.py）：voice_to_score / detect_key / create_track / write_notes / duplicate_bars /
set_track_mix / apply_effect / apply_pattern / analyze_levels / export_audio。
每个工具：JSON schema 参数 + 返回文本观测；错误直接抛，由 loop 捕获成观测回喂。
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

from ..analysis.dataset import midi_to_note_name
from .registry import ToolRegistry, ToolSpec
from .skills import SkillLibrary, tool_use_skill

_SUMMARY_NOTE_CAP = 80  # load_score 每个轨最多列出的音符数（防超长观测）


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------


def _score_from_path(score_path: str):
    from ..core.score import Score

    with open(score_path, encoding="utf-8") as f:
        return Score.from_dict(json.load(f))


def tool_load_score(args: dict) -> str:
    """读 Score JSON → 紧凑摘要（标题/调性/各轨音符表）。"""
    path = args["path"]
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    try:
        score = _score_from_path(path)
    except KeyError as e:
        raise ValueError(
            f"不是合法的 Score JSON：缺字段 {e}。Score 需含 title/tempo/key_candidates/tracks/meta；"
            f"如果这是预设/技能/配置文件，请用 read_text 读取原文."
        ) from None
    except json.JSONDecodeError as e:
        raise ValueError(f"不是 JSON 文件（{e}）；文本文件请用 read_text 读取") from None
    lines = [f"score: {score.title or '(无标题)'} tempo={score.tempo} sig={score.time_signature} "
             f"keys={[k.key for k in score.key_candidates]} tracks={len(score.tracks)}"]
    for ti, track in enumerate(score.tracks):
        lines.append(f"track[{ti}] name={track.name!r} program={track.instrument.program!r} "
                     f"volume={track.instrument.volume} notes={len(track.notes)}")
        for i, n in enumerate(track.notes):
            if i >= _SUMMARY_NOTE_CAP:
                lines.append(f"  …共 {len(track.notes)} 音，其余省略")
                break
            lines.append(
                f"  [{i}] {midi_to_note_name(n.pitch_midi)}({int(n.pitch_midi)}) "
                f"start={n.start:.3f} end={n.end:.3f} dur={n.end - n.start:.3f} "
                f"vel={n.velocity:.2f} conf={n.confidence:.2f} dev={n.deviation_cents:.1f}"
            )
    return "\n".join(lines)


def tool_edit_score(args: dict) -> str:
    """对 Score 应用人工标注 + LLM 改谱（复用 tsov.analysis.edit，ADR-0009），落盘新 JSON。"""
    score_path = args["score_path"]
    feedback = str(args.get("feedback", ""))
    annotations = args.get("annotations")  # 可选 [{index,action,value}]（确定性优先，不走 LLM）
    out_path = args.get("output") or str(Path(score_path).parent / "agent-edited-score.json")

    from ..analysis.edit import edit_score as _edit_score

    score = _score_from_path(score_path)
    result = _edit_score(score, feedback=feedback, annotations=annotations, llm=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result.new_score.to_dict(), f, ensure_ascii=False, indent=2)

    lines = [f"已写回：{out_path}"]
    lines += [("  " + d) for d in result.diff_summary] or ["  (无改动)"]
    notes = result.new_score.tracks[0].notes if result.new_score.tracks else []
    lines.append(f"notes={len(notes)} keys={[k.key for k in result.new_score.key_candidates]}")
    if result.error:
        lines.append(f"error: {result.error}")
    return "\n".join(lines)


def tool_set_tempo(args: dict) -> str:
    """设置速度/拍号（M-V6 时间参数）：写回新 score JSON（同 edit_score 约定，Web 端采纳 = 一个 commit）。"""
    score_path = args["score_path"]
    out_path = args.get("output") or str(Path(score_path).parent / "agent-edited-score.json")
    score = _score_from_path(score_path)
    tempo = args.get("tempo")
    sig = args.get("time_signature")
    if tempo is None and sig is None:
        raise ValueError("set_tempo 至少需要 tempo 或 time_signature 之一")

    from ..core.score import parse_time_signature

    changes: list[str] = []
    if tempo is not None:
        t = float(tempo)
        if not (20.0 <= t <= 400.0):
            raise ValueError(f"tempo 越界（20-400 BPM）：{t}")
        score.tempo = round(t, 3)
        changes.append(f"tempo={score.tempo}")
    if sig is not None:
        num, den = parse_time_signature(str(sig))
        if f"{num}/{den}" != str(sig).strip():
            raise ValueError(f"time_signature 非法（形如 6/8）：{sig!r}")
        score.time_signature = f"{num}/{den}"
        changes.append(f"time_signature={score.time_signature}")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(score.to_dict(), f, ensure_ascii=False, indent=2)

    ts_num, ts_den = parse_time_signature(score.time_signature)
    bar_sec = (60.0 / float(score.tempo or 120.0)) * (4.0 * ts_num / ts_den)
    ends = [n.end for tr in score.tracks for n in tr.notes]
    t_end = max(ends) if ends else 0.0
    bars = math.ceil(t_end / bar_sec) if bar_sec > 0 else 0
    return (f"已写回：{out_path}（{', '.join(changes)}；"
            f"每小节≈{bar_sec:.3f}s，现有内容≈{bars} 小节，末尾 {t_end:.2f}s）")


def tool_render_wav(args: dict) -> str:
    """宿主离线渲染：Score JSON → WAV（ADR-0013 HostEngine）。"""
    from ..host import HostEngine

    score_path = args["score_path"]
    out_wav = args.get("out_wav") or "output/agent-render.wav"
    engine = HostEngine()
    wav = engine.render_score(score_path, out_wav)
    return f"已渲染：{wav}（{engine.samplerate}Hz，宿主 HostEngine/RF2 音源）"


def tool_play_score(args: dict) -> str:
    """宿主实时回放 Score（预渲染缓冲 + sounddevice；阻塞到播完）。"""
    from ..host import HostEngine

    HostEngine().play_score(args["score_path"])
    return "宿主回放完毕（sounddevice）"


def tool_transcribe(args: dict) -> str:
    """音频 → Voice（GAME 管成品歌 / RMVPE 管哼唱），落盘 Voice JSON。"""
    from ..dsp import transcribe

    audio = args["audio"]
    backend = str(args.get("backend", "game"))
    out_path = args.get("output") or "output/agent-voice.json"
    voice = transcribe(audio, backend=backend)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(voice.to_dict(), f, ensure_ascii=False, indent=2)
    return (
        f"backend={voice.backend} notes={len(voice.notes)} bpm={voice.bpm:.1f} "
        f"segments={len(voice.segments)} duration={voice.notes[-1].end - voice.notes[0].start:.2f}s "
        f"voice 已写：{out_path}"
    )


def tool_understand(args: dict) -> str:
    """MOSS 参考曲理解（服务 127.0.0.1:8300，M5）。"""
    from ..analysis.moss import understand

    u = understand(args["audio"], url=str(args.get("url", "http://127.0.0.1:8300")))
    if u.get("error"):
        raise RuntimeError(u["error"])
    return f"描述：{u.get('description','')} 标签：{'、'.join(u.get('tags',[]))} 歌词：{(u.get('lyrics') or '')[:100]}"


def tool_list_dir(args: dict) -> str:
    """列出目录（默认 output/）下的 mid/json/wav 产物（供 agent 找素材）。"""
    path = args.get("path", "output")
    root = Path(path)
    if not root.is_dir():
        raise FileNotFoundError(path)
    lines = []
    for p in sorted(root.rglob("*"))[:200]:
        if p.is_file() and p.suffix.lower() in (".mid", ".midi", ".json", ".wav", ".mp3", ".m4a"):
            rel = p.relative_to(root)
            lines.append(f"{rel}  [{p.stat().st_size} bytes]")
    if not lines:
        return f"{path} 下没有 mid/json/wav 产物"
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------


def build_default_registry(skills: SkillLibrary | None = None) -> ToolRegistry:
    """agent 默认工具链（tsov CLI 能力的函数化）+ skill 机制（use_skill）。"""
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="load_score",
            description="读取 tsov 的 Score JSON（如 output/.../stage-04-score.json），返回标题/调性/音符表摘要",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "score json 路径"}},
                "required": ["path"],
            },
            handler=tool_load_score,
        )
    )
    registry.register(
        ToolSpec(
            name="edit_score",
            description="对 Score 应用人工标注和/或 LLM 改谱（自然语言反馈），写出新 score JSON 并返回 diff",
            parameters={
                "type": "object",
                "properties": {
                    "score_path": {"type": "string"},
                    "feedback": {"type": "string", "description": "自然语言修改反馈（如 '改成 D 多利亚调式'）"},
                    "annotations": {"type": "array", "items": {"type": "object"},
                                    "description": "人工标注 [{index,action,value}]，确定性命中，可选"},
                    "output": {"type": "string", "description": "输出路径，缺省同目录 agent-edited-score.json"},
                },
                "required": ["score_path"],
            },
            handler=tool_edit_score,
        )
    )
    registry.register(
        ToolSpec(
            name="set_tempo",
            description="设置工程时间参数：速度（BPM）与拍号（如 6/8）。写回 score JSON（同 edit_score 落盘约定）",
            parameters={
                "type": "object",
                "properties": {
                    "score_path": {"type": "string"},
                    "tempo": {"type": "number", "description": "BPM（20-400）"},
                    "time_signature": {"type": "string", "description": "拍号，形如 6/8 / 3/4（可选）"},
                    "output": {"type": "string", "description": "输出路径，缺省同目录 agent-edited-score.json"},
                },
                "required": ["score_path"],
            },
            handler=tool_set_tempo,
        )
    )
    registry.register(
        ToolSpec(
            name="render_wav",
            description="宿主离线渲染：Score JSON → WAV（HostEngine，SF2 音源）",
            parameters={
                "type": "object",
                "properties": {
                    "score_path": {"type": "string"},
                    "out_wav": {"type": "string", "description": "输出 wav 路径，缺省 output/agent-render.wav"},
                },
                "required": ["score_path"],
            },
            handler=tool_render_wav,
        )
    )
    registry.register(
        ToolSpec(
            name="play_score",
            description="宿主实时回放 Score（预渲染缓冲 + sounddevice，阻塞到播完）",
            parameters={
                "type": "object",
                "properties": {"score_path": {"type": "string"}},
                "required": ["score_path"],
            },
            handler=tool_play_score,
        )
    )
    registry.register(
        ToolSpec(
            name="transcribe",
            description="音频 → Voice（game 管成品歌，rmvpe 管哼唱），落盘 Voice JSON",
            parameters={
                "type": "object",
                "properties": {
                    "audio": {"type": "string"},
                    "backend": {"type": "string", "enum": ["game", "rmvpe"], "default": "game"},
                    "output": {"type": "string"},
                },
                "required": ["audio"],
            },
            handler=tool_transcribe,
        )
    )
    registry.register(
        ToolSpec(
            name="understand",
            description="MOSS 参考曲理解（描述/标签/歌词，服务 127.0.0.1:8300）",
            parameters={
                "type": "object",
                "properties": {"audio": {"type": "string"}, "url": {"type": "string"}},
                "required": ["audio"],
            },
            handler=tool_understand,
        )
    )
    registry.register(
        ToolSpec(
            name="list_dir",
            description="列出目录下 mid/json/wav/mp3 产物（默认 output/），便于定位素材与产物",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "default": "output"}},
                "required": [],
            },
            handler=tool_list_dir,
        )
    )
    skills = skills or SkillLibrary()
    registry.register(
        ToolSpec(
            name="use_skill",
            description="加载技能全文（渐进披露：系统提示的「可用技能」目录里选一个，调用后按其中步骤执行）",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "技能名（见系统提示技能目录）"}},
                "required": ["name"],
            },
            handler=tool_use_skill(skills),
        )
    )
    # M-V6 双任务工具链：作曲/工程工具（voice_to_score / create_track / write_notes / …）
    from .tools_compose import register_compose_tools

    register_compose_tools(registry)
    return registry
