"""CLI 薄壳入口（ADR-0005 决策 1/6：库 + CLI 薄壳，同一套函数两种入口）。

命令：
- `tsov preprocess <输入...> -o <输出目录>`：转码 16k wav + noisereduce 降噪
- `tsov backends`：列出 DSP / 渲染后端（DSP 为注册表插件：game 成品歌 + rmvpe 哼唱）
- `tsov run <wav> [--backend game]`：M3 闭环（转录→语义层→LLM 分析→MIDI→回放）
- `tsov transcribe/analyze/render/eval`：各阶段单跑
- `tsov arrange <score.json> [--key]`：自动配器（melody+harmony+bass+drums 多轨）
- `tsov edit <score.json> [--feedback] [--annotations]`：对话式改谱（M4，人工标注+LLM）
- `tsov understand <audio>` / `tsov style <score> <ref>`：MOSS 参考曲理解 / 风格改谱（M5/M6）
- `tsov agent run <task>`：agentloop 独立最小闭环（ADR-0012，不依赖 dsh/opencode）
- `tsov host render/play/record <score.json>`：宿主框架渲染 / 回放 / 录制（ADR-0013）
- `tsov web [--host 127.0.0.1] [--port 8790]`：可视化宿主 Web 壳（M-V2，ADR-0014，docs/05）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _dsp_backends() -> list[str]:
    from .dsp import list_backends

    return list_backends()


def _cmd_preprocess(args: argparse.Namespace) -> int:
    from .dsp import preprocess_batch

    results = preprocess_batch(
        args.inputs,
        args.output_dir,
        sr=args.sr,
        noise_reduce_strength=args.noise_reduce_strength,
    )
    for r in results:
        print(f"{r.source} -> {r.denoised_path}  ({r.duration_sec}s)")
    print(f"共 {len(results)} 个文件，manifest 在 {args.output_dir}/manifest.json")
    return 0


def _cmd_backends(args: argparse.Namespace) -> int:
    from .dsp import list_backends as list_dsp_backends
    from .dsp import transcribe as _t  # noqa: F401 仅确认模块可导入
    from .render import list_backends

    print("DSP backends（M4 插件注册表）:", ", ".join(list_dsp_backends()))
    print("Render backends:", list_backends())
    return 0


def _cmd_transcribe(args: argparse.Namespace) -> int:
    import json
    import os

    from .dsp import transcribe

    voice = transcribe(args.audio, backend=args.backend)
    if args.output is None:
        args.output = f"output/transcribe-{args.backend}.json"
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(voice.to_dict(), f, ensure_ascii=False, indent=2)
    print(f"backend={args.backend} notes={len(voice.notes)} -> {args.output}")
    return 0


def _cmd_analyze(args: argparse.Namespace) -> int:
    import json
    import os

    from .analysis import analyze
    from .core.notes import Voice

    voice = Voice.from_dict(json.load(open(args.voice_json, encoding="utf-8")))
    result = analyze(voice, llm=not args.no_llm)
    if args.output is None:
        args.output = f"output/analyze-{os.path.basename(args.voice_json)}"
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
    if result.error:
        print(f"[降级] {result.error}")
    print(f"key_candidates={result.key_candidates} suspicious={len(result.suspicious_notes)} -> {args.output}")
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    import json

    from .core.score import Score
    from .render import render_score

    score = Score.from_dict(json.load(open(args.score_json, encoding="utf-8")))
    out = render_score(score, backend="fluidsynth", output_path=args.output, soundfont=args.soundfont)
    print(f"回放 WAV：{out}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    import datetime
    import json

    from .pipeline import run_closed_loop

    if args.out_dir is None:
        args.out_dir = f"output/m3-closed-loop/{datetime.date.today().isoformat()}"
    summary = run_closed_loop(
        args.wav,
        out_dir=args.out_dir,
        backend=args.backend,
        soundfont=args.soundfont,
        llm=not args.no_llm,
    )
    print("== M3 闭环完成 ==")
    print(f"输入：{summary['input_audio']}")
    print(f"音符数：{summary['note_count']}  backend={summary['backend']}  llm_used={summary['llm_used']}")
    if summary["analysis_error"]:
        print(f"[降级] LLM 分析：{summary['analysis_error']}")
    for name, path in summary["artifacts"].items():
        print(f"  {name}: {path}")
    print(f"总耗时：{summary['total_elapsed_sec']}s（各阶段见 {summary['artifacts']['summary']}）")
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    import json

    from .core.notes import Voice
    from .eval import compare_to_reference, compute_self_contained

    voice = Voice.from_dict(json.load(open(args.voice_json, encoding="utf-8")))
    self_metrics = compute_self_contained(voice)
    print("== 自足指标 ==")
    print(json.dumps(self_metrics.to_dict(), ensure_ascii=False, indent=2))
    if args.reference:
        ref_metrics = compare_to_reference(voice, args.reference)
        print("== 参考谱对比 ==")
        print(
            json.dumps(
                {k: v for k, v in ref_metrics.to_dict().items() if k != "detail"},
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tsov", description="the shape of voice — AI 音乐创作 agent（原型阶段）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("preprocess", help="m4a/mp3 → 16k 单声道 wav + noisereduce 降噪")
    p.add_argument("inputs", nargs="+", help="输入音频文件")
    p.add_argument("-o", "--output-dir", default="output/m1-preprocess", help="输出目录")
    p.add_argument("--sr", type=int, default=16000, help="目标采样率")
    p.add_argument("--noise-reduce-strength", type=float, default=0.8, help="降噪强度 0-1")
    p.set_defaults(func=_cmd_preprocess)

    p = sub.add_parser("backends", help="列出 DSP / 渲染后端")
    p.set_defaults(func=_cmd_backends)

    dsp_backends = list(_dsp_backends())
    p = sub.add_parser("transcribe", help="音频 wav → Voice 声部对象（插件后端）")
    p.add_argument("audio", help="输入 wav")
    p.add_argument("--backend", default="game", choices=dsp_backends)
    p.add_argument("-o", "--output", default=None, help="输出 stage JSON 路径")
    p.set_defaults(func=_cmd_transcribe)

    p = sub.add_parser("analyze", help="Voice → 语义层数据集 → LLM 分析")
    p.add_argument("voice_json", help="原始层 Voice 的 stage JSON")
    p.add_argument("--no-llm", action="store_true", help="跳过 LLM 分析（只出规则语义层）")
    p.add_argument("-o", "--output", default=None, help="输出 stage JSON 路径")
    p.set_defaults(func=_cmd_analyze)

    p = sub.add_parser("render", help="Score → 音频（fluidsynth 第一适配器）")
    p.add_argument("score_json", help="Score 的 stage JSON")
    p.add_argument("--soundfont", default=None, help=".sf2 路径；默认 vendor/soundfonts/FluidR3_GM.sf2")
    p.add_argument("-o", "--output", default="out.wav")
    p.set_defaults(func=_cmd_render)

    p = sub.add_parser("run", help="M3 闭环：音频 wav → 转录 → 语义层 → LLM 分析 → MIDI → 回放 WAV")
    p.add_argument("wav", help="输入哼唱 wav（16k 单声道，建议先 preprocess 降噪）")
    p.add_argument("--backend", default="game", choices=dsp_backends)
    p.add_argument("--soundfont", default=None, help=".sf2 路径；默认 vendor/soundfonts/FluidR3_GM.sf2")
    p.add_argument("--out-dir", default=None, help="输出目录；默认 output/m3-closed-loop/<日期>/")
    p.add_argument("--no-llm", action="store_true", help="跳过 LLM 分析（离线跑通）")
    p.set_defaults(func=_cmd_run)

    p = sub.add_parser("eval", help="评估指标输出（M2：自足 + 参考谱对比）")
    p.add_argument("voice_json", help="原始层 Voice 的 stage JSON")
    p.add_argument("--reference", default=None, help="参考谱（MIDI/JSON 路径），可选")
    p.set_defaults(func=_cmd_eval)

    p = sub.add_parser("arrange", help="M7 自动配器：单轨旋律 → 多轨（melody+harmony+bass+drums）")
    p.add_argument("score_json", help="Score 的 stage JSON")
    p.add_argument("--style", default="pop", help="配器风格（第一版只支持 pop）")
    p.add_argument("--key", default=None, help="调式（如 'D dorian'）；缺省取 score.key_candidates[0]")
    p.add_argument("-o", "--output", default=None, help="输出多轨 score.json 路径")
    p.set_defaults(func=_cmd_arrange)

    p = sub.add_parser("style", help="M6 参考曲风格改谱闭环：MOSS 理解 → LLM 改谱")
    p.add_argument("score_json", help="Score 的 stage JSON")
    p.add_argument("reference_audio", help="参考曲音频（成品歌/参考曲目）")
    p.add_argument("--extra", default="", help="补充要求（自然语言）")
    p.add_argument("--url", default="http://127.0.0.1:8300", help="MOSS 服务地址")
    p.add_argument("--no-llm", action="store_true", help="跳过 LLM 改谱")
    p.add_argument("--out-dir", default=None, help="产物目录；默认 output/m6-style/<日期>/<序号>")
    p.set_defaults(func=_cmd_style)

    p = sub.add_parser("understand", help="M5 参考曲目理解：MOSS-Music 服务化客户端")
    p.add_argument("audio", help="输入音频（成品歌/参考曲目）")
    p.add_argument("--url", default="http://127.0.0.1:8300", help="MOSS 服务地址")
    p.set_defaults(func=_cmd_understand)

    p = sub.add_parser("edit", help="M4 改谱：人工标注 + LLM 修正 Score")
    p.add_argument("score_json", help="Score 的 stage JSON（如 stage-04-score.json）")
    p.add_argument("--feedback", default="", help="自然语言修改反馈（喂 LLM）")
    p.add_argument("--annotations", default=None, help="人工标注 JSON 文件（[{index, action, value}]）")
    p.add_argument("--no-llm", action="store_true", help="跳过 LLM，只应用人工标注")
    p.add_argument("-o", "--output", default=None, help="输出修正后 score.json 路径")
    p.set_defaults(func=_cmd_edit)

    p = sub.add_parser("agent", help="agentloop 独立最小闭环（ADR-0012，不依赖 dsh/opencode）")
    agent_sub = p.add_subparsers(dest="agent_cmd", required=True)
    p2 = agent_sub.add_parser("run", help="一次会话跑任务：工具调用循环 → 最终回答 → 会话 JSONL 落盘")
    p2.add_argument("task", help="任务描述（自然语言，如 '读 …json 改成 D 多利亚调式并渲染回放'）")
    p2.add_argument("--max-turns", type=int, default=12, help="最大工具循环轮数")
    p2.add_argument("--model", default=None, help="LLM 模型（缺省 TSOV_LLM_MODEL）")
    p2.add_argument("--session-dir", default="output/agent-sessions", help="会话 JSONL 目录")
    p2.set_defaults(func=_cmd_agent_run)

    p = sub.add_parser("host", help="宿主框架（ADR-0013）：Score → 离线渲染 / 实时回放（同一 graph）")
    host_sub = p.add_subparsers(dest="host_cmd", required=True)
    p3 = host_sub.add_parser("render", help="离线渲染：Score JSON → WAV（HostEngine，SF2 音源）")
    p3.add_argument("score_json", help="Score 的 stage JSON")
    p3.add_argument("-o", "--output", default="output/host-render.wav", help="输出 wav 路径")
    p3.set_defaults(func=_cmd_host_render)
    p4 = host_sub.add_parser("play", help="实时回放 Score（预渲染缓冲 + sounddevice，阻塞到播完）")
    p4.add_argument("score_json")
    p4.set_defaults(func=_cmd_host_play)
    p5 = host_sub.add_parser("play-file", help="播放已有 WAV")
    p5.add_argument("wav")
    p5.set_defaults(func=_cmd_host_play_file)
    p6 = host_sub.add_parser("export", help="导出矩阵（M-V4）：master/bus/stems WAV + MIDI")
    p6.add_argument("score_json", help="Score 的 stage JSON")
    p6.add_argument("-o", "--out-dir", default="output/host-export", help="输出目录")
    p6.add_argument("--mono", action="store_true", help="单声道导出（默认立体声）")
    p6.add_argument("--no-stems", action="store_true", help="不导每轨 stems")
    p6.add_argument("--no-buses", action="store_true", help="不导总线 stems")
    p6.add_argument("--no-midi", action="store_true", help="不导 MIDI")
    p6.add_argument("--midi-stems", action="store_true", help="每轨单独 MIDI")
    p6.set_defaults(func=_cmd_host_export)
    p7 = host_sub.add_parser("record", help="录制：输入设备采集 → WAV（可接闭环转录）")
    p7.add_argument("-o", "--output", default="output/host-record.wav", help="输出 wav 路径")
    p7.add_argument("--seconds", type=float, default=5.0, help="录制时长（秒；默认 5）")
    p7.add_argument("--device", default=None, help="输入设备索引或名字子串（缺省=系统默认输入）")
    p7.add_argument("--samplerate", type=int, default=48000, help="采样率（默认 48000）")
    p7.add_argument("--channels", type=int, default=1, help="声道数（默认 1）")
    p7.add_argument("--list-devices", action="store_true", help="列出可用输入设备后退出")
    p7.add_argument("--then-transcribe", action="store_true", help="录完接 M3 闭环（转录→分析→MIDI）")
    p7.add_argument("--backend", default="game", help="--then-transcribe 的转录后端（默认 game）")
    p7.add_argument("--no-llm", action="store_true", help="--then-transcribe 时跳过 LLM 分析")
    p7.set_defaults(func=_cmd_host_record)

    p = sub.add_parser("presets", help="预设库：效果链预设 + 配器预设（list/show/apply-effect）")
    presets_sub = p.add_subparsers(dest="presets_cmd", required=True)
    q1 = presets_sub.add_parser("list", help="列出预设")
    q1.add_argument("--type", choices=["effects", "arrangements"], default=None, help="只看某类")
    q1.set_defaults(func=_cmd_presets)
    q2 = presets_sub.add_parser("show", help="查看预设详情")
    q2.add_argument("name")
    q2.set_defaults(func=_cmd_presets)
    q3 = presets_sub.add_parser("apply-effect", help="把效果预设应用到某轨（输出新 Score JSON）")
    q3.add_argument("score_json")
    q3.add_argument("track", help="轨道索引或轨名")
    q3.add_argument("preset", help="效果预设名")
    q3.add_argument("-o", "--output", default=None, help="输出 Score JSON（缺省 = 覆盖原文件旁 *-fx.json）")
    q3.set_defaults(func=_cmd_presets)

    p = sub.add_parser("web", help="M-V2 可视化宿主 Web 壳（ADR-0014）：FastAPI + 静态前端 + SSE")
    p.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1，仅本地回环）")
    p.add_argument("--port", type=int, default=8790, help="监听端口（默认 8790）")
    p.set_defaults(func=_cmd_web)

    return parser


def _cmd_edit(args: argparse.Namespace) -> int:
    import json
    import os

    from .analysis.edit import edit_score
    from .core.score import Score

    score = Score.from_dict(json.load(open(args.score_json, encoding="utf-8")))
    annotations = None
    if args.annotations:
        annotations = json.load(open(args.annotations, encoding="utf-8"))
        if not isinstance(annotations, list):
            raise ValueError("annotations 文件必须是 JSON 数组")
    result = edit_score(score, feedback=args.feedback, annotations=annotations, llm=not args.no_llm)

    if args.output is None:
        args.output = f"output/edit-{os.path.basename(args.score_json)}"
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result.new_score.to_dict(), f, ensure_ascii=False, indent=2)

    note_count = len(result.new_score.tracks[0].notes) if result.new_score.tracks else 0
    print(f"== 编辑结果 ({note_count} 音) ==")
    for line in result.diff_summary:
        print("  " + line)
    if result.error:
        print(f"[降级/拒绝] {result.error}")
    print(f"-> {args.output}")
    return 0


def _cmd_style(args: argparse.Namespace) -> int:
    import datetime
    import json
    import os

    from .analysis.style import style_transfer
    from .core.score import Score
    from .midi.export import score_to_midi
    from .render import render_midi

    if args.out_dir is None:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        args.out_dir = f"output/m6-style/{datetime.date.today().isoformat()}/{stamp}"
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    score = Score.from_dict(json.load(open(args.score_json, encoding="utf-8")))
    result, request_info = style_transfer(
        score, args.reference_audio, moss_url=args.url,
        extra_feedback=args.extra, llm=not args.no_llm,
    )

    # 落盘
    (out_dir / "style-request.json").write_text(
        json.dumps(request_info, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "edit-score.json").write_text(
        json.dumps(result.new_score.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "diff.txt").write_text(
        "\n".join(["# style 改谱 diff", *["  " + d for d in result.diff_summary]]), encoding="utf-8")
    if not args.no_llm:
        score_to_midi(result.new_score, str(out_dir / "song.mid"))
        render_midi(out_dir / "song.mid", out_dir / "song.wav")

    u = request_info["understanding"]
    (out_dir / "README.md").write_text(
        "# M6 参考曲风格改谱\n\n"
        f"- 输入 score：{args.score_json}\n"
        f"- 参考曲：{args.reference_audio}\n"
        f"- MOSS 理解：{u.get('description') or ''}（tags: {u.get('tags')}）\n"
        f"- feedback：{request_info['feedback']}\n"
        f"- 改动：{len(result.diff_summary)} 处（见 diff.txt）\n"
        f"- error：{result.error or '无'}\n"
        "- 产物：style-request.json / edit-score.json / diff.txt / song.mid / song.wav\n",
        encoding="utf-8",
    )

    u = request_info["understanding"]
    print("== M6 参考曲风格改谱 ==")
    print(f"参考曲理解：{u.get('description') or ''}（tags: {u.get('tags')}）")
    print("diff_summary:")
    for line in result.diff_summary:
        print("  " + line)
    if result.error:
        print(f"[降级/拒绝] {result.error}")
    print(f"产物 -> {out_dir}")
    return 0


def _cmd_arrange(args: argparse.Namespace) -> int:
    import json
    import os

    from .arrange import arrange
    from .core.score import Score

    score = Score.from_dict(json.load(open(args.score_json, encoding="utf-8")))
    out_score = arrange(score, style=args.style, key=args.key)
    if args.output is None:
        args.output = f"output/arrange-{os.path.basename(args.score_json)}"
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out_score.to_dict(), f, ensure_ascii=False, indent=2)

    print("== M7 自动配器 ==")
    for t in out_score.tracks:
        print(f"  {t.name:8s} program={t.instrument.program:8s} notes={len(t.notes)}")
    print(f"tempo={out_score.tempo}  key={out_score.key_candidates[0].key if out_score.key_candidates else '-'}")
    print(f"-> {args.output}")
    return 0


def _cmd_understand(args: argparse.Namespace) -> int:
    from .analysis.moss import understand

    result = understand(args.audio, url=args.url)
    print("== MOSS-Music 理解 ==")
    print(f"描述：{result.get('description') or '（空）'}")
    print(f"标签：{', '.join(result.get('tags') or []) or '（空）'}")
    lyrics = result.get("lyrics") or ""
    print(f"歌词：{lyrics[:120]}{'...' if len(lyrics) > 120 else ''}")
    if result.get("error"):
        print(f"[错误] {result['error']}")
        return 2
    return 0


def _cmd_agent_run(args: argparse.Namespace) -> int:
    from .agent import AgentLoop

    loop = AgentLoop(max_turns=args.max_turns, session_dir=args.session_dir, model=args.model)
    result = loop.run(args.task)
    print("== agent 任务 ==")
    for note in result.notes:
        print(f"  [工具观测] {note}")
    print(f"轮数={result.turns} 工具调用={result.tool_calls_made} 会话={result.session_path}")
    print("== 最终回答 ==")
    print(result.answer)
    return 0


def _cmd_host_render(args: argparse.Namespace) -> int:
    from .host import HostEngine

    engine = HostEngine()
    wav = engine.render_score(args.score_json, args.output)
    print(f"渲染完成：{wav}（{engine.samplerate}Hz，HostEngine/SF2 音源）")
    return 0


def _cmd_host_play(args: argparse.Namespace) -> int:
    from .host import HostEngine

    HostEngine().play_score(args.score_json)
    print("宿主回放完毕")
    return 0


def _cmd_host_play_file(args: argparse.Namespace) -> int:
    from .host import HostEngine

    HostEngine().play_wav(args.wav)
    print("宿主回放完毕")
    return 0


def _cmd_host_export(args: argparse.Namespace) -> int:
    from .host.engine import HostEngine

    report = HostEngine().export_score_file(
        args.score_json, args.out_dir,
        stereo=not args.mono,
        stems=not args.no_stems,
        buses=not args.no_buses,
        midi=not args.no_midi,
        midi_stems=args.midi_stems,
    )
    mode = "立体声" if report["stereo"] else "单声道"
    print(f"导出完成：{report['out_dir']}（{len(report['files'])} 个文件，{report['samplerate']}Hz，{mode}，缩放 {report['scale']}）")
    for f in report["files"]:
        print(f"  - {f}")
    return 0


def _cmd_presets(args: argparse.Namespace) -> int:
    import json

    from .presets import apply_effect_preset, load_library

    lib = load_library()
    if lib.errors:
        print("[警告] 预设加载有错误：")
        for e in lib.errors:
            print(f"  - {e}")

    if args.presets_cmd == "list":
        names = lib.list_names(args.type)
        for kind, items in names.items():
            print(f"== {kind}（{len(items)}）==")
            for n in items:
                preset = lib.effects[n] if kind == "effects" else lib.arrangements[n]
                print(f"  {n}  —  {preset.title}")
        return 0

    if args.presets_cmd == "show":
        if args.name in lib.effects:
            p = lib.effects[args.name]
            print(json.dumps(p.raw, ensure_ascii=False, indent=2))
        elif args.name in lib.arrangements:
            p = lib.arrangements[args.name]
            print(json.dumps(p.raw, ensure_ascii=False, indent=2))
        else:
            print(f"没有预设：{args.name}")
            return 1
        return 0

    # apply-effect
    from .core.score import Score

    score = Score.from_dict(json.load(open(args.score_json, encoding="utf-8")))
    track = int(args.track) if str(args.track).lstrip("-").isdigit() else args.track
    new_score = apply_effect_preset(score, track, args.preset, library=lib)
    out = args.output
    if out is None:
        src = args.score_json
        out = (src[:-5] if src.lower().endswith(".json") else src) + "-fx.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(new_score.to_dict(), f, ensure_ascii=False, indent=2)
    tr = new_score.tracks[track if isinstance(track, int) else next(i for i, t in enumerate(new_score.tracks) if t.name == track)]
    print(f"已应用效果预设 {args.preset!r} → 轨 {tr.name!r}（{len(tr.instrument.effects)} 个效果）→ {out}")
    return 0


def _cmd_host_record(args: argparse.Namespace) -> int:
    from .host.record import list_input_devices, record_to_wav

    if args.list_devices:
        for d in list_input_devices():
            mark = "*" if d["default"] else " "
            print(f"{mark} [{d['index']}] {d['name']}  ch={d['channels']}  sr={d['default_samplerate']:.0f}")
        return 0

    report = record_to_wav(
        args.output,
        seconds=args.seconds,
        device=args.device,
        samplerate=args.samplerate,
        channels=args.channels,
    )
    ov = "  [溢出!]" if report["overflowed"] else ""
    print(
        f"录制完成：{report['path']}  {report['seconds']}s  {report['frames']} frames  "
        f"{report['samplerate']}Hz/{report['channels']}ch{ov}"
    )
    if args.then_transcribe:
        import datetime

        from .pipeline import run_closed_loop

        out_dir = f"output/m3-closed-loop/{datetime.date.today().isoformat()}"
        summary = run_closed_loop(
            str(report["path"]), out_dir=out_dir, backend=args.backend, llm=not args.no_llm
        )
        print(
            f"闭环完成：notes={summary['note_count']} backend={summary['backend']} "
            f"llm_used={summary['llm_used']} 产物={out_dir}"
        )
    return 0


def _cmd_web(args: argparse.Namespace) -> int:
    from .web import main as web_main

    print(f"tsov 可视化宿主：http://{args.host}:{args.port}（Ctrl+C 退出）")
    web_main(host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    from .env import load_env

    load_env()  # 项目根 .env（gitignored，密钥走这里，不入库）
    # GBK 控制台防崩：LLM 回答里可能有 ✅/音符等非 GBK 字符（handoff 坑 82），打印一律降级替换
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 非 tty/已关闭等
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except NotImplementedError as e:
        print(f"[M2/M3 未实现] {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 CLI 薄壳收口，避免裸 traceback
        print(f"错误：{e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
