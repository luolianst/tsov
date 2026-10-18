"""轨模板（S4.2）：只存轨布局，不存音符。

模板是**全局**的：``presets/track-templates/*.json``。一条轨的布局 =
音源引用（program/soundfont/source_id）+ 混音（volume/pan/mute/solo/bus）+
效果链 + 文件夹归属；音符、Part、音频 clip 一律不入模板。

应用模板只产出命令层序列（``add_track`` / ``mount_source`` / ``set_instrument`` /
``set_track_mix`` / ``add_effect``），由 Project.apply_batch 执行——与 UI/agent 同径。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .core.atomic import write_json_atomic
from .core.score import Score, Track

_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "presets" / "track-templates"
_NAME_RE = re.compile(r"^[^\x00-\x1f/\\:*?\"<>|]{1,64}$")


def default_templates_path() -> Path:
    """本机模板库（TSOV_TEMPLATES_PATH 可覆盖；测试必须指到临时目录）。"""
    env = os.environ.get("TSOV_TEMPLATES_PATH")
    return Path(env) if env else _TEMPLATE_DIR


class TemplateError(ValueError):
    """模板非法/不存在/重名。"""


def _layout(track: Track) -> dict:
    inst = track.instrument
    return {
        "name": str(track.name or ""),
        "folder": str(track.folder or ""),
        "program": str(inst.program or ""),
        "soundfont": str(getattr(inst, "soundfont", "") or ""),
        "source_id": str(getattr(inst, "source_id", "") or ""),
        "volume": float(inst.volume),
        "pan": float(track.pan),
        "mute": bool(track.mute),
        "solo": bool(track.solo),
        "bus": str(track.bus or "master"),
        "effects": [e.to_dict() for e in (inst.effects or [])],
    }


@dataclass
class TrackTemplate:
    name: str
    title: str = ""
    tracks: list[dict] = field(default_factory=list)
    path: Path | None = None

    def to_dict(self) -> dict:
        return {"version": 1, "name": self.name, "title": self.title or self.name,
                "tracks": self.tracks}

    @classmethod
    def from_dict(cls, data: dict, *, path: Path | None = None) -> "TrackTemplate":
        name = str(data.get("name") or "").strip()
        if not name:
            raise TemplateError("模板缺 name")
        if not _NAME_RE.fullmatch(name):
            raise TemplateError(f"模板名非法：{name!r}（≤64，不能含路径分隔符）")
        tracks = data.get("tracks")
        if not isinstance(tracks, list) or not tracks:
            raise TemplateError(f"模板 {name} 的 tracks 为空")
        out: list[dict] = []
        for i, item in enumerate(tracks):
            if not isinstance(item, dict):
                raise TemplateError(f"模板 {name} tracks[{i}] 需为对象")
            program = str(item.get("program") or "")
            sid = str(item.get("source_id") or "")
            if not program and not sid:
                raise TemplateError(f"模板 {name} tracks[{i}] 缺 program/source_id")
            effects = item.get("effects") or []
            if not isinstance(effects, list):
                raise TemplateError(f"模板 {name} tracks[{i}] effects 需为数组")
            out.append({
                "name": str(item.get("name") or f"轨道 {i + 1}")[:64],
                "folder": str(item.get("folder") or "")[:64],
                "program": program,
                "soundfont": str(item.get("soundfont") or ""),
                "source_id": sid,
                "volume": float(item.get("volume", 1.0)),
                "pan": float(item.get("pan", 0.0)),
                "mute": bool(item.get("mute", False)),
                "solo": bool(item.get("solo", False)),
                "bus": str(item.get("bus") or "master"),
                "effects": [dict(e) for e in effects if isinstance(e, dict)],
            })
        return cls(name=name, title=str(data.get("title") or name), tracks=out, path=path)

    @classmethod
    def from_score(cls, name: str, score: Score, *, title: str = "") -> "TrackTemplate":
        tracks = [_layout(t) for t in score.tracks if str(getattr(t, "kind", "midi")) != "audio"]
        if not tracks:
            raise TemplateError("工程没有可入库的 MIDI 轨（音频轨不入模板）")
        return cls(name=name, title=title or name, tracks=tracks)

    def commands(self, *, start_index: int = 0) -> list[dict]:
        """生成把模板追加到 start_index 起的命令序列（不改已有轨）。"""
        commands: list[dict] = []
        for i, lay in enumerate(self.tracks):
            track = start_index + i
            add_value = {"name": lay.get("name") or f"轨道 {track + 1}"}
            if lay.get("folder"):
                add_value["folder"] = lay["folder"]
            commands.append({"op": "add_track", "value": add_value})
            sid = str(lay.get("source_id") or "")
            if sid:
                commands.append({"op": "mount_source", "track": track,
                                 "value": {"source_id": sid}})
            else:
                commands.append({"op": "set_instrument", "track": track,
                                 "value": {"program": str(lay.get("program") or ""),
                                           "soundfont": str(lay.get("soundfont") or "")}})
            commands.append({"op": "set_track_mix", "track": track, "value": {
                "volume": float(lay.get("volume", 1.0)),
                "pan": float(lay.get("pan", 0.0)),
                "mute": bool(lay.get("mute", False)),
                "solo": bool(lay.get("solo", False)),
                "bus": str(lay.get("bus") or "master"),
            }})
            for eff in lay.get("effects") or []:
                if isinstance(eff, dict) and eff.get("type"):
                    commands.append({"op": "add_effect", "track": track,
                                     "value": {"type": str(eff["type"]),
                                               "params": dict(eff.get("params") or {})}})
        return commands


class TrackTemplateLibrary:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root is not None else default_templates_path()
        self.errors: list[str] = []

    def _path(self, name: str) -> Path:
        if not _NAME_RE.fullmatch(str(name or "")):
            raise TemplateError(f"模板名非法：{name!r}")
        return self.root / f"{name}.json"

    def list(self) -> list[dict]:
        self.errors = []
        out: list[dict] = []
        if not self.root.is_dir():
            return out
        for p in sorted(self.root.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                tpl = TrackTemplate.from_dict(data, path=p)
                out.append({"name": tpl.name, "title": tpl.title,
                            "tracks": len(tpl.tracks)})
            except Exception as exc:
                self.errors.append(f"{p.name}: {exc}")
        return out

    def get(self, name: str) -> TrackTemplate:
        p = self._path(name)
        if not p.is_file():
            raise TemplateError(f"没有模板：{name}")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TemplateError(f"模板读取失败：{p.name}: {exc}") from exc
        return TrackTemplate.from_dict(data, path=p)

    def save(self, tpl: TrackTemplate, *, overwrite: bool = False) -> TrackTemplate:
        p = self._path(tpl.name)
        if p.exists() and not overwrite:
            raise TemplateError(f"模板已存在：{tpl.name}（overwrite=true 才覆盖）")
        self.root.mkdir(parents=True, exist_ok=True)
        write_json_atomic(p, tpl.to_dict(), indent=1)
        tpl.path = p
        return tpl

    def save_from_score(self, name: str, score: Score, *, title: str = "",
                        overwrite: bool = False) -> TrackTemplate:
        tpl = TrackTemplate.from_score(name, score, title=title)
        return self.save(tpl, overwrite=overwrite)
