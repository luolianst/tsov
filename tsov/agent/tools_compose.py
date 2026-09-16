"""tsov 作曲/工程工具集（M-V6：双任务工具链）——

voice_to_score / detect_key / create_track / write_notes / duplicate_bars /
set_track_mix / apply_effect / apply_pattern / analyze_levels / export_audio

约定：
- 全部沿用 edit_score 的落盘约定：默认写 `<score 同目录>/agent-edited-score.json`
  （Web 端会话结束采纳 = 一个 commit；CLI 场景自行接续）
- 写谱坐标：bar（1 起）+ grid（16 分格序号，1 起；6/8 每小节 12 格）+ len（16 分格数）
  ——按 tempo/拍号换算秒，避免 LLM 手算秒数
- 音高可用 MIDI 号（pitch_midi）或音名（note: "E4"/"f#3"/"Bb2"，C4=60）
- 错误直接抛（ValueError/RuntimeError），由 agent 循环捕获成观测回喂
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from ..core.notes import Note, Voice
from ..core.score import Instrument, Score, Track, parse_time_signature
from ..dsp.pitch import midi_to_hz
from .registry import ToolRegistry, ToolSpec

_NOTE_BASE = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _load(score_path: str) -> Score:
    with open(score_path, encoding="utf-8") as f:
        return Score.from_dict(json.load(f))


def _save(score: Score, out_path: str) -> str:
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(score.to_dict(), f, ensure_ascii=False, indent=2)
    return out_path


def _out_path(args: dict, score_path: str) -> str:
    return args.get("output") or str(Path(score_path).parent / "agent-edited-score.json")


def _bar_grid_sec(score: Score) -> tuple[float, int, float]:
    """(每小节秒数, 每小节16分格数, 每格秒数)——按 tempo/拍号。"""
    num, den = parse_time_signature(score.time_signature)
    bar_sec = (60.0 / float(score.tempo or 120.0)) * (4.0 * num / den)
    gpb = max(1, int(round(num * 16 / den)))
    return bar_sec, gpb, bar_sec / gpb


def _resolve_track(score: Score, track) -> int:
    if track is None:
        return 0
    s = str(track).strip()
    if isinstance(track, int) or s.lstrip("+-").isdigit():
        idx = int(s)
        if 0 <= idx < len(score.tracks):
            return idx
        raise ValueError(f"track 越界：{track!r}（共 {len(score.tracks)} 条）")
    for i, tr in enumerate(score.tracks):
        if tr.name == s:
            return i
    raise ValueError(f"找不到轨道：{track!r}（现有：{[t.name for t in score.tracks]}）")


def note_name_to_midi(name: str) -> int | None:
    """'E4' / 'f#3' / 'Bb2' → MIDI 号（C4=60）；解析失败 None。"""
    s = str(name).strip().lower().replace("♯", "#").replace("♭", "b")
    m = re.fullmatch(r"([a-g])([#b]?)(-?\d+)", s)
    if not m:
        return None
    semi = _NOTE_BASE[m.group(1)] + (1 if m.group(2) == "#" else -1 if m.group(2) == "b" else 0)
    return (int(m.group(3)) + 1) * 12 + semi


def _pitch_of(spec: dict) -> int:
    if "pitch_midi" in spec:
        pm = int(spec["pitch_midi"])
    elif "note" in spec:
        pm = note_name_to_midi(spec["note"])
        if pm is None:
            raise ValueError(f"无法解析音名：{spec.get('note')!r}（形如 E4 / f#3 / Bb2）")
    else:
        raise ValueError("音符需要 pitch_midi 或 note 之一")
    if not (0 <= pm <= 127):
        raise ValueError(f"pitch 越界：{pm}")
    return pm


def _make_note(pm: int, start: float, end: float, vel: float) -> Note:
    return Note(start=round(start, 6), end=round(end, 6), pitch_midi=pm, pitch_hz=midi_to_hz(pm),
                velocity=round(max(0.0, min(1.0, float(vel))), 3), confidence=0.9)


def _track_line(score: Score) -> str:
    return "；".join(f"[{i}]{t.name}({t.instrument.program or 'piano'},{len(t.notes)}音)" for i, t in enumerate(score.tracks))


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------


def tool_voice_to_score(args: dict) -> str:
    """Voice JSON（transcribe 产物）→ Score JSON（单轨 piano，tempo 取 Voice.bpm）。"""
    vp = Path(args["voice_path"])
    if not vp.is_file():
        raise FileNotFoundError(str(vp))
    voice = Voice.from_dict(json.loads(vp.read_text(encoding="utf-8")))
    if not voice.notes:
        raise ValueError(f"Voice 里没有音符：{vp}")
    track = Track(name="melody", instrument=Instrument(backend="fluidsynth", program="piano", volume=0.8),
                  notes=voice.notes)
    score = Score(title=str(args.get("title") or vp.stem), tempo=float(voice.bpm or 120.0),
                  key_candidates=[], tracks=[track], meta={"source": "voice_to_score", "voice": str(vp)})
    out = _save(score, _out_path(args, str(vp)))
    t_end = max(n.end for n in voice.notes)
    return (f"已写回：{out}（单轨 piano；tempo={score.tempo:.1f} notes={len(voice.notes)} 时长 {t_end:.2f}s；"
            f"后续以本文件继续编辑，会话结束 Web 端会采纳）")


def tool_detect_key(args: dict) -> str:
    """重算调性候选（scale-fit；基于音高分布 + 主音权重）。"""
    from ..analysis.key import detect_key

    score = _load(args["score_path"])
    ti = _resolve_track(score, args.get("track"))
    notes = score.tracks[ti].notes
    cands = detect_key(notes)
    if not cands:
        return f"track[{ti}] {score.tracks[ti].name}：音符不足（{len(notes)} 音），无法判定调性"
    return f"track[{ti}] {score.tracks[ti].name}（{len(notes)} 音）调性候选：" + \
        "、".join(f"{c.key}({c.confidence:.2f})" for c in cands)


def tool_create_track(args: dict) -> str:
    """新增音轨（GM 音色名限定，未知名直接报错——防静默回落钢琴）。"""
    from ..midi.export import GM_PROGRAMS

    score = _load(args["score_path"])
    program = str(args.get("program") or "piano").strip().lower()
    if program not in GM_PROGRAMS:
        raise ValueError(f"未知音色名 {program!r}；可用：{sorted(GM_PROGRAMS)}")
    name = str(args.get("name") or program)
    volume = float(args.get("volume", 0.8))
    score.tracks.append(Track(name=name, instrument=Instrument(backend="fluidsynth", program=program, volume=volume),
                              notes=[]))
    out = _save(score, _out_path(args, args["score_path"]))
    return f"已新增 track[{len(score.tracks) - 1}] name={name!r} program={program}；现有：{_track_line(score)} ｜ 写回：{out}"


def tool_write_notes(args: dict) -> str:
    """按「小节+16分格」写音符（bar/grid/len → 秒），mode=append|replace。"""
    score = _load(args["score_path"])
    ti = _resolve_track(score, args.get("track", 0))
    notes_in = args.get("notes")
    if not isinstance(notes_in, list) or not notes_in:
        raise ValueError("notes 不能为空数组")
    mode = str(args.get("mode", "append")).lower()
    if mode not in ("append", "replace"):
        raise ValueError(f"mode 只支持 append/replace：{mode!r}")
    bar_sec, gpb, grid_sec = _bar_grid_sec(score)
    tr = score.tracks[ti]
    if mode == "replace":
        tr.notes = []
    added = 0
    bar_lo, bar_hi = None, None
    for i, spec in enumerate(notes_in):
        try:
            bar = int(spec["bar"]); grid = int(spec["grid"]); ln = int(spec.get("len", 2))
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"第 {i} 个音符缺 bar/grid（或格式错）：{spec!r}") from None
        if bar < 1:
            raise ValueError(f"第 {i} 音 bar 需 ≥1：{bar}")
        if not (1 <= grid <= gpb):
            raise ValueError(f"第 {i} 音 grid 需在 1..{gpb}（本工程每小节 {gpb} 个 16 分格）：{grid}")
        if ln < 1 or grid - 1 + ln > gpb:
            raise ValueError(f"第 {i} 音 len 越界（grid {grid} + len {ln} > {gpb}）：{spec!r}")
        pm = _pitch_of(spec)
        start = (bar - 1) * bar_sec + (grid - 1) * grid_sec
        tr.notes.append(_make_note(pm, start, start + ln * grid_sec, spec.get("velocity", 0.8)))
        added += 1
        bar_lo = bar if bar_lo is None else min(bar_lo, bar)
        bar_hi = bar if bar_hi is None else max(bar_hi, bar)
    tr.notes.sort(key=lambda n: (n.start, n.pitch_midi))
    out = _save(score, _out_path(args, args["score_path"]))
    return (f"已{'重写' if mode == 'replace' else '追加'} {added} 音 → track[{ti}] {tr.name}；"
            f"覆盖小节 {bar_lo}-{bar_hi}（{gpb} 格/小节）；该轨共 {len(tr.notes)} 音 ｜ 写回：{out}")


def tool_duplicate_bars(args: dict) -> str:
    """整小节复制（段落复制填充）；track 省略 = 全轨复制（复刻段落）。"""
    score = _load(args["score_path"])
    src0 = int(args["src_start_bar"]); src1 = int(args["src_end_bar"]); dest = int(args["dest_start_bar"])
    if not (1 <= src0 <= src1) or dest < 1:
        raise ValueError("小节范围非法（需 1 ≤ src_start ≤ src_end，dest ≥ 1）")
    bar_sec, _, _ = _bar_grid_sec(score)
    s0, s1 = (src0 - 1) * bar_sec, src1 * bar_sec          # [s0, s1) = 源小节区间
    shift = (dest - 1) * bar_sec - s0
    targets = [_resolve_track(score, args["track"])] if args.get("track") is not None else range(len(score.tracks))
    copied = 0
    for ti in targets:
        tr = score.tracks[ti]
        picked = [n for n in tr.notes if s0 - 1e-6 <= n.start < s1 - 1e-6]
        for n in picked:
            tr.notes.append(Note(start=round(n.start + shift, 6), end=round(n.end + shift, 6),
                                 pitch_midi=n.pitch_midi, pitch_hz=n.pitch_hz, velocity=n.velocity,
                                 confidence=n.confidence, deviation_cents=n.deviation_cents))
            copied += 1
        tr.notes.sort(key=lambda n: (n.start, n.pitch_midi))
    out = _save(score, _out_path(args, args["score_path"]))
    if args.get("track") is None:
        return f"已全轨复制 小节 {src0}-{src1} → {dest}（共 {copied} 音）｜ 写回：{out}"
    return (f"已复制 track[{targets[0]}] {score.tracks[targets[0]].name} 小节 {src0}-{src1} → {dest}"
            f"（{copied} 音）｜ 写回：{out}")


def tool_set_track_mix(args: dict) -> str:
    """调轨音量（Instrument.volume 0-2）与声像（pan -1..1，负左正右）。"""
    score = _load(args["score_path"])
    ti = _resolve_track(score, args.get("track", 0))
    tr = score.tracks[ti]
    parts = []
    if args.get("volume") is not None:
        v = float(args["volume"])
        if not (0.0 <= v <= 2.0):
            raise ValueError(f"volume 越界（0-2）：{v}")
        parts.append(f"volume {tr.instrument.volume}→{v}")
        tr.instrument.volume = v
    if args.get("pan") is not None:
        p = float(args["pan"])
        if not (-1.0 <= p <= 1.0):
            raise ValueError(f"pan 越界（-1..1）：{p}")
        parts.append(f"pan {getattr(tr, 'pan', 0)}→{p}")
        tr.pan = p
    if not parts:
        raise ValueError("至少给 volume 或 pan 之一")
    out = _save(score, _out_path(args, args["score_path"]))
    return f"track[{ti}] {tr.name}：{'、'.join(parts)} ｜ 写回：{out}"


def tool_apply_effect(args: dict) -> str:
    """给轨应用效果预设（混响等；同一轨整体替换为该预设链）。track 必填（防误落到 track[0]）。"""
    from ..presets import apply_effect_preset, load_library

    score = _load(args["score_path"])
    if args.get("track") is None:
        raise ValueError("track 必填（轨索引或轨名）——效果钉在哪条轨上必须明确；"
                         "总线（master）效果暂不支持")
    ti = _resolve_track(score, args.get("track"))
    preset = str(args["preset"])
    lib = load_library()
    try:
        new_score = apply_effect_preset(score, ti, preset, library=lib)
    except KeyError:
        raise ValueError(f"未知效果预设 {preset!r}；可用：{lib.list_names('effects')}") from None
    out = _save(new_score, _out_path(args, args["score_path"]))
    chain = " → ".join(e.type for e in new_score.tracks[ti].instrument.effects) or "（空）"
    return f"track[{ti}] {new_score.tracks[ti].name} 已应用效果链：{chain} ｜ 写回：{out}"


# 6/8 高速术力口鼓型模板（源自 presets/arrangements/wotaiko-fast-6-8.json 的经验格位；
# 16 分格制 1..12：八分格 n = 16分格 2n-1 / 2n）
_DRUM_PATTERNS: dict[str, list[tuple[int, int, int, float]]] = {
    # (grid, len, pitch, velocity)  pitch: 36 kick / 38 snare / 39 clap / 42 closed hat / 46 open hat / 51 ride / 53 bell / 49 crash
    "wotaiko_drums_base": [
        (1, 2, 36, 0.95), (5, 2, 36, 0.90), (7, 2, 36, 0.95), (11, 2, 36, 0.90),
        (7, 2, 38, 0.90),
        (1, 2, 42, 0.70), (3, 2, 42, 0.62), (7, 2, 42, 0.70), (9, 2, 42, 0.62),
        (5, 2, 46, 0.75), (11, 2, 46, 0.75),
    ],
    "wotaiko_drums_energy": [
        (1, 2, 36, 0.98), (5, 2, 36, 0.92), (7, 2, 36, 0.98), (11, 2, 36, 0.92), (12, 1, 36, 0.85),
        (7, 2, 38, 0.95), (7, 2, 39, 0.78),
        (1, 2, 51, 0.74), (3, 2, 51, 0.66), (5, 2, 51, 0.70), (7, 2, 51, 0.74), (9, 2, 51, 0.66), (11, 2, 51, 0.70),
        (7, 1, 53, 0.80),
    ],
}


def tool_apply_pattern(args: dict) -> str:
    """往鼓轨按内置 6/8 高速鼓型模板落 N 小节（base=主歌 / energy=副歌）。"""
    score = _load(args["score_path"])
    ti = _resolve_track(score, args.get("track", 0))
    pattern = str(args.get("pattern", "wotaiko_drums_base"))
    if pattern not in _DRUM_PATTERNS:
        raise ValueError(f"未知 pattern {pattern!r}；可用：{sorted(_DRUM_PATTERNS)}")
    start_bar = int(args.get("start_bar", 1)); bars = int(args.get("bars", 1))
    if start_bar < 1 or bars < 1:
        raise ValueError("start_bar / bars 需 ≥1")
    crash = bool(args.get("crash", False))
    bar_sec, gpb, grid_sec = _bar_grid_sec(score)
    if gpb != 12:
        raise ValueError(f"本模板面向 6/8（12 格/小节）；当前工程为 {score.time_signature!r}（{gpb} 格）")
    tr = score.tracks[ti]
    tpl = list(_DRUM_PATTERNS[pattern])
    if crash:
        tpl.append((1, 2, 49, 0.9))
    n = 0
    for b in range(start_bar, start_bar + bars):
        for grid, ln, pitch, vel in tpl:
            start = (b - 1) * bar_sec + (grid - 1) * grid_sec
            tr.notes.append(_make_note(pitch, start, start + ln * grid_sec, vel))
            n += 1
    tr.notes.sort(key=lambda x: (x.start, x.pitch_midi))
    out = _save(score, _out_path(args, args["score_path"]))
    return f"已按 {pattern} 落 小节 {start_bar}-{start_bar + bars - 1}（{n} 击）→ track[{ti}] {tr.name} ｜ 写回：{out}"


def tool_analyze_levels(args: dict) -> str:
    """渲染各轨 stem 测 RMS（整段 + 最响窗口）+ 混音峰值——多轨音量平衡的判断依据。"""
    import numpy as np

    from ..host import HostEngine
    from ..host.mix import render_buses

    score = _load(args["score_path"])
    win = float(args.get("window_sec", 4.0))
    engine = HostEngine()
    session = engine.load(score)
    try:
        sr = session.samplerate
        mix = render_buses(session, stereo=False, auto_scale=False)
        mix_peak = float(np.max(np.abs(mix))) if mix.size else 0.0
        rows = []
        for i, tr in enumerate(score.tracks):
            if not tr.notes:
                rows.append((i, tr.name, None, None, 0))
                continue
            buf = render_buses(session, stereo=False, only_track=i, auto_scale=False)
            if buf.size == 0:
                rows.append((i, tr.name, None, None, len(tr.notes)))
                continue
            rms_all = float(np.sqrt(np.mean(buf ** 2)))
            w = max(1, int(win * sr))
            n_win = max(1, len(buf) // w)
            rms_win = max(float(np.sqrt(np.mean(buf[k * w:(k + 1) * w] ** 2))) for k in range(n_win))
            rows.append((i, tr.name, rms_all, rms_win, len(tr.notes)))
    finally:
        session.close()

    def db(x):
        return 20.0 * np.log10(max(x, 1e-9)) if x else -120.0

    loud = [r for r in rows if r[2]]
    ref = max(r[2] for r in loud) if loud else 0.0
    lines = [f"电平报告（{len(score.tracks)} 轨；以最响轨为相对 0 dB；窗口 {win:.1f}s）",
             f"混音峰值（未缩放，满刻度 1.0）：{mix_peak:.3f}" + ("  ⚠ 超过 1.0（会削波/被整体缩放）" if mix_peak > 1.0 else "  ✓")]
    for i, name, rms_all, rms_win, n in rows:
        if rms_all:
            lines.append(f"track[{i}] {name}: 整段 {db(rms_all):.1f} dB / 最响窗口 {db(rms_win):.1f} dB "
                         f"（相对主轨 {db(rms_all) - db(ref):+.1f} dB；{n} 音）")
        else:
            lines.append(f"track[{i}] {name}: （无音符，{n} 音）")
    lines.append("提示：调整音量用 set_track_mix（volume），目标=各轨在「同时发声窗口」相对电平接近 0 dB（见配器预设 level_hint）")
    return "\n".join(lines)


def tool_export_audio(args: dict) -> str:
    """渲染（含效果链，立体声）→ 导出 mp3（ffmpeg 320k，ffmpeg 必需）或 wav。"""
    from ..host import HostEngine

    score_path = args["score_path"]
    out = Path(args["out"])
    fmt = str(args.get("format", "mp3")).lower()
    if fmt not in ("mp3", "wav"):
        raise ValueError(f"format 只支持 mp3/wav：{fmt!r}")
    engine = HostEngine()
    session = engine.load_score(score_path)
    try:
        if fmt == "wav":
            wav_path = out if out.suffix.lower() == ".wav" else out.with_suffix(".wav")
            audio = engine.render(session, out_wav=wav_path, stereo=True)
            peak = float(max(abs(float(audio.min())), abs(float(audio.max())))) if audio.size else 0.0
            return f"已导出 WAV：{wav_path}（{len(audio) / engine.samplerate:.2f}s，峰值 {peak:.3f}）"
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("找不到 ffmpeg（导出 mp3 需要）")
        wav_path = out.with_suffix(".wav")
        audio = engine.render(session, out_wav=wav_path, stereo=True)
        peak = float(max(abs(float(audio.min())), abs(float(audio.max())))) if audio.size else 0.0
        out.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run([ffmpeg, "-y", "-i", str(wav_path), "-codec:a", "libmp3lame",
                               "-b:a", "320k", str(out)],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg 失败：{proc.stderr[-300:]}")
        size = out.stat().st_size
        return (f"已导出 MP3：{out}（{len(audio) / engine.samplerate:.2f}s，峰值 {peak:.3f}，"
                f"{size / 1024:.0f} KB；同目录保留 {wav_path.name}）")
    finally:
        session.close()


def tool_export_midi(args: dict) -> str:
    """Score → MIDI 文件（含 tempo/拍号/音色；鼓轨走 MIDI ch10）。"""
    from ..midi.export import score_to_midi

    score_path = args["score_path"]
    out = args.get("out") or str(Path(score_path).with_suffix(".mid"))
    score = _load(score_path)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    score_to_midi(score, out)
    n = sum(len(t.notes) for t in score.tracks)
    return (f"已导出 MIDI：{out}（{len(score.tracks)} 轨 / {n} 音；tempo={score.tempo:.1f} "
            f"sig={score.time_signature}）")


def tool_read_text(args: dict) -> str:
    """读取仓库内的文本文件原文（预设/技能/文档/配置；限仓库内、≤400KB）。"""
    root = Path(__file__).resolve().parents[2]  # 仓库根
    p = Path(str(args["path"]))
    if not p.is_absolute():
        p = root / p
    try:
        p = p.resolve()
        rel = p.relative_to(root)
    except ValueError:
        raise ValueError(f"只允许读仓库内文件（{root}）；路径越界：{args['path']!r}") from None
    if not p.is_file():
        raise FileNotFoundError(str(p))
    size = p.stat().st_size
    if size > 400_000:
        raise ValueError(f"文件过大（{size} bytes > 400KB）：{rel}（换用更小的文件或只看片段）")
    text = p.read_text(encoding="utf-8", errors="replace")
    cap = 60_000
    cut = f"\n…（截断：全文 {size} bytes，此处显示前 {cap} 字符）" if len(text) > cap else ""
    return f"文件 {rel}（{size} bytes）：\n{text[:cap]}{cut}"


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------


def register_compose_tools(registry: ToolRegistry) -> None:
    """把作曲/工程工具注册进 agent 工具表（M-V6 双任务工具链）。"""
    registry.register(ToolSpec(
        name="voice_to_score",
        description="Voice JSON（transcribe 产物）→ Score JSON（单轨 piano，tempo 取 Voice.bpm）；默认写 agent-edited-score.json",
        parameters={"type": "object", "properties": {
            "voice_path": {"type": "string"},
            "output": {"type": "string", "description": "输出路径（缺省 <voice 目录>/agent-edited-score.json）"},
            "title": {"type": "string"},
        }, "required": ["voice_path"]},
        handler=tool_voice_to_score))

    registry.register(ToolSpec(
        name="detect_key",
        description="重算调性候选（基于音高分布；用于判断调外音/修复方向）",
        parameters={"type": "object", "properties": {
            "score_path": {"type": "string"},
            "track": {"description": "轨索引或轨名（缺省 0）"},
        }, "required": ["score_path"]},
        handler=tool_detect_key))

    registry.register(ToolSpec(
        name="create_track",
        description="新增音轨（音色名限 GM 表：piano/e_piano/guitar*/bass/synth_bass/synth_lead*/pad*/strings/organ/brass/drums）",
        parameters={"type": "object", "properties": {
            "score_path": {"type": "string"},
            "name": {"type": "string"},
            "program": {"type": "string"},
            "volume": {"type": "number"},
        }, "required": ["score_path", "program"]},
        handler=tool_create_track))

    registry.register(ToolSpec(
        name="write_notes",
        description=("按「小节+16分格」写音符：note={bar,grid,len,note|pitch_midi,velocity?}；"
                     "6/8 每小节 12 格（八分格 n = 16分格 2n-1），4/4 每小节 16 格；"
                     "音名 C4=60（如 E4 / f#3）；mode=append|replace"),
        parameters={"type": "object", "properties": {
            "score_path": {"type": "string"},
            "track": {"description": "轨索引或轨名"},
            "notes": {"type": "array", "items": {"type": "object"}},
            "mode": {"type": "string", "enum": ["append", "replace"]},
        }, "required": ["score_path", "notes"]},
        handler=tool_write_notes))

    registry.register(ToolSpec(
        name="duplicate_bars",
        description="整小节复制（段落复制填充）：src 小节范围 → dest 起点；track 省略 = 全轨复制（复刻段落）",
        parameters={"type": "object", "properties": {
            "score_path": {"type": "string"},
            "src_start_bar": {"type": "integer"},
            "src_end_bar": {"type": "integer"},
            "dest_start_bar": {"type": "integer"},
            "track": {"description": "轨索引或轨名（缺省=全轨）"},
        }, "required": ["score_path", "src_start_bar", "src_end_bar", "dest_start_bar"]},
        handler=tool_duplicate_bars))

    registry.register(ToolSpec(
        name="set_track_mix",
        description="调轨音量（Instrument.volume 0-2）与声像（pan -1..1，负左正右）",
        parameters={"type": "object", "properties": {
            "score_path": {"type": "string"},
            "track": {"description": "轨索引或轨名"},
            "volume": {"type": "number"},
            "pan": {"type": "number"},
        }, "required": ["score_path"]},
        handler=tool_set_track_mix))

    registry.register(ToolSpec(
        name="apply_effect",
        description="给轨应用效果预设（混响等；预设名如 piano-pop-reverb / piano-bright-hall / synth-lead / master-limiter）",
        parameters={"type": "object", "properties": {
            "score_path": {"type": "string"},
            "track": {"description": "轨索引或轨名"},
            "preset": {"type": "string"},
        }, "required": ["score_path", "preset"]},
        handler=tool_apply_effect))

    registry.register(ToolSpec(
        name="apply_pattern",
        description="往鼓轨落内置 6/8 高速鼓型：wotaiko_drums_base（主歌）/ wotaiko_drums_energy（副歌）；crash=true 加段头镲",
        parameters={"type": "object", "properties": {
            "score_path": {"type": "string"},
            "track": {"description": "轨索引或轨名（缺省 0）"},
            "pattern": {"type": "string", "enum": list(_DRUM_PATTERNS)},
            "start_bar": {"type": "integer"},
            "bars": {"type": "integer"},
            "crash": {"type": "boolean"},
        }, "required": ["score_path", "pattern"]},
        handler=tool_apply_pattern))

    registry.register(ToolSpec(
        name="analyze_levels",
        description="渲染各轨 stem 测 RMS（整段+最响窗口）+ 混音峰值——多轨音量平衡的判断依据",
        parameters={"type": "object", "properties": {
            "score_path": {"type": "string"},
            "window_sec": {"type": "number"},
        }, "required": ["score_path"]},
        handler=tool_analyze_levels))

    registry.register(ToolSpec(
        name="export_audio",
        description="渲染（含效果链，立体声）→ 导出 mp3（ffmpeg 320k）或 wav",
        parameters={"type": "object", "properties": {
            "score_path": {"type": "string"},
            "out": {"type": "string", "description": "输出路径（mp3 时给 .mp3）"},
            "format": {"type": "string", "enum": ["mp3", "wav"]},
        }, "required": ["score_path", "out"]},
        handler=tool_export_audio))

    registry.register(ToolSpec(
        name="export_midi",
        description="Score → MIDI 文件（含 tempo/拍号/音色；鼓轨走 ch10）",
        parameters={"type": "object", "properties": {
            "score_path": {"type": "string"},
            "out": {"type": "string", "description": "输出路径（缺省与 score 同目录同名 .mid）"},
        }, "required": ["score_path"]},
        handler=tool_export_midi))

    registry.register(ToolSpec(
        name="read_text",
        description="读取仓库内文本文件原文（预设 json / 技能 md / 文档；≤400KB，超长截断）",
        parameters={"type": "object", "properties": {
            "path": {"type": "string", "description": "仓库内相对路径或绝对路径（如 presets/arrangements/wotaiko-fast-6-8.json）"},
        }, "required": ["path"]},
        handler=tool_read_text))
