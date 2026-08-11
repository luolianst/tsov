"""音频预处理脚本（FR-08 / ADR-0004）：ffmpeg 转 16k 单声道 wav + noisereduce 降噪。

用法：
  python scripts/transcode_denoise.py [输入文件或目录...] [-o 输出目录] [--sr 16000] [--strength 0.8]

默认输入 `samples/raw/`，输出 `output/m1-preprocess/`（16k/ + denoised/ + manifest.json）。
薄壳：复用 tsov.dsp.preprocess（库函数两种入口之一，另一个是 CLI `tsov preprocess`）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tsov.dsp import preprocess_batch  # noqa: E402


def collect_inputs(paths: list[str]) -> list[Path]:
    """展开输入：文件直接收，目录递归收音频（m4a/mp3/wav/flac/ogg）。"""
    audio_ext = {".m4a", ".mp3", ".wav", ".flac", ".ogg"}
    found: list[Path] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            found.extend(f for f in path.rglob("*") if f.suffix.lower() in audio_ext)
        elif path.is_file():
            found.append(path)
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="转码 16k wav + noisereduce 降噪")
    parser.add_argument("inputs", nargs="*", default=["samples/raw"], help="输入文件或目录（默认 samples/raw）")
    parser.add_argument("-o", "--output-dir", default="output/m1-preprocess")
    parser.add_argument("--sr", type=int, default=16000)
    parser.add_argument("--strength", type=float, default=0.8, help="降噪强度 0-1")
    args = parser.parse_args(argv)

    inputs = collect_inputs(args.inputs)
    if not inputs:
        print("未找到音频文件", file=sys.stderr)
        return 1
    print(f"共 {len(inputs)} 个输入：")
    for i in inputs:
        print(f"  {i}")

    results = preprocess_batch(inputs, args.output_dir, sr=args.sr, noise_reduce_strength=args.strength)
    print()
    for r in results:
        print(f"[{r.elapsed_sec:6.1f}s] {Path(r.source).name} -> {r.denoised_path} ({r.duration_sec}s)")
    print(f"\n完成 {len(results)} 个，manifest: {Path(args.output_dir) / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
