"""HostSession：Score → 音轨图（ADR-0003 DAW 心智在宿主内的落地）。

- 每个 score Track → 一个 HostTrack（原始 Track + 实例化后的 SoundSource）
- 音轨图是宿主渲染/回放的输入；不改写 ADR-0005 schema，只做「实例化 + 排序 + 时长」视图
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.notes import Note
from ..core.score import Score, Track
from ..render.fluidsynth_backend import default_soundfont
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
    ) -> "HostSession":
        """Score → 宿主会话：为每条轨道实例化音源（第一版全部 sf2）。

        - soundfont 缺省取 render 层默认（vendor/soundfonts/FluidR3_GM.sf2）
        - 打击乐轨自动走 channel 9（SF2Source 内处理）
        """
        soundfont = soundfont or default_soundfont()
        if not soundfont:
            raise RuntimeError("未找到 SoundFont——先下载到 vendor/soundfonts/FluidR3_GM.sf2（见 docs/ADR-0008）")
        tracks: list[HostTrack] = []
        for track in score.tracks:
            source = make_source(
                track.instrument.program,
                soundfont=soundfont,
                samplerate=samplerate,
            )
            tracks.append(HostTrack(track=track, source=source))
        return cls(score=score, tracks=tracks, samplerate=samplerate)

    @property
    def duration(self) -> float:
        """会话总时长（所有轨最后音符 end 的最大值）。"""
        ends = [float(n.end) for t in self.tracks for n in t.notes]
        return max(ends) if ends else 0.0

    def close(self) -> None:
        for t in self.tracks:
            t.source.close()
