---
name: from-scratch-compose
description: 空谱从零创作：先出结构段表、逐段写、段落复制填充、风格约束与自查；创作类请求先读
---

# 空谱从零创作（分结构段写）

## 1. 先出「结构段表」，不先写音
把整曲拆成结构段再动笔：段名（前奏 / 主歌 / 导歌 / 预副歌 / 副歌 / 间奏 / 桥段 / 独奏 / 尾奏）、
每段小节数、BPM、拍号、调性、编制（轨道清单 + 音色）。先把这张表交出来——方向对了再写音。

## 2. 风格约束（写之前先列，写作时对照）
- BPM / 拍号 / 调性（例：高速术力口 = 190-210 BPM、6/8 或 4/4、小调居多）；
- 编制：轨道清单 + 每轨音色——**只用 GM 标准名**（未知名会静默回落钢琴）；
- 律动：鼓型与节奏（快歌 = 8 分底鼓 + 反拍镲；6/8 = 附点律动）；贝斯跟底鼓走；
- 频段：低音别和贝斯打架；lead 与 pad 分层（pad 让位，别糊成一团）；
- 接缝：段与段之间留 1-2 拍过渡（鼓 break / 贝斯过门），不要硬切。

## 3. 逐段写作（一次一段）
- 每段铺写顺序：和弦/低音 → 旋律 → 鼓/律动；
- 音高落音阶内；级进为主、大跳 ≤ 五度；句末落主音/属音；
- 时值写满小节（按拍号对齐格数；6/8 每小节 12 个十六分格 = 6 个八分格）；
- 一段写完 → 渲染/自查 → 再进下一段（不要一口气铺满全曲）。

## 3.5 工具速查（按这个链路干）
1. `create_track`（program 用 GM 名：piano / synth_lead / synth_bass / pad / guitar_clean / guitar_muted / drums）建编制；
2. `write_notes` 写音：note = `{bar, grid, len, note|pitch_midi, velocity}`——bar 从 1 数，grid 是 16 分格序号（6/8 每小节 12 格，八分格 n = 16 分格 2n-1），**不要自己算秒数**；mode=replace 可整轨重写；
3. 鼓不用逐音写：`apply_pattern`（wotaiko_drums_base 主歌 / wotaiko_drums_energy 副歌，crash=true 加段头镲）；
4. 段落复制：`duplicate_bars`（省略 track = 全轨复制；复制后改和声/加花做「重复中的变化」）；
5. 音量平衡：`analyze_levels` 测各轨 RMS → `set_track_mix` 调 volume/pan（目标=同时发声窗口相对电平接近）；
6. 混响等：`apply_effect`（预设如 piano-pop-reverb / synth-lead / synth-bass / drum-bus-glue / guitar-electric-clean）；
7. 交付：`export_audio`（mp3 需 ffmpeg；渲染含效果链，立体声）+ `export_midi`（.mid）；
8. 拿不准调性：`detect_key`；要读预设/文档原文：`read_text`（仓库内文本文件）；
9. **`apply_effect` 的 track 必填**（缺省会误落到 track[0]）；总线（master）效果暂不支持，峰值靠各轨音量控制。

## 3.6 轨名 vs 乐器名（多轨编制必读，G5 实测教训）
- **轨名（name）＝声部角色；乐器（program）＝音色**，两者独立：多声部经常共用同一音色，靠轨名区分
  （例：弦乐组 = 多轨同 strings/harp，轨名 vn1 / vn2 / va / vc / cb）。
- 轨名是**引用锚点**（书签、重命名、删除都按轨名或索引定位）→ 起短名、保持唯一（重名会造成引用歧义，改名重名会被拒）。
- 编制表里「几轨同一乐器」= 多条 `create_track`、不同 name、可同 program；**别为了区分声部去发明 GM 表外的音色名**（会静默回落钢琴）。
- 轨管理：`rename_track`（track=轨名或索引；重名拒绝、书签引用同步）/ `remove_track`（track 必填；该轨书签一并清理）。

## 3.7 改速度的两种语义（2026-09 增）
- `set_tempo` 默认 **remap=true「跟速重排」**：改 BPM 同时把音符/书签/automation 等比缩放——音乐真的变快/变慢（DAW 习惯）；
- 只改谱面标注、音符不动：`set_tempo(..., remap=false)`；纯缩放、不动 BPM：`scale_time`（新时长 = 旧 × factor）。

## 4. 段落复制填充（省力且保证结构感）
后段与前段同构时，**复制前段再改**（换和声 / 加花 / 换配置），不要逐音重写——
流行曲的结构感就来自「重复中的变化」。

## 5. 禁项
- 不要清空已有谱（非空输入不允许清空，会被拒绝）；
- 不要发明 GM 表外的音色名；音高 0-127 / velocity 0-1 / start<end 不越界；
- 参考素材（乐句库）只做「选择 + 改写变奏」，不逐音照抄。

## 6. 自查
load_score 检查音高集合 ⊆ 音阶、各轨职责清晰 → render_wav 渲染试听 →
报告：结构段表 + 各段产物路径 + 未决问题。
