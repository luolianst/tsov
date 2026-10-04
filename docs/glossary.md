# Glossary — tsov vocabulary, up close

**[简体中文](glossary.zh-CN.md) | English**

tsov has its own small vocabulary. This page maps the terms you'll meet in the **UI (Chinese-only in v0.1)**, in the docs, and in the code — with the closest DAW equivalent where one exists.

> New here? Read the [quickstart](../README.md#quickstart) first; come back when a button, a term, or a Chinese label needs decoding.

## The workspace

| Term | 中文 | Meaning | DAW equivalent |
|---|---|---|---|
| project | 工程 | A song folder — score, audio, edit history. One git repository per project. | project / session |
| track | 轨道 | One musician's line of music: MIDI notes or an audio clip. | track |
| score | 乐谱 | The note-level song data (tracks, instruments, tempo, key) — the editable truth every change lands in. | MIDI / piano-roll data |
| bus | 总线 | A group channel: several tracks feed in, its effect chain applies to the sum, then out to master. | group / bus |
| effect chain | 效果链 | An ordered rack of effects on a track or bus (built-ins + VST3). | FX chain |
| automation | 自动化 | Parameter curves over time (e.g. volume rising into a chorus). | automation lane |
| bookmark | 书签 | A named position or range you can jump back to — scoped to the project, a folder or a track. | marker / region |
| favorite | 收藏 | A named checkpoint of the whole project (backed by a git tag) — jump back any time; auto-created during long edit runs. | named versions / save-point |
| rollback | 回滚 | Time-travel through edit history — every action is snapshotted, nothing is lost. | undo history (stronger) |

## The humming quick-lane（哼唱快车道）

The one-click path from a hummed recording to editable notes. Every step is a small tool: visible, re-runnable, tweakable — run the whole chain, or a single step.

| Term | 中文 | Meaning |
|---|---|---|
| quick-lane | 哼唱快车道 | The pipeline panel (dock ▸ 链) that turns a humming take into clean notes. No standard DAW equivalent — think "a guided one-click pipeline". |
| run the chain | ▶ 运行全链 | Execute all five steps in order. Steps can also be re-run alone, or edited and re-applied. |
| denoise | 降噪 | Strip background noise (noisereduce, with a 16 kHz transcode). |
| loudness | 响度 | Level the take before transcription (EBU R128, default −16 LUFS). |
| transcribe | 转录 | Audio → notes. RMVPE for humming; GAME for produced vocals. |
| quantize | 量化 | Pull note timings onto the beat grid (the grid follows the BPM). |
| scale snap | 调内吸附 | Gently nudge out-of-key notes back into the detected key. |
| convert to score | 转乐谱…（实验性） | Right-click an audio track (or use the record panel): attach the quick-lane to it and open the pipeline. |
| adopt | 采纳 | Accept a computed result into the project — as a real, undoable edit. |
| discard | 丢弃 | Reject it — nothing enters the project. |
| audition | 试听 | Preview a candidate before you decide. |

## Working with the agent

| Term | 中文 | Meaning |
|---|---|---|
| chat | 对话 | The panel where you work on the song with the LLM agent, in plain language. |
| agent session | 会话 | One conversation thread, bound to the project it's editing. |
| action card | 动作卡 | The receipt for an agent action: what it did, with a diff — and one-click undo. |
| staging | 暂存区 | The review tray (dock ▸ AI): results from the chain / AI tune / arrangement wait here until you adopt or discard them. |
| tune | 调参 | The AI mixing panel: measures the mix, suggests parameter changes, applies the ones you tick. |
| arrange | 配器 | The orchestration panel: generates accompaniment parts from pattern packs — preview, adopt, or reroll with feedback. |
| command layer | 命令层 | The single transactional path every edit goes through — UI clicks and agent tools alike. No side doors for the bot. Like a DAW's action/scripting layer, but mandatory for everyone. |
| EditBatch | — | The transaction unit: a batch of operations applied atomically, snapshotted, undoable. |

## Mixing & the rest

| Term | 中文 | Meaning |
|---|---|---|
| dock tabs | 通道 / 效果器 / 音源 / 混音台 / 录音 / 链 | Per-track settings, effect chains, instrument routing, the mixer, recording, and the quick-lane. |
| export | 导出 | Render the result: master, buses, stems, MIDI — pick what you need. |
| stems | 分轨 | One audio file per track, for handing the song to another tool. |
| metronome | 节拍器 | Click track. |
| loop | 循环 | A playback range that repeats. |
| follow-speed | 跟速 | When you change the tempo, the notes move with it (vs a notation-only change). |
| preset | 预设 | Ready-made effect chains & arrangement patterns, each with provenance notes. |

## Engines & instruments

| Term | Meaning |
|---|---|
| RMVPE | Transcription backend for humming (weights fetched separately — see [setup-engines.md](setup-engines.md)). |
| GAME | Transcription backend for produced vocals. |
| FluidSynth | MIDI rendering engine (GM soundfont). |
| program | A track's instrument: a GM instrument name, a `vst3:` plugin, or an `sfz:` sampler. |

---

*Found a term missing or mistranslated? That's the easiest first PR — or just tell us in an issue.*
