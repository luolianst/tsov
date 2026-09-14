# presets —— 预设库（效果链 + 配器）

tsov 预设库。加载入口：`tsov/presets.py`（`PresetLibrary` / `load_library`）。

## 目录约定

| 目录 | 内容 | 用途 |
|---|---|---|
| `presets/effects/*.json` | 效果链预设（EffectPreset） | `tsov presets apply-effect <score.json> <轨> <预设名>`；混音层直接渲染 |
| `presets/arrangements/*.json` | 配器预设（ArrangementPreset） | 从零作曲/配器实例化轨道（编制、音域、力度、相对电平、声像、节奏型） |

来源与参数依据：`effects-sources.md` / `arrangements-sources.md`（由调研子代理整理，含出处链接与映射说明）。

## 效果链预设格式

```json
{
  "name": "kebab-case",
  "title": "中文标题",
  "description": "场景说明",
  "targets": ["piano"], "tags": ["pop"],
  "chain": [
    {"type": "highpass", "params": {"cutoff_frequency_hz": 35}},
    {"type": "compressor", "params": {"threshold_db": -18, "ratio": 2, "attack_ms": 25, "release_ms": 150}},
    {"type": "reverb", "params": {"room_size": 0.75, "damping": 0.5, "wet_level": 0.2, "dry_level": 0.5, "width": 0.9}}
  ],
  "notes": "参数依据/映射说明"
}
```

- 支持类型与参数键：见 `tsov/host/effect.py` 的 `_PARAM_RANGES`（reverb/delay/compressor/chorus/distortion/gain/highpass/lowpass/limiter/brickwall/phaser）
- 参数越界会被钳制；未知参数键在加载时报错（预设校验走 `strict=True`）
- 坏文件不会静默跳过：`tsov presets list` 会打印警告、`PresetLibrary.errors` 可查

## 配器预设格式

```json
{
  "name": "wotaiko-fast-6-8",
  "title": "…", "description": "…",
  "tempo_hint": {"bpm_range": [190, 210], "meter": "6/8"},
  "instruments": [
    {"role": "piano", "gm_program": 0, "register": ["G3","C6"], "velocity_range": [64,112],
     "level_hint_db": -4, "pan": 0.0, "pattern_notes": "角色/节奏型说明"},
    {"role": "e_drums", "gm_program": null, "midi_channel": 10, "pattern_notes": "…"}
  ],
  "mix_notes": "频率分配/平衡要点",
  "sources": ["…"]
}
```

## 现有预设

- effects：piano-pop-reverb / piano-solo-intimate / piano-bright-hall / vocal-pop / vocal-acappella / guitar-acoustic / guitar-electric-clean / guitar-electric-dist / synth-lead / synth-pad / synth-bass / keys-rhodes-phase / drum-bus-glue / master-limiter
- arrangements：wotaiko-fast-6-8（高速术力口 6/8）/ pop-band-standard / rock-band / piano-ballad-duet / edm-electro-4-4 / orchestral-basic

> 预设参数为「标准链 + 免费 DSP（pedalboard 内置）」的映射近似（见各文件 `notes` 与 sources）；渲染后请以耳听微调。
