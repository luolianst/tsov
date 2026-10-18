"""HostSession：Score → 音轨图（ADR-0003 DAW 心智在宿主内的落地）。

- 每个 score Track → 一个 HostTrack（原始 Track + 实例化后的 SoundSource）
- 音轨图是宿主渲染/回放的输入；不改写 ADR-0005 schema，只做「实例化 + 排序 + 时长」视图
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from ..core.notes import Note
from ..core.score import Score, Track
from ..render.fluidsynth_backend import default_soundfont
from .audio_source import AudioClipSource
from .instrument import SoundSource, make_source


class _LazySource(SoundSource):
    """延迟实例化音源（P0 B1/B2）：缓存命中时完全不建 synth / 不拉 worker。

    首次 render / 显式属性访问才调用工厂；close 只关闭已创建的真源。
    """

    name = "lazy"
    streamable = True

    def __init__(self, factory):
        self._factory = factory
        self._src: SoundSource | None = None
        self._lock = threading.Lock()

    def _get(self) -> SoundSource:
        if self._src is None:
            with self._lock:
                if self._src is None:
                    self._src = self._factory()
        return self._src

    def render(self, notes, samplerate: int, n_frames: int):
        return self._get().render(notes, samplerate, n_frames)

    def close(self) -> None:
        src, self._src = self._src, None
        if src is not None:
            src.close()

    def __getattr__(self, item):
        # 实时流接口（note_on/note_off/pull/reset 等）→ 首次访问才实例化
        return getattr(self._get(), item)


@dataclass
class HostTrack:
    """音轨图节点：score 轨道 + 已实例化音源。"""

    track: Track
    source: SoundSource

    @property
    def notes(self) -> list[Note]:
        return self.track.notes


class HostSession:
    """宿主会话：Score → 音轨图。"""

    def __init__(self, score: Score, tracks: list[HostTrack], samplerate: int = 44100):
        self.score = score
        self.tracks = tracks
        self.samplerate = int(samplerate)

    @classmethod
    def from_score(
        cls,
        score: Score,
        soundfont: str | None = None,
        samplerate: int = 44100,
        base_dir=None,
        lazy: bool = True,
    ) -> "HostSession":
        """Score → 宿主会话：为每条轨道实例化音源。

        - 普通轨：``make_source(instrument.program)``（sf2；打击乐自动 channel 9）
        - 音频轨（E2/E6，kind=="audio"）：``AudioClipSource``（多 clip）——``base_dir``（工程根）
          解析各 clip 的 ``file`` 相对路径（旧 ``{file, offset}`` 形态读时迁移）；
          缺 base_dir / 缺 file / 文件不存在 → 明确报错
        - soundfont 缺省取 render 层默认（vendor/soundfonts/FluidR3_GM.sf2）
        """
        soundfont = soundfont or default_soundfont()
        if not soundfont:
            raise RuntimeError("未找到 SoundFont——先下载到 vendor/soundfonts/FluidR3_GM.sf2（见 docs/ADR-0008）")
        tracks: list[HostTrack] = []
        for track in score.tracks:
            if str(getattr(track, "kind", "midi") or "midi") == "audio":
                audio_meta = getattr(track, "audio", None) or {}
                clips_norm = track.audio_clips()
                if not clips_norm and "clips" not in audio_meta:
                    raise ValueError(f"音频轨 {track.name!r} 缺 audio.file")
                if clips_norm:
                    for cl in clips_norm:
                        rel = str(cl.get("file") or "")
                        if not rel:
                            raise ValueError(f"音频轨 {track.name!r} 缺 audio.file")
                        src_path = Path(rel)
                        if src_path.is_absolute() or ".." in src_path.parts:
                            raise ValueError(f"音频轨 {track.name!r} 的 audio.file 必须为工程内相对路径：{rel!r}")
                    if base_dir is None:
                        first_rel = str(clips_norm[0].get("file") or "")
                        raise ValueError(
                            f"音频轨 {track.name!r} 需要工程根来解析 {first_rel!r}——"
                            "请用 HostEngine.load(score, base_dir=…) 或 load_score(path)"
                        )
                    resolved = [{**cl, "path": Path(base_dir) / Path(str(cl["file"]))} for cl in clips_norm]
                else:
                    resolved = []
                source = AudioClipSource(clips=resolved, samplerate=samplerate)
            else:
                extra = {}
                if str(track.instrument.program or "").startswith("vst3:"):
                    extra = {  # 批L L1-3/L1-4：VST3 参数 diff / 预设引用（其余音源无此概念）
                        "params": dict(getattr(track.instrument, "params", {}) or {}),
                        "preset": getattr(track.instrument, "preset", None),
                        "base_dir": base_dir,
                    }
                factory = partial(make_source, track.instrument.program,
                                  soundfont=getattr(track.instrument, "soundfont", "") or soundfont,
                                  samplerate=samplerate, **extra)
                source = _LazySource(factory) if lazy else factory()
            tracks.append(HostTrack(track=track, source=source))
        return cls(score=score, tracks=tracks, samplerate=samplerate)

    @property
    def duration(self) -> float:
        """会话总时长 = max(音符尾, 音频轨 clip 尾〔offset + 文件时长〕)。"""
        ends = [float(n.end) for t in self.tracks for n in t.notes]
        for t in self.tracks:
            if not isinstance(t.source, AudioClipSource):
                continue   # 懒音源不要为了 probe clip_end 而实例化
            clip_end = getattr(t.source, "clip_end", None)
            if clip_end is not None:
                ends.append(float(clip_end))
        return max(ends) if ends else 0.0

    def close(self) -> None:
        for t in self.tracks:
            t.source.close()
