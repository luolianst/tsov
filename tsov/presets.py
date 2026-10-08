"""预设库：效果链预设 + 配器预设。

- 目录：仓库根 ``presets/effects/*.json``、``presets/arrangements/*.json``
- 效果链预设（effect preset）：可直接应用到某条轨（Instrument.effects）
- 配器预设（arrangement preset）：编制/音域/力度/相对电平/声像/节奏型，供从零作曲实例化轨道
- 校验：效果链逐条过 ``host.effect.validate_effect`` 且 ``EffectChain(strict=True)`` 可构造；
  配器预设检查必需字段
- ``apply_effect_preset``：返回**新 Score**（深拷贝，原对象不动）

CLI：``tsov presets list/show/apply-effect``
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path

from .core.score import Effect, Score

_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PRESET_DIR = _ROOT / "presets"


@dataclass
class EffectPreset:
    name: str
    title: str = ""
    description: str = ""
    targets: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    chain: list[Effect] = field(default_factory=list)
    notes: str = ""
    source: str = ""
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "EffectPreset":
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("效果预设缺 name")
        chain = [Effect(type=str(c["type"]), params=dict(c.get("params") or {})) for c in (data.get("chain") or [])]
        if not chain:
            raise ValueError(f"效果预设 {name} 的 chain 为空")
        return cls(
            name=name,
            title=str(data.get("title") or name),
            description=str(data.get("description") or ""),
            targets=list(data.get("targets") or []),
            tags=list(data.get("tags") or []),
            chain=chain,
            notes=str(data.get("notes") or ""),
            source=str(data.get("source") or ""),
            raw=dict(data),
        )

    def validate(self) -> list[str]:
        from .host.effect import EffectChain, validate_effect

        problems: list[str] = []
        for eff in self.chain:
            problems.extend(validate_effect(eff))
        if not problems:
            try:
                EffectChain(self.chain, strict=True)
            except Exception as e:  # 参数构造失败（越界/未支持）
                problems.append(f"链构造失败：{e}")
        return problems


@dataclass
class ArrangementPreset:
    name: str
    title: str = ""
    description: str = ""
    tempo_hint: dict = field(default_factory=dict)
    instruments: list[dict] = field(default_factory=list)
    mix_notes: str = ""
    sources: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "ArrangementPreset":
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("配器预设缺 name")
        instruments = list(data.get("instruments") or [])
        if not instruments:
            raise ValueError(f"配器预设 {name} 的 instruments 为空")
        for ins in instruments:
            if not ins.get("role"):
                raise ValueError(f"配器预设 {name} 有乐器缺 role")
        return cls(
            name=name,
            title=str(data.get("title") or name),
            description=str(data.get("description") or ""),
            tempo_hint=dict(data.get("tempo_hint") or {}),
            instruments=instruments,
            mix_notes=str(data.get("mix_notes") or ""),
            sources=list(data.get("sources") or []),
            raw=dict(data),
        )

    def validate(self) -> list[str]:
        problems: list[str] = []
        roles = [str(i.get("role")) for i in self.instruments]
        if len(roles) != len(set(roles)):
            problems.append("role 重复")
        for ins in self.instruments:
            prog = ins.get("gm_program")
            if prog is not None and not (0 <= int(prog) <= 127):
                problems.append(f"{ins.get('role')}: gm_program 越界 {prog}")
        return problems


class PresetLibrary:
    """预设库（按目录扫描加载；坏文件带文件名报错，不静默跳过）。"""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root is not None else DEFAULT_PRESET_DIR
        self.effects: dict[str, EffectPreset] = {}
        self.arrangements: dict[str, ArrangementPreset] = {}
        self.errors: list[str] = []

    # ---- 加载 ----

    def load(self) -> "PresetLibrary":
        self.effects, self.arrangements = {}, {}
        self.errors = []
        self._load_dir(self.root / "effects", EffectPreset, self.effects)
        self._load_dir(self.root / "arrangements", ArrangementPreset, self.arrangements)
        return self

    def _load_dir(self, d: Path, cls, target: dict) -> None:
        if not d.is_dir():
            return
        for path in sorted(d.glob("*.json")):
            if path.name.endswith(".patterns.json"):
                continue  # E4 段3：配器模式库（机读数据，tsov.arrange.library 装载）——非预设
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                preset = cls.from_dict(data)
                problems = preset.validate()
                if problems:
                    raise ValueError("；".join(problems))
                target[preset.name] = preset
            except Exception as e:
                self.errors.append(f"{path.name}: {e}")

    # ---- 查询 ----

    def get_effect(self, name: str) -> EffectPreset:
        if name not in self.effects:
            raise KeyError(f"没有效果预设：{name}（可用：{', '.join(sorted(self.effects)) or '无'}）")
        return self.effects[name]

    def get_arrangement(self, name: str) -> ArrangementPreset:
        if name not in self.arrangements:
            raise KeyError(f"没有配器预设：{name}（可用：{', '.join(sorted(self.arrangements)) or '无'}）")
        return self.arrangements[name]

    def list_names(self, kind: str | None = None) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        if kind in (None, "effects"):
            out["effects"] = sorted(self.effects)
        if kind in (None, "arrangements"):
            out["arrangements"] = sorted(self.arrangements)
        return out


def load_library(root: str | Path | None = None) -> PresetLibrary:
    return PresetLibrary(root).load()


def apply_effect_preset(
    score: Score,
    track: int | str | None = None,
    preset: EffectPreset | str = "",
    *,
    library: PresetLibrary | None = None,
    target: str = "track",
    ref: str | None = None,
) -> Score:
    """把效果预设应用到目标层（track / bus / master；缺省轨），返回新 Score。

    - target="track"：track 必填（索引或轨名）；目标轨效果链**整体替换**为该预设链；
    - target="bus"：ref = 总线名（须存在）；target="master"：作用于 score.master。
    （v0.2 批C 后段：P27——限幅等预设可挂 master/总线。）
    """
    if isinstance(preset, str):
        preset = (library or load_library()).get_effect(preset)

    new_score = copy.deepcopy(score)
    tgt = str(target or "track").strip() or "track"
    chain = [Effect(type=e.type, params=dict(e.params)) for e in preset.chain]
    if tgt == "track":
        idx: int | None = None
        if isinstance(track, int):
            idx = track
        elif track is not None:
            for i, tr in enumerate(score.tracks):
                if tr.name == track:
                    idx = i
                    break
        if idx is None or not (0 <= idx < len(score.tracks)):
            raise KeyError(f"找不到轨道：{track!r}（共 {len(score.tracks)} 条）")
        new_score.tracks[idx].instrument.effects = chain
        return new_score
    if tgt == "bus":
        name = str(ref or "").strip()
        bus = next((b for b in (new_score.buses or []) if b.name == name), None)
        if bus is None:
            known = [b.name for b in (new_score.buses or [])] + ["master"]
            raise KeyError(f"找不到总线：{name!r}（可用：{', '.join(known)}；master 用 target='master'）")
        bus.effects = chain
        return new_score
    if tgt == "master":
        new_score.master.effects = chain
        return new_score
    raise ValueError(f"未知 target：{target!r}（track / bus / master）")
