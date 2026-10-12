---
name: transcription-diagnosis
description: 当转录结果可疑（音不准/缺音/八度怪/粘连）或要跑「音频→工程」转谱链路时加载：三问定位、修正批、转谱五步。
---

# 转录疑点诊断

## 三问定位（先定性质，再动手）
1. **个别错还是一段错？** 一段连续 → 分割/八度问题；个别 → 音准游离
2. **首尾对吗？** 首尾对、中段低 12 → 疑似 subharmonic；全段低 12 → 统一八度错
3. **降噪前后一致吗？** 一致 → 不是降噪问题

## 转谱链路（音频 → 可交付工程）
1. `transcribe`（成品歌=game / 纯哼唱=rmvpe）→ Voice JSON
2. `voice_to_score`（Voice → Score 单轨钢琴，tempo 取 Voice.bpm）
3. 修正：`load_score` + `detect_key` → `edit_score` 标注批（阈值与理由见 references/cleanup-rules.md）
4. 效果：`apply_effect`（纯钢琴曲常用 piano-pop-reverb / piano-bright-hall）
5. 交付：`export_audio`（mp3）

## 深入页（按需用 read_text 读取——仓库内路径）
- `tsov/agent/skills/references/diagnostics.md` —— 字段速读 / 八度细节 / 处理手段细表 / 历史教训
- `tsov/agent/skills/references/cleanup-rules.md` —— 修正批阈值与理由 / 验证纪律
- `tsov/agent/skills/references/backends.md` —— game / rmvpe 选用差异与规模量级

## 纪律（硬性）
- 规则补丁的输出要用光谱/独立模型验证声学事实，**不能因为「听起来接近原曲」就信**；
- 「速度是否对齐」用 **onset 匹配相对原音频**（现场观众素材别用全带互相关，会失效）。
