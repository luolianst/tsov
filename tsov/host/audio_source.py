"""音频素材音源（M-V8 E2 首刀 → E6 音频编辑刀）：工程音频文件 → 时间线摆样。

- ``AudioClipSource``：``render(notes, samplerate, n_frames)`` → (n,) float32——
  clip 列表按 ``start``（时间线秒）摆放入缓冲；``src_offset``/``src_len``（None=到文件尾）
  圈定源区间；``stretch``（时间线时长/源时长，>1=拉长放慢）经 librosa 保音高伸缩；
  ``fade_in``/``fade_out``（秒）线性包络；相邻重叠自动互补线性交叉（G3 默认）。
- 兼容 E2 旧签名：``AudioClipSource(path, offset=…)``（单 clip；测试/老调用面）。
- 文件 SR ≠ 目标 SR → ``resample_to`` 兜底重采样（入库已统一 44.1k，本路径为兼容/测试用）。
- ``streamable = False``：不参与实时流式通道（SynthStreamer 会拒绝 → 自动回退 MixStreamer）；
  播放走**渲染混入**（任务书 §9.1 C：一处集成，试听/宿主播放/导出全通）。
- 音频轨不入 stem 缓存（mix.py 按 isinstance 跳过）——每次直渲、永远最新；
  本实例内按文件做读缓存（同一会话多次渲染不重复解码）。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np


def load_mono(path, samplerate: int | None = None) -> tuple[np.ndarray, int]:
    """读音频文件 → (mono float32, 实际 SR)；``samplerate`` 给定时重采样到该 SR。"""
    import soundfile as sf

    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
    mono = np.ascontiguousarray(mono, dtype=np.float32)
    if samplerate is not None and int(sr) != int(samplerate):
        mono = resample_to(mono, int(sr), int(samplerate))
        sr = samplerate
    return mono, int(sr)


def load_stereo(path, samplerate: int | None = None) -> tuple[np.ndarray, int]:
    """读音频文件 → (stereo float32 (n,2), 实际 SR)；单声道复制、>2 声道取前两路。

    ``samplerate`` 给定时重采样到该 SR（resample_poly 按 axis=0；兜底逐通道线性）。
    """
    import soundfile as sf

    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    elif data.shape[1] > 2:
        data = data[:, :2]
    data = np.ascontiguousarray(data, dtype=np.float32)
    if samplerate is not None and int(sr) != int(samplerate):
        try:
            from math import gcd

            from scipy.signal import resample_poly

            g = gcd(int(sr), int(samplerate))
            data = resample_poly(data, int(samplerate) // g, int(sr) // g, axis=0).astype(np.float32)
        except Exception:  # noqa: BLE001 —— scipy 缺失/异常：逐通道线性兜底
            n_out = max(1, int(round(len(data) * int(samplerate) / int(sr))))
            x_old = np.linspace(0.0, 1.0, num=len(data), endpoint=False)
            x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
            out = np.empty((n_out, data.shape[1]), dtype=np.float32)
            for ch in range(data.shape[1]):
                out[:, ch] = np.interp(x_new, x_old, data[:, ch])
            data = out
        sr = samplerate
    return data, int(sr)


def resample_to(audio: np.ndarray, sr_from: int, sr_to: int) -> np.ndarray:
    """重采样（scipy resample_poly 优先；scipy 缺失时线性插值兜底）。"""
    audio = np.asarray(audio, dtype=np.float32)
    if sr_from == sr_to or audio.size == 0:
        return audio
    try:
        from math import gcd

        from scipy.signal import resample_poly

        g = gcd(int(sr_from), int(sr_to))
        return resample_poly(audio, sr_to // g, sr_from // g).astype(np.float32)
    except Exception:  # noqa: BLE001 —— scipy 缺失/异常：线性兜底
        n_out = max(1, int(round(len(audio) * sr_to / sr_from)))
        x_old = np.linspace(0.0, 1.0, num=len(audio), endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
        return np.interp(x_new, x_old, audio).astype(np.float32)


def time_stretch_preserve_pitch(seg: np.ndarray, stretch: float) -> np.ndarray:
    """保音高时间伸缩（E6）：librosa ``time_stretch``（rate=1/stretch）优先。

    支持 (n,) mono 与 (n,2) stereo（librosa 走多通道约定 (ch, n)）。
    失败（librosa 缺失等）→ 线性重采样兜底——**会变调**，仅防断链；调用方验收以 librosa 路径为准。
    """
    seg = np.asarray(seg, dtype=np.float32)
    if seg.size == 0 or abs(float(stretch) - 1.0) < 1e-9:
        return seg
    two_d = seg.ndim == 2
    try:
        import librosa

        y = seg.T if two_d else seg                      # (…, n) 约定
        out = np.asarray(librosa.effects.time_stretch(y, rate=1.0 / float(stretch)), dtype=np.float32)
        return out.T if two_d else out
    except Exception:  # noqa: BLE001 —— 兜底不静默吞语义：见 docstring
        if two_d:                                        # 逐通道线性兜底
            return np.stack([time_stretch_preserve_pitch(ch, stretch) for ch in seg.T], axis=1).astype(np.float32)
        n_out = max(1, int(round(len(seg) * float(stretch))))
        x_old = np.linspace(0.0, 1.0, num=len(seg), endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
        return np.interp(x_new, x_old, seg).astype(np.float32)


class AudioClipSource:
    """工程音频文件 → 时间线样本（多 clip：切片/去尾/淡入淡出/时间伸缩）。"""

    streamable = False

    def __init__(self, path=None, *, offset: float = 0.0, clips: list[dict] | None = None,
                 samplerate: int = 44100):
        """两种构造：

        - 旧（E2）：``AudioClipSource(path, offset=…)``——单 clip，直接给文件路径。
        - 新（E6）：``AudioClipSource(clips=[{path, start, src_offset, src_len, stretch, fade_in, fade_out}, …])``
          ——``path`` 须为**已解析的绝对路径**（session 层负责工程相对路径解析/护栏）。
        """
        items: list[dict] = []
        if clips is None:
            if path is None:
                raise ValueError("AudioClipSource 需 path 或 clips 之一")
            items.append(self._make_item(Path(path), start=max(0.0, float(offset or 0.0))))
        else:
            for c in clips:
                items.append(
                    self._make_item(
                        Path(str(c.get("path") or "")),
                        start=float(c.get("start") or 0.0),
                        src_offset=float(c.get("src_offset") or 0.0),
                        src_len=None if c.get("src_len") is None else float(c["src_len"]),
                        stretch=float(c["stretch"]) if c.get("stretch") is not None else 1.0,
                        fade_in=float(c.get("fade_in") or 0.0),
                        fade_out=float(c.get("fade_out") or 0.0),
                    )
                )
        self.clips = items
        self._cache: dict[tuple[str, int], np.ndarray] = {}
        # ---- 旧属性兼容（E2 调用面/测试：path / offset / file_samplerate / channels / clip_duration）----
        first = items[0] if items else None
        self.path = first["path"] if first else (Path(path) if path is not None else None)
        self.offset = max(0.0, float(offset or 0.0)) if clips is None else (first["start"] if first else 0.0)
        if first is not None:
            self.file_samplerate = int(first["fsr"])
            self.channels = int(first["channels"])
            self.clip_duration = float(first["dur"])
        else:
            self.file_samplerate = 0
            self.channels = 0
            self.clip_duration = 0.0
        self.samplerate = int(samplerate)  # 期待的目标 SR（渲染引擎）；render 以实参为准

    @staticmethod
    def _make_item(path: Path, *, start: float = 0.0, src_offset: float = 0.0,
                   src_len: float | None = None, stretch: float = 1.0,
                   fade_in: float = 0.0, fade_out: float = 0.0) -> dict:
        """单 clip 规范化 + 文件体检（不存在 → FileNotFoundError）。"""
        import soundfile as sf

        if not path.is_file():
            raise FileNotFoundError(f"音频文件不存在：{path}")
        info = sf.info(str(path))
        frames, fsr = int(info.frames), int(info.samplerate)
        return {
            "path": path,
            "start": max(0.0, float(start or 0.0)),
            "src_offset": max(0.0, float(src_offset or 0.0)),
            "src_len": src_len,
            "stretch": float(stretch or 1.0),
            "fade_in": max(0.0, float(fade_in or 0.0)),
            "fade_out": max(0.0, float(fade_out or 0.0)),
            "dur": (frames / fsr) if fsr else 0.0,
            "fsr": fsr,
            "channels": int(info.channels),
        }

    def _span_src(self, c: dict) -> float:
        """源区间秒数：src_len 给定时用它（并按文件截断）；否则「到文件尾」。"""
        avail = max(0.0, c["dur"] - c["src_offset"])
        if c["src_len"] is None:
            return avail
        return min(float(c["src_len"]), avail)

    @property
    def clip_end(self) -> float:
        """全部 clip 在时间线上的最大结束秒。"""
        end = 0.0
        for c in self.clips:
            end = max(end, c["start"] + self._span_src(c) * c["stretch"])
        return end

    def _load(self, path: Path, sr: int) -> np.ndarray:
        key = (str(path), int(sr))
        cached = self._cache.get(key)
        if cached is None:
            stereo, _ = load_stereo(path, samplerate=sr)
            cached = stereo
            self._cache[key] = cached
        return cached

    def render(self, notes, samplerate: int, n_frames: int) -> np.ndarray:  # noqa: ARG002 —— 音频轨无音符，notes 不参与
        """按 clip 列表摆放样本 → (n_frames, 2) float32（立体声保真；伸缩 + 淡化 + 重叠交叉）。"""
        sr = int(samplerate)
        out = np.zeros((int(n_frames), 2), dtype=np.float32)
        if not self.clips or n_frames <= 0:
            return out
        # 1) 逐 clip 取样 + 伸缩 → 段列表 [i0, arr, fade_in, fade_out, *重叠标记]
        segs: list[list] = []
        for c in self.clips:
            stereo = self._load(c["path"], sr)
            span = self._span_src(c)
            if span <= 0:
                continue
            a0 = int(round(c["src_offset"] * sr))
            a1 = min(len(stereo), a0 + int(round(span * sr)))
            seg = stereo[a0:a1]
            if seg.size == 0:
                continue
            if abs(c["stretch"] - 1.0) > 1e-9:
                seg = time_stretch_preserve_pitch(seg, c["stretch"])
            segs.append(
                [
                    int(round(c["start"] * sr)),
                    np.asarray(seg, dtype=np.float32),
                    float(c["fade_in"]),
                    float(c["fade_out"]),
                ]
            )
        if not segs:
            return out
        # 2) 相邻重叠 → 互补线性交叉（前段 ×(1−x)、后段 ×x；G3 默认）
        segs.sort(key=lambda s: s[0])
        for i in range(len(segs) - 1):
            i0a, da = segs[i][0], segs[i][1]
            i0b, db = segs[i + 1][0], segs[i + 1][1]
            ov0 = max(i0a, i0b)
            ov1 = min(i0a + len(da), i0b + len(db))
            if ov1 > ov0:
                segs[i].append((ov0, ov1, "out"))
                segs[i + 1].append((ov0, ov1, "in"))
        # 3) 包络（淡入淡出 + 交叉）→ 混入输出
        for s in segs:
            i0 = int(s[0])
            arr = np.array(s[1], dtype=np.float32, copy=True)
            env = np.ones(len(arr), dtype=np.float32)
            fi = min(int(round(float(s[2]) * sr)), len(arr))
            if fi > 0:
                env[:fi] *= np.linspace(0.0, 1.0, fi, dtype=np.float32)
            fo = min(int(round(float(s[3]) * sr)), len(arr))
            if fo > 0:
                env[-fo:] *= np.linspace(1.0, 0.0, fo, dtype=np.float32)
            for ov0, ov1, kind in s[4:]:
                lo = max(0, int(ov0) - i0)
                hi = min(len(arr), int(ov1) - i0)
                if hi <= lo:
                    continue
                x = np.linspace(0.0, 1.0, hi - lo, dtype=np.float32)
                env[lo:hi] *= (1.0 - x) if kind == "out" else x
            arr *= env[:, None]
            beg = max(0, i0)
            end = min(int(n_frames), i0 + len(arr))
            if end > beg:
                out[beg:end] += arr[beg - i0 : end - i0]
        return out

    def close(self) -> None:  # 与 SoundSource 协议一致（无资源可放）
        return None
