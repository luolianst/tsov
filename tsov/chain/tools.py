"""处理链工具注册表（M-V8 E3 段2）。

工具 = {id · 名称 · 档位 tier · 参数 schema · run 契约}；每个工具可单独调用，
「链」只是 presets/chains/*.json 里的工具序列 + 参数（没有魔法）。ADR-0017：
链、REST、agent 走同一实现。

档位（Q2 定）：
- fast：毫秒~百毫秒级（DSP）——改参自动顺跑（无「应用」按钮）
- slow：秒级（如转录）——改参标脏，「应用」按钮显式触发

run 契约：run(ctx: ChainContext) -> dict —— 读 ctx.prev（上一步产物）/ctx.source_audio，
写 ctx.artifact(...)，返回统计（并入状态）。取消检查点：ctx.check_cancel()（步边界）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path


class ChainCancelled(Exception):
    """链被用户取消（步边界检查点抛出）。"""


@dataclass
class ChainContext:
    """工具执行上下文（runner 构造；工具唯一入口）。"""

    project_root: Path
    run_dir: Path
    source_audio: Path
    score_tempo: float
    step_id: str
    params: dict
    prev: Path | None = None          # 上一步产物（链式输入）
    cancel: threading.Event | None = None

    def artifact(self, name: str) -> Path:
        """本步产物路径（<project>/chain/<run-ts>/<name>）。"""
        return self.run_dir / name

    def check_cancel(self) -> None:
        """取消检查点（各步开始时、耗时子过程前后调用）。"""
        if self.cancel is not None and self.cancel.is_set():
            raise ChainCancelled("已取消")

    def log(self, msg: str) -> None:
        """进度消息（runner 挂接；工具可自由调用）。"""
        hook = getattr(self, "on_log", None)
        if callable(hook):
            try:
                hook(str(msg))
            except Exception:  # noqa: BLE001 日志不该弄崩工具
                pass


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _ffmpeg(args: list[str]) -> None:
    """ffmpeg 调用（错误带 stderr；与 dsp/preprocess 同口径）。"""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except FileNotFoundError as e:
        raise RuntimeError(
            f"ffmpeg 不存在（PATH 与程序目录都找不到）：{e}。"
            f"若是开箱包：把 ffmpeg.exe/ffprobe.exe 放进 tsov 包的 ffmpeg\\ 文件夹后重启 tsov；"
            f"若是源码安装：完整 ffmpeg 发行包进 PATH。") from e
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败：{' '.join(cmd)}\n{(proc.stderr or '').strip()[:400]}")


def _load_notes(path: Path) -> tuple[list, float | None]:
    """读音符：voice.json（{notes,bpm,...}）或 notes 数组 json →（Note 列表, bpm）。"""
    from ..core.notes import Note

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return [Note.from_dict(n) for n in data.get("notes") or []], (float(data["bpm"]) if data.get("bpm") else None)
    return [Note.from_dict(n) for n in data], None


def _dump_notes(path: Path, notes: list) -> Path:
    Path(path).write_text(
        json.dumps([n.to_dict() for n in notes], ensure_ascii=False, indent=2), encoding="utf-8")
    return Path(path)


# ---------------------------------------------------------------------------
# 工具实现（哼唱链 5 件 + 批E 拆解链）
# ---------------------------------------------------------------------------


def _optional_float(value) -> float | None:
    """宽松解析可选数值：空/None/非法 → None（参数表单 text 控件友好）。"""
    import math

    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _phase_from_beats(beats, bpm: float) -> float:
    """网格相位＝median(拍点 mod 拍周期)（拍即网格锚）。"""
    import numpy as np

    arr = np.asarray(beats, dtype=float)
    if arr.size == 0 or bpm <= 0:
        return 0.0
    period = 60.0 / bpm
    return float(np.mod(float(np.median(np.mod(arr, period))), period))


def _onset_env(y, sr: int):
    """统一 hop=256 的 onset 强度包络（fold 扫描/兜底相位共用）。"""
    import numpy as np

    import librosa

    hop = 256
    oenv = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    t_env = np.arange(len(oenv)) * (hop / sr)
    return oenv, t_env


def _fold_scan(y, sr: int, bpm0: float, *, span: float = 0.05, step: float = 0.01):
    """折叠扫描精修 BPM：oenv 按候选网格折叠，峰锐度（max/mean）最大者胜。

    对「反拍重音」曲比 tempogram 更稳（全曲 onset 相干叠加）；返回 (bpm, oenv, t_env)。
    """
    import numpy as np

    oenv, t_env = _onset_env(y, sr)
    lo, hi = bpm0 * (1.0 - span), bpm0 * (1.0 + span)
    best_sc, best_bpm = -1.0, float(bpm0)
    for bpm in np.arange(lo, hi + step / 2, step):
        per = 60.0 / bpm
        hist, _ = np.histogram(np.mod(t_env, per), bins=96, range=(0, per), weights=oenv)
        sc = float(hist.max() / (hist.mean() + 1e-9))
        if sc > best_sc:
            best_sc, best_bpm = sc, float(bpm)
    return best_bpm, oenv, t_env


def _fold_phase(oenv, t_env, bpm: float) -> float:
    """oenv 折叠峰的相位（抛物线插值）——无打击素材的相位兜底。"""
    import numpy as np

    per = 60.0 / bpm
    bins = 512
    hist, _ = np.histogram(np.mod(t_env, per), bins=bins, range=(0, per), weights=oenv)
    i = int(hist.argmax())
    binw = per / bins
    frac = 0.0
    if 0 < i < bins - 1:
        a, b, c = float(hist[i - 1]), float(hist[i]), float(hist[i + 1])
        den = a - 2 * b + c
        if den != 0:
            frac = 0.5 * (a - c) / den
    return float(np.mod((i + 0.5 + frac) * binw, per))


def _attack_onsets(y, sr: int):
    """低频瞬态起跳点（样本级，物理域）：kick/低频打击的 attack 起点列表。"""
    import numpy as np
    import scipy.signal as ss

    sos = ss.butter(4, 150.0, "lowpass", fs=sr, output="sos")
    low = ss.sosfiltfilt(sos, y)
    env = np.abs(low)
    k = max(1, int(0.004 * sr))
    env = np.convolve(env, np.ones(k) / k, mode="same")
    if env.size == 0 or float(env.max()) <= 0:
        return np.array([])
    peak_idx, _ = ss.find_peaks(env, height=float(np.percentile(env, 99.0)),
                                distance=max(1, int(0.25 * sr)))
    onsets = []
    for p in peak_idx:
        h = float(env[p])
        lo = max(0, int(p) - int(0.15 * sr))
        seg = env[lo:p]
        below = np.where(seg < 0.15 * h)[0]
        if below.size == 0:
            continue
        onsets.append((lo + int(below[-1]) + 1) / sr)
    return np.asarray(onsets, dtype=float)


def _phase_from_attacks(onsets, bpm: float) -> float | None:
    """起跳点 → 相位（循环中位）；点数不足/离散度大 → None（走兜底）。"""
    import numpy as np

    arr = np.asarray(onsets, dtype=float)
    if arr.size < 16 or bpm <= 0:
        return None
    per = 60.0 / bpm
    ang = 2 * np.pi * np.mod(arr, per) / per
    phi0 = float(np.angle(np.mean(np.exp(1j * ang)))) % (2 * np.pi)
    ph0 = phi0 / (2 * np.pi) * per
    delta = np.mod(np.mod(arr, per) - ph0 + per / 2, per) - per / 2
    if float(np.std(delta)) > per / 8:
        return None
    return float(np.mod(ph0 + float(np.median(delta)), per))


def _find_grid_json(ctx: ChainContext) -> dict | None:
    """找网格参考 grid.json：本次 run 目录优先 → 历史 run（新→旧）回退 → None。

    「改参再跑」只跑下游步时，grid.json 属更早的 run 目录（复用跨 run 继承口径）。
    """
    cands = [ctx.run_dir / "grid.json"]
    chain_root = ctx.project_root / "chain"
    if chain_root.is_dir():
        try:
            for d in sorted((p for p in chain_root.iterdir()
                             if p.is_dir() and p.name != ctx.run_dir.name),
                            key=lambda p: p.name, reverse=True):
                cands.append(d / "grid.json")
        except OSError:
            pass
    for p in cands:
        try:
            if p.is_file():
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except (OSError, ValueError):
            continue
    return None


def _kill_process_tree(pid: int) -> None:
    """杀进程树（Windows：taskkill /T /F 穿 trampoline 链；POSIX：killpg）。"""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    else:
        import signal

        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def _exec_child(ctx: ChainContext, cmd: list[str], *, label: str, eta_sec: float,
                env_extra: dict | None = None, on_cancel=None,
                heartbeat_sec: float = 10.0) -> tuple[int, list[str], float]:
    """跑子进程（慢档 U1）：心跳日志 + 取消杀进程树；返回 (rc, 输出尾部, 实耗秒)。

    - 取消：ctx.cancel 置位 → 杀进程树 → on_cancel() 清半成品 → raise ChainCancelled
    - 输出：stderr 并入 stdout 收集尾部（失败诊断）；编码强制 utf-8/replace（GBK 输出不炸读线程）
    """
    env = {**os.environ, **(env_extra or {})}
    t0 = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", env=env,
                            start_new_session=(os.name != "nt"))
    tail: deque = deque(maxlen=80)
    reader = threading.Thread(target=lambda: [tail.append(ln) for ln in proc.stdout], daemon=True)
    reader.start()
    last_beat = 0.0
    while proc.poll() is None:
        if ctx.cancel is not None and ctx.cancel.is_set():
            _kill_process_tree(proc.pid)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            if on_cancel is not None:
                try:
                    on_cancel()
                except OSError:
                    pass
            raise ChainCancelled(f"已取消：{label}进程已终止")
        elapsed = time.time() - t0
        if elapsed - last_beat >= heartbeat_sec:
            ctx.log(f"{label}… 已 {int(elapsed)}s / 预计 ~{int(eta_sec)}s")
            last_beat = elapsed
        time.sleep(0.5)
    reader.join(timeout=2)
    return int(proc.returncode or 0), [ln.rstrip() for ln in tail], time.time() - t0


def _find_stem_wav(ctx: ChainContext, stem: str) -> Path | None:
    """找声部轨 stems/<stem>.wav：本次 run 目录优先 → 历史 run（新→旧）回退 → None。"""
    rel = Path("stems") / f"{stem}.wav"
    cands = [ctx.run_dir / rel]
    chain_root = ctx.project_root / "chain"
    if chain_root.is_dir():
        try:
            for d in sorted((p for p in chain_root.iterdir()
                             if p.is_dir() and p.name != ctx.run_dir.name),
                            key=lambda p: p.name, reverse=True):
                cands.append(d / rel)
        except OSError:
            pass
    for p in cands:
        if p.is_file():
            return p
    return None


def _stem_tag_from_voice(name: str) -> str | None:
    """voice-<声部>.json → <声部>（auto 链名不匹配 → None）。"""
    for s in _SEP_STEMS:
        if name == f"voice-{s}.json":
            return s
    return None


def _stem_tag_from_quantized(name: str) -> str | None:
    """notes-<声部>-quantized.json → <声部>（auto 链名不匹配 → None）。"""
    for s in _SEP_STEMS:
        if name == f"notes-{s}-quantized.json":
            return s
    return None


def _run_analyze_grid(ctx: ChainContext) -> dict:
    """① 定速与对齐：源曲音频粗检 BPM＋拍点相位 → grid.json（全链网格参考）。

    方法：beat_track 取拍点数组；BPM 用「oenv 折叠扫描」精修（对反拍重音比
    tempogram 稳）；相位首选「低频瞬态起跳点」（物理域）→ oenv 折叠峰兜底。
    局限（如实）：恒定速度假设（变速歌无 tempo track，逐段修正＝后话）。
    手调口：bpm_override / offset_ms 改参再跑。
    """
    import numpy as np

    import librosa

    ctx.check_cancel()
    src = ctx.source_audio
    ctx.log("定速与对齐：分析节拍中…")
    y, sr = librosa.load(str(src), sr=22050, mono=True)
    duration = float(len(y)) / float(sr)
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    bpm_detected = float(np.atleast_1d(tempo)[0])
    beats = librosa.frames_to_time(np.asarray(beat_frames, dtype=int), sr=sr)
    ctx.check_cancel()

    override = _optional_float(ctx.params.get("bpm_override"))
    oenv = t_env = None
    if override is not None and override > 0:
        bpm = override
    else:
        bpm, oenv, t_env = _fold_scan(y, sr, bpm_detected)

    # 相位：低频起跳点（物理域，首选）→ oenv 折叠峰（兜底）
    phase = None
    phase_source = "fold"
    n_attacks = 0
    try:
        attacks = _attack_onsets(y, sr)
        n_attacks = int(attacks.size)
        phase = _phase_from_attacks(attacks, bpm)
        if phase is not None:
            phase_source = "attacks"
    except Exception:  # noqa: BLE001 相位兜底链：起跳点路失败不影响产出
        pass
    if phase is None:
        if oenv is None:
            oenv, t_env = _onset_env(y, sr)
        phase = _fold_phase(oenv, t_env, bpm)
    offset_ms = _optional_float(ctx.params.get("offset_ms")) or 0.0
    beat_offset = float(np.mod(phase + offset_ms / 1000.0, 60.0 / bpm))

    if beats.size >= 3:
        ioi = np.diff(beats)
        mean = float(np.mean(ioi))
        conf = float(np.clip(1.0 - (float(np.std(ioi)) / mean if mean > 0 else 1.0), 0.0, 1.0))
    else:
        conf = 0.0

    out = ctx.artifact("grid.json")
    out.write_text(json.dumps({
        "bpm": round(bpm, 6),
        "beat_offset": round(beat_offset, 6),
        "beats": [round(float(t), 6) for t in beats],
        "confidence": round(conf, 3),
        "duration_sec": round(duration, 3),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"artifact": out.name, "bpm": round(bpm, 3), "beat_offset": round(beat_offset, 6),
            "beats_count": int(beats.size), "confidence": round(conf, 3),
            "duration_sec": round(duration, 3), "bpm_detected": round(bpm_detected, 3),
            "phase_source": phase_source, "attacks": n_attacks}


_SEP_STEMS = ("vocals", "drums", "bass", "other")


# 质量档人声路：解释器直启入口（2026-10-14——不再依赖 Scripts 启动器 exe；
# uv 生成的 exe 内嵌装机绝对路径，开箱包搬家即废〔v0.1.6 轮同族教训〕）
_SEP_ENTRY = ("import sys; sys.argv[0] = 'audio-separator'; "
              "from audio_separator.utils.cli import main; sys.exit(main())")


def _sep_assets_check() -> tuple[bool, str]:
    """质量档人声路资产：（audio-separator 包 ＋ Roformer 模型）就位？返回 (ok, 说明)。"""
    import importlib.util

    model_file = (Path(__file__).resolve().parents[2] / "vendor" / "audio-separator-models"
                  / "vocals_mel_band_roformer.ckpt")
    missing: list[str] = []
    if importlib.util.find_spec("audio_separator") is None:
        missing.append("audio-separator 未安装（当前解释器环境找不到包）")
    if not model_file.is_file():
        missing.append(f"Roformer 模型缺失（{model_file}）")
    if not missing:
        return True, ""
    hint = ("指路：模型缺 → python scripts/fetch_engines.py --only sep-model；"
            "包缺 → 装法见 docs/refs/E批-B档资产-拉取手册-2026-10-10.md")
    return False, "；".join(missing) + "。" + hint


def _sep_failure_message(rc: int, tail: list[str], src) -> str:
    """分离失败 → 带指路的报错（J2 风格）。"""
    text = "\n".join(tail[-40:])
    low = text.lower()
    hints: list[str] = []
    if "out of memory" in low:
        hints.append("可能是显存不足：关掉占用 GPU 的程序后重试，或先用快档")
    if "download" in low or "urlopen" in low or "connection" in low or "timed out" in low:
        hints.append("可能是模型权重下载失败：检查网络，或跑 python scripts/fetch_engines.py --only demucs 预置")
    if "ffmpeg" in low:
        hints.append("可能是缺少 ffmpeg：开箱包把 ffmpeg.exe 放进包的 ffmpeg\\ 目录；源码装保证 PATH 里有")
    if not hints:
        hints.append("可先重试一次；仍失败请连本条消息一起报障")
    return (f"分离失败（demucs 退出码 {rc}）：{src.name}\n"
            f"---- 输出尾部 ----\n{text}\n---- 指路 ----\n" + "\n".join(hints))


def _run_separate(ctx: ChainContext) -> dict:
    """② 分离：源曲 → 四轨 stems（快档＝demucs htdemucs；质量档＝人声 Roformer＋其余 ft）。

    产物：stems/{vocals,drums,bass,other}.wav ＋ stems/manifest.json（来源/档位/模型/耗时/字节）。
    慢档 UX（U1）：开始含 ETA、每 ~10s 心跳、完成报实耗；取消＝杀进程树＋清半成品。
    质量档缺件（audio-separator/Roformer）→ 自动降级纯 ft ＋ 日志/stats 指路（绝不静默）。
    """
    import sys

    from ..dsp.preprocess import probe_duration

    quality = str(ctx.params.get("quality") or "fast")
    if quality not in ("fast", "quality"):
        quality = "fast"

    src = ctx.source_audio
    stems_dir = ctx.run_dir / "stems"
    raw_dir = stems_dir / "_raw"
    raw_sep = stems_dir / "_raw_sep"
    shutil.rmtree(stems_dir, ignore_errors=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    ctx.check_cancel()
    duration = probe_duration(src)
    py = sys.executable

    def _cleanup() -> None:
        shutil.rmtree(stems_dir, ignore_errors=True)

    degraded = False
    degrade_reason = ""
    if quality == "quality":
        ok, reason = _sep_assets_check()
        if not ok:
            degraded, degrade_reason = True, reason
            ctx.log(f"质量档缺件 → 自动降级纯 demucs-ft：{reason}")

    # 路①：demucs（快档 htdemucs；质量档/降级 htdemucs_ft）
    demucs_model = "htdemucs" if quality == "fast" else "htdemucs_ft"
    eta1 = (10.0 + 0.06 * duration) if quality == "fast" else (15.0 + 0.09 * duration)
    label1 = "分离中（快档）" if quality == "fast" else "分离中（质量档·伴奏 ft）"
    ctx.log(f"{label1}：预计 ~{int(eta1)}s（素材 {duration:.0f}s）")
    rc1, tail1, elapsed1 = _exec_child(
        ctx, [py, "-m", "demucs", "-d", "cuda", "-n", demucs_model, "-o", str(raw_dir), str(src)],
        label=label1, eta_sec=eta1,
        env_extra={"TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD": "1"}, on_cancel=_cleanup)
    if rc1 != 0:
        raise RuntimeError(_sep_failure_message(rc1, tail1, src))
    ctx.check_cancel()
    ctx.log(f"{label1}完成：实耗 {elapsed1:.1f}s")

    # 路②：质量档人声 Roformer（未降级时）
    vocals_override: Path | None = None
    elapsed2 = 0.0
    if quality == "quality" and not degraded:
        eta2 = 12.0 + 0.10 * duration
        label2 = "分离中（质量档·人声 Roformer）"
        ctx.log(f"{label2}：预计 ~{int(eta2)}s")
        model_dir = Path(__file__).resolve().parents[2] / "vendor" / "audio-separator-models"
        raw_sep.mkdir(parents=True, exist_ok=True)
        rc2, tail2, elapsed2 = _exec_child(
            ctx, [str(py), "-c", _SEP_ENTRY, str(src), "-m", "vocals_mel_band_roformer.ckpt",
                  "--output_dir", str(raw_sep), "--output_format=WAV"],
            label=label2, eta_sec=eta2,
            env_extra={"AUDIO_SEPARATOR_MODEL_DIR": str(model_dir)}, on_cancel=_cleanup)
        if rc2 != 0:
            raise RuntimeError(_sep_failure_message(rc2, tail2, src))
        ctx.check_cancel()
        ctx.log(f"{label2}完成：实耗 {elapsed2:.1f}s")
        vocal_files = sorted(raw_sep.glob("*(vocals)*.wav")) if raw_sep.is_dir() else []
        if len(vocal_files) != 1:
            raise RuntimeError(f"Roformer 未按预期产出 vocals（{raw_sep} 命中 {len(vocal_files)}）\n"
                               + "\n".join(tail2[-20:]))
        vocals_override = vocal_files[0]

    # 整理：demucs 四轨 → stems/；vocals 优先用 Roformer 版
    out_root = raw_dir / demucs_model
    track_dirs = sorted(p for p in out_root.glob("*") if p.is_dir()) if out_root.is_dir() else []
    if len(track_dirs) != 1:
        raise RuntimeError(
            f"demucs 未按预期产出（{out_root} 下 {len(track_dirs)} 个目录）\n"
            + "\n".join(tail1[-20:]))
    track_dir = track_dirs[0]
    stems: dict = {}
    for stem in ("bass", "drums", "other"):
        f = track_dir / f"{stem}.wav"
        if not f.is_file():
            raise RuntimeError(f"demucs 缺产物：{f}")
        dest = stems_dir / f"{stem}.wav"
        shutil.move(str(f), str(dest))
        stems[stem] = {"file": f"stems/{stem}.wav", "bytes": dest.stat().st_size}
    vocals_src = vocals_override if vocals_override is not None else track_dir / "vocals.wav"
    if not vocals_src.is_file():
        raise RuntimeError(f"缺 vocals 产物：{vocals_src}")
    dest = stems_dir / "vocals.wav"
    shutil.move(str(vocals_src), str(dest))
    stems["vocals"] = {"file": "stems/vocals.wav", "bytes": dest.stat().st_size}
    shutil.rmtree(raw_dir, ignore_errors=True)
    shutil.rmtree(raw_sep, ignore_errors=True)
    stems = {k: stems[k] for k in _SEP_STEMS}

    models_desc = "htdemucs" if quality == "fast" else "htdemucs_ft+vocals_mel_band_roformer"
    if quality == "quality" and degraded:
        models_desc = "htdemucs_ft"
    elapsed_total = round(elapsed1 + elapsed2, 2)
    manifest = {
        "source": src.name, "quality": quality, "model": models_desc,
        "degraded": degraded, "degrade_reason": degrade_reason or None,
        "elapsed_sec": elapsed_total, "duration_sec": round(duration, 3),
        "stems": stems,
    }
    (stems_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    ctx.log(f"分离完成：实耗 {elapsed_total:.1f}s" + ("（已降级：纯 ft）" if degraded else ""))
    return {"artifact": "stems/manifest.json",
            "stems": {k: v["file"] for k, v in stems.items()},
            "quality": quality, "model": models_desc, "degraded": degraded,
            "elapsed_sec": elapsed_total, "duration_sec": round(duration, 3)}


def _run_denoise(ctx: ChainContext) -> dict:
    """① 降噪：源音频 → 16k 单声道转码（00）+ noisereduce 谱门降噪（01）。"""
    from ..dsp.preprocess import denoise_wav, probe_duration, transcode_to_wav

    ctx.check_cancel()
    src = ctx.source_audio
    wav16 = ctx.artifact("00-source-16k.wav")
    transcode_to_wav(src, wav16, sr=16000)
    ctx.check_cancel()
    strength = float(ctx.params.get("strength", 0.8))
    out = ctx.artifact("01-denoised.wav")
    denoise_wav(wav16, out, strength=strength)
    return {"artifact": out.name, "duration_sec": round(probe_duration(out), 3),
            "sample_rate": 16000, "strength": strength, "reference": wav16.name}


def _run_loudnorm(ctx: ChainContext) -> dict:
    """② 响度：ffmpeg loudnorm（EBU R128；目标 -16 LUFS / 峰值 -1 dB）。"""
    from ..dsp.preprocess import probe_duration

    ctx.check_cancel()
    src = ctx.prev or ctx.artifact("01-denoised.wav")
    target_lufs = float(ctx.params.get("target_lufs", -16.0))
    true_peak = float(ctx.params.get("true_peak_db", -1.0))
    out = ctx.artifact("02-loudnorm.wav")
    _ffmpeg(["-i", str(src),
             "-af", f"loudnorm=I={target_lufs}:TP={true_peak}:LRA=11",
             "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(out)])
    return {"artifact": out.name, "duration_sec": round(probe_duration(out), 3),
            "target_lufs": target_lufs, "true_peak_db": true_peak}


def _wav_peak_dbfs(path) -> float:
    """输入 wav 峰值（dBFS；满幅=0）——近静音判定用（E1-8 负例口径）。"""
    import numpy as np
    import soundfile as sf

    data, _ = sf.read(str(path), dtype="float32", always_2d=True)
    peak = float(np.abs(data).max()) if data.size else 0.0
    return float(20.0 * np.log10(max(peak, 1e-9)))


# 子进程形态后端（U1）：重模型转录走独立进程——可杀进程取消、崩了不拖垮服务
_SPAWN_BACKENDS = ("adtof", "mt3")


def _run_transcribe_spawn(ctx: ChainContext, backend: str, stem: str, src: Path,
                          out_name: str) -> dict:
    """转录·子进程形态（E 段2）：spawn 模块拉起 → 原子产物读回；取消＝杀进程树＋清半成品。"""
    import sys

    from ..dsp.preprocess import probe_duration

    root = Path(__file__).resolve().parents[2]
    out = ctx.artifact(out_name)
    params = {k: v for k, v in ctx.params.items() if k not in ("stem", "backend")}
    try:
        duration = float(probe_duration(src))
    except Exception:  # noqa: BLE001 时长仅用于 ETA——探测失败不拖累转录
        duration = 0.0
    eta = 8.0 + 0.05 * duration
    ctx.log(f"转录中（{backend}）：预计 ~{int(eta)}s（素材 {duration:.0f}s）")

    def _cleanup() -> None:
        for p in (out, out.with_name(out.name + ".tmp")):
            try:
                p.unlink()
            except OSError:
                pass

    cmd = [sys.executable, "-m", "tsov.dsp.backends.spawn", backend, str(src), str(out),
           json.dumps(params, ensure_ascii=False)]
    rc, tail, elapsed = _exec_child(
        ctx, cmd, label=f"转录中（{backend}）", eta_sec=eta,
        env_extra={"PYTHONPATH": str(root), "PYTHONUTF8": "1"}, on_cancel=_cleanup)
    if rc != 0 or not out.is_file():
        _cleanup()
        raise RuntimeError(f"转录失败（{backend}，rc={rc}）：\n" + "\n".join(tail[-20:]))
    data = json.loads(out.read_text(encoding="utf-8"))
    n_notes = len(data.get("notes") or [])
    ctx.log(f"转录完成（{backend}）：{n_notes} 音符，实耗 {elapsed:.1f}s")
    return {"artifact": out.name, "notes": n_notes, "bpm": float(data.get("bpm") or 0.0),
            "backend": backend, "params": params, "stem": stem, "empty": n_notes == 0}


def _run_transcribe(ctx: ChainContext) -> dict:
    """③ 转录：wav → Voice（默认 RMVPE；stem 指定声部轨时走拆解链输入）。

    stem=auto → 链式输入（prev/loudnorm）→ 03-voice.json（哼唱链逐字不变）；
    stem=<声部> → 输入 stems/<声部>.wav（本 run 缺→历史 run 回退）→ voice-<声部>.json。
    近静音输入（峰值 < -40dBFS）后端无产出 = 合法空输出（E1-8 负例）；有声无产出才报错。
    backend∈{adtof, mt3} 走子进程形态（U1：杀进程取消/崩溃隔离）→ _run_transcribe_spawn。
    """
    from ..dsp.transcribe import transcribe

    ctx.check_cancel()
    stem = str(ctx.params.get("stem") or "auto")
    if stem not in ("auto", "vocals", "drums", "bass", "other"):
        stem = "auto"
    if stem == "auto":
        src = ctx.prev or ctx.artifact("02-loudnorm.wav")
        out_name = "03-voice.json"
    else:
        found = _find_stem_wav(ctx, stem)
        if found is None:
            raise RuntimeError(
                f"找不到声部轨 stems/{stem}.wav（本 run 与历史 run 均无）——先跑「分离」步")
        src = found
        out_name = f"voice-{stem}.json"
    backend = str(ctx.params.get("backend", "rmvpe") or "rmvpe")
    if backend in _SPAWN_BACKENDS:
        return _run_transcribe_spawn(ctx, backend, stem, Path(src), out_name)
    kw: dict = {}
    if backend == "rmvpe":
        kw = {
            "confidence_threshold": float(ctx.params.get("confidence_threshold", 0.4)),
            "min_note_ms": float(ctx.params.get("min_note_ms", 60.0)),
            "pitch_jump_semitones": float(ctx.params.get("pitch_jump_semitones", 0.5)),
        }
    ctx.log(f"转录中（{backend}）——秒级步骤，请稍候")
    try:
        voice = transcribe(str(src), backend=backend, **kw)
    except RuntimeError as e:
        if "未产出" not in str(e):
            raise
        peak = _wav_peak_dbfs(src)
        if peak > -40.0:
            raise
        ctx.log(f"输入近静音（峰值 {peak:.1f} dBFS）——按空输出处理")
        from ..core.notes import Voice as _Voice
        voice = _Voice(notes=[], bpm=120.0, source_audio=str(src), backend=backend)
    ctx.check_cancel()
    out = ctx.artifact(out_name)
    out.write_text(json.dumps(voice.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return {"artifact": out.name, "notes": len(voice.notes), "bpm": float(voice.bpm),
            "backend": backend, "params": kw, "stem": stem,
            "empty": len(voice.notes) == 0}


def _run_quantize(ctx: ChainContext) -> dict:
    """④ 量化：音符对齐网格（网格跟随 BPM；strength=100% 全吸 / 50% 半拉）。

    网格来源：bpm ＝步参数 > grid.json > 音符文件 > 工程 tempo；相位＝grid.json（缺→0）。
    """
    from dataclasses import replace

    from ..dsp.quantize import quantize_notes

    ctx.check_cancel()
    src = ctx.prev or ctx.artifact("03-voice.json")
    notes, bpm = _load_notes(src)
    _tag = _stem_tag_from_voice(Path(src).name)
    out_name = f"notes-{_tag}-quantized.json" if _tag else "04-notes-quantized.json"
    grid = int(ctx.params.get("grid", 16))
    strength = max(0.0, min(1.0, float(ctx.params.get("strength", 1.0))))

    # BPM 优先级：步参数 > grid.json > 音符文件 > 工程 tempo
    param_bpm = _optional_float(ctx.params.get("bpm"))
    grid_data = _find_grid_json(ctx)
    grid_bpm = _optional_float((grid_data or {}).get("bpm"))
    if param_bpm is not None and param_bpm > 0:
        use_bpm, bpm_source = param_bpm, "param"
    elif grid_bpm is not None and grid_bpm > 0:
        use_bpm, bpm_source = grid_bpm, "grid"
    elif bpm:
        use_bpm, bpm_source = float(bpm), "voice"
    else:
        use_bpm, bpm_source = float(ctx.score_tempo or 120.0), "project"

    # 相位：grid.json 的 beat_offset；bpm 被步参数覆盖时按 beats 以新周期重算（相位与周期配套）
    phase = 0.0
    if grid_data is not None:
        if bpm_source == "param" and (grid_bpm is None or abs(use_bpm - grid_bpm) > 1e-9):
            beats = grid_data.get("beats")
            phase = _phase_from_beats(beats, use_bpm) if isinstance(beats, list) and beats else 0.0
        else:
            phase = _optional_float(grid_data.get("beat_offset")) or 0.0

    q = quantize_notes(notes, bpm=use_bpm, grid=grid, phase_s=phase)
    if strength < 1.0:  # 半拉：在原位置与量化点之间插值（保时值轮廓）
        q = [replace(a, start=round(a0.start + (a.start - a0.start) * strength, 6),
                     end=round(a0.end + (a.end - a0.end) * strength, 6))
             for a0, a in zip(notes, q)]
    out = _dump_notes(ctx.artifact(out_name), q)
    return {"artifact": out.name, "notes": len(q), "grid": grid, "strength": strength,
            "bpm": use_bpm, "bpm_source": bpm_source, "phase_s": round(phase, 6)}


def _run_snap_scale(ctx: ChainContext) -> dict:
    """⑤ 调内吸附：调外且 |dev|≥ 阈值的音吸到最近调内音 → 05-notes-snapped.json。"""
    from ..core.snap import snap_out_of_key

    ctx.check_cancel()
    src = ctx.prev or ctx.artifact("04-notes-quantized.json")
    notes, _bpm = _load_notes(src)
    _tag = _stem_tag_from_quantized(Path(src).name)
    out_name = f"notes-{_tag}-snapped.json" if _tag else "05-notes-snapped.json"
    raw_key = str(ctx.params.get("key", "auto") or "auto")
    key = None if raw_key.lower() in ("auto", "", "none") else raw_key
    threshold = float(ctx.params.get("threshold_cents", 42.0))
    snapped, stats = snap_out_of_key(notes, key=key, threshold_cents=threshold)
    out = _dump_notes(ctx.artifact(out_name), snapped)
    return {"artifact": out.name, "notes": len(snapped), "snapped": stats["snapped"],
            "key": stats["key"], "threshold_cents": threshold}


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

TOOLS: dict[str, dict] = {
    "analyze_grid": {
        "id": "analyze_grid", "tier": "fast",
        "name": {"zh": "定速与对齐", "en": "Tempo & grid"},
        "desc": {"zh": "源曲粗检 BPM＋拍点相位 → 网格参考（恒定速度假设）",
                 "en": "Detect BPM & beat phase → grid reference"},
        "params": {
            "bpm_override": {"type": "text", "default": "",
                             "label": {"zh": "BPM 覆盖（空=自动检测）", "en": "BPM override"}},
            "offset_ms": {"type": "number", "default": 0.0, "min": -500.0, "max": 500.0,
                          "step": 1.0, "label": {"zh": "相位微调 ms", "en": "Phase offset ms"}},
        },
        "run": _run_analyze_grid,
    },
    "separate": {
        "id": "separate", "tier": "slow",
        "name": {"zh": "分离", "en": "Separate"},
        "desc": {"zh": "源曲 → 人声/鼓/贝斯/其他 四轨（快档 demucs；秒级步骤）",
                 "en": "Split track into 4 stems (demucs)"},
        "params": {
            "quality": {"type": "select", "default": "quality", "options": ["fast", "quality"],
                        "label": {"zh": "档位（quality＝质量档）", "en": "Quality"}},
        },
        "run": _run_separate,
    },
    "denoise": {
        "id": "denoise", "tier": "fast",
        "name": {"zh": "降噪", "en": "Denoise"},
        "desc": {"zh": "noisereduce 谱门降噪（含 16k 转码；强度越大降得越狠）",
                 "en": "noisereduce spectral gating (with 16k transcode)"},
        "params": {
            "strength": {"type": "number", "default": 0.8, "min": 0.0, "max": 1.0,
                         "step": 0.05, "label": {"zh": "强度", "en": "Strength"}},
        },
        "run": _run_denoise,
    },
    "loudnorm": {
        "id": "loudnorm", "tier": "fast",
        "name": {"zh": "响度", "en": "Loudness"},
        "desc": {"zh": "ffmpeg loudnorm（EBU R128 响度归一）",
                 "en": "ffmpeg loudnorm (EBU R128)"},
        "params": {
            "target_lufs": {"type": "number", "default": -16.0, "min": -30.0, "max": -8.0,
                            "step": 0.5, "label": {"zh": "目标响度 LUFS", "en": "Target LUFS"}},
            "true_peak_db": {"type": "number", "default": -1.0, "min": -6.0, "max": 0.0,
                             "step": 0.5, "label": {"zh": "峰值上限 dB", "en": "True peak dB"}},
        },
        "run": _run_loudnorm,
    },
    "transcribe": {
        "id": "transcribe", "tier": "slow",
        "name": {"zh": "转录", "en": "Transcribe"},
        "desc": {"zh": "音频 → 音符（哼唱默认 RMVPE；成品歌 GAME；鼓 ADTOF；多器乐 MT3）",
                 "en": "Audio to notes (RMVPE / GAME / ADTOF drums / MT3 multi)"},
        "params": {
            "backend": {"type": "select", "default": "rmvpe",
                        "options": ["rmvpe", "game", "adtof", "mt3"],
                        "label": {"zh": "后端", "en": "Backend"}},
            "stem": {"type": "select", "default": "auto",
                     "options": ["auto", "vocals", "drums", "bass", "other"],
                     "label": {"zh": "声部（auto=链式输入）", "en": "Stem (auto)"}},
            "pitch_jump_semitones": {"type": "number", "default": 0.5, "min": 0.2, "max": 1.5,
                                     "step": 0.1, "label": {"zh": "音高跳变阈值", "en": "Pitch jump"}},
            "min_note_ms": {"type": "number", "default": 60.0, "min": 30.0, "max": 300.0,
                            "step": 10.0, "label": {"zh": "最短音 ms", "en": "Min note ms"}},
            "confidence_threshold": {"type": "number", "default": 0.4, "min": 0.1, "max": 0.9,
                                     "step": 0.05, "label": {"zh": "置信度阈值", "en": "Confidence"}},
        },
        "run": _run_transcribe,
    },
    "quantize": {
        "id": "quantize", "tier": "fast",
        "name": {"zh": "量化", "en": "Quantize"},
        "desc": {"zh": "音符对齐节拍网格（网格跟随 BPM；强度=全吸/半拉）",
                 "en": "Quantize notes to beat grid"},
        "params": {
            "grid": {"type": "select", "default": 16,
                     "options": [4, 8, 16, 32,
                                 {"value": 3, "label": {"zh": "1/8 三连", "en": "1/8 triplet"}},
                                 {"value": 6, "label": {"zh": "1/16 三连", "en": "1/16 triplet"}}],
                     "label": {"zh": "网格", "en": "Grid"}},
            "strength": {"type": "number", "default": 1.0, "min": 0.0, "max": 1.0,
                         "step": 0.05, "label": {"zh": "强度", "en": "Strength"}},
            "bpm": {"type": "text", "default": "",
                    "label": {"zh": "BPM 覆盖（空=自动：网格>转录>工程）", "en": "BPM override"}},
        },
        "run": _run_quantize,
    },
    "snap_scale": {
        "id": "snap_scale", "tier": "fast",
        "name": {"zh": "调内吸附", "en": "Scale snap"},
        "desc": {"zh": "调外音吸附回调内（自动检测调性；微偏差才吸）",
                 "en": "Snap out-of-key notes back into scale"},
        "params": {
            "key": {"type": "text", "default": "auto",
                    "label": {"zh": "调性（auto=自动检测）", "en": "Key (auto)"}},
            "threshold_cents": {"type": "number", "default": 42.0, "min": 10.0, "max": 80.0,
                                "step": 1.0, "label": {"zh": "吸附阈值音分", "en": "Threshold cents"}},
        },
        "run": _run_snap_scale,
    },
}


def get_tool(tool_id: str) -> dict:
    """按 id 取工具定义；未知 id → KeyError。"""
    if tool_id not in TOOLS:
        raise KeyError(f"未知链工具：{tool_id!r}（可选：{', '.join(TOOLS)}）")
    return TOOLS[tool_id]


def list_tools() -> list[dict]:
    """工具清单（不含 run 函数；供 REST/前端渲染参数表单）。"""
    return [{k: v for k, v in t.items() if k != "run"} for t in TOOLS.values()]
