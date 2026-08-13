"""CLI 薄壳入口（ADR-0005 决策 1/6：库 + CLI 薄壳，同一套函数两种入口）。

命令：
- `tsov preprocess <输入...> -o <输出目录>`：转码 16k wav + noisereduce 降噪
- `tsov backends`：列出 DSP / 渲染后端（DSP 为注册表插件，M4 定案 game）
- `tsov run <wav> [--backend game]`：M3 闭环（转录→语义层→LLM 分析→MIDI→回放）
- `tsov transcribe/analyze/render/eval`：各阶段单跑
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

    print(f"== 编辑结果 ({len(result.new_score.tracks[0].notes)} 音) ==")
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


def _raise_notimpl(cmd: str) -> int:
    raise NotImplementedError(f"{cmd} 命令接口已建，实现随 M2/M3 填充")


def main(argv: list[str] | None = None) -> int:
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
