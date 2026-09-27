"""HostSession：Score → 音轨图（ADR-0003 DAW 心智在宿主内的落地）。

- 每个 score Track → 一个 HostTrack（原始 Track + 实例化后的 SoundSource）
- 音轨图是宿主渲染/回放的输入；不改写 ADR-0005 schema，只做「实例化 + 排序 + 时长」视图
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..core.notes import Note
from ..core.score import Score, Track
from ..render.fluidsynth_backend import default_soundfont
from .audio_source import AudioClipSource
from .instrument import SoundSource, make_source


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
                source = make_source(
                    track.instrument.program,
                    soundfont=soundfont,
                    samplerate=samplerate,
                )
            tracks.append(HostTrack(track=track, source=source))
        return cls(score=score, tracks=tracks, samplerate=samplerate)

    @property
    def duration(self) -> float:
        """会话总时长 = max(音符尾, 音频轨 clip 尾〔offset + 文件时长〕)。"""
        ends = [float(n.end) for t in self.tracks for n in t.notes]
        for t in self.tracks:
            clip_end = getattr(t.source, "clip_end", None)
            if clip_end is not None:
                ends.append(float(clip_end))
        return max(ends) if ends else 0.0

    def close(self) -> None:
        for t in self.tracks:
            t.source.close()
