---
name: transcription-diagnosis
description: 转录疑点诊断（deviation_cents 解读/八度问题/弱起音/快速连音缺音），怀疑转错音先读
---

# 转录疑点诊断

## 字段速读（Note）
- `deviation_cents`：距最近半音的偏差（±50 为界）。>40 = 音准游离（滑音/误检候选）
- `confidence`：底座置信度。<0.5 的音优先怀疑
- 八度疑似：旋律整体比预期低/高 12 的倍数（GAME 对无词哼唱有此风险）

## 三问定位（先定性质再动手）
1. **个别错还是一段错？** 一段连续 → 分割/八度问题；个别 → 音准游离
2. **首尾对吗？** 首尾对、中段低 12 → 疑似 subharmonic；全段低 12 → 统一八度错
3. **降噪前后一致吗？** 一致 → 不是降噪问题

## 处理手段
- 个别游离音：人工标注 `pitch` 修正（**不要整体移调**）
- 快速连音粘连缺音：属分割参数问题 → 建议重跑转录（调 min_note_ms / pitch_jump），或标注 `add` 补音
- 弱起音漏检（头尾 1s 内）：标注 `add` 补上
- 修正后自查：音高集合 ⊆ 目标音阶、总音数合理、渲染试听

## 转谱链路（音频 → 可交付工程）
1. `transcribe`（成品歌=game / 纯哼唱=rmvpe）→ Voice JSON；
2. `voice_to_score`（Voice → Score 单轨钢琴，tempo 取 Voice.bpm；默认写 agent-edited-score.json）；
3. 自查修复：`load_score` 看 deviation/confidence → `detect_key` 定调 → `edit_score` 用 annotations 确定性修正；
   常用两条批：① 噪声短音（<55ms 且 velocity≤0.45）删除；② 调外且 |deviation_cents|≥42 → 吸到最近调内音；
4. 加效果：`apply_effect`（纯钢琴曲常用 piano-pop-reverb / piano-bright-hall）；
5. 交付：`export_audio`（mp3）。

## 纪律
- 规则补丁的输出要用光谱/独立模型验证声学事实，**不能因为「听起来接近原曲」就信**
- 「速度是否对齐」验证用 **onset 匹配相对原音频**（现场观众素材别用全带互相关，会失效）
