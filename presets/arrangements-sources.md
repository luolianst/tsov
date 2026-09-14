# 配器预设来源汇总（arrangements-sources）

本目录下每个 `*.json` 为一个配器预设：角色、GM program、音域、力度范围、相对电平、声像、节奏型说明。
所有预设的目标渲染环境：GM 音色库（FluidR3 GM soundfont，多轨 MIDI 渲染）。

## 全局约定（所有预设统一）

- **GM program 号**：采用官方 General MIDI Level 1 表的 **0–127** 计法（0 = Acoustic Grand Piano）。
  常被引用的 1–128 表编号 = 本编号 + 1（例如 1–128 表的 28 号 “Electric Guitar (clean)” = 本文件的 27）。来源：Wikipedia General MIDI。
- **GM 打击乐**：没有 program 号，MIDI 通道 10（该表述为 1 起数；程序内部 0-based channel 9）只能是打击乐；
  各鼓件用 note number 指定（如 kick=36、snare=38、closed hat=42、open hat=46、ride=51）。
- **音高记法**：科学音高记法（C4 = 中央 C = MIDI 60）。部分 DAW 显示 C3 = 中央 C，注意 ±1 八度换算。
- **pan**：-1.0（全左）～ +1.0（全右），0.0 居中。
- **level_hint_db**：单轨相对电平建议值，各预案里写明锚点（多数为“鼓组 = 0 dB”，抒情钢琴预案为“人声线 = 0 dB”）。
  这些数值为**经验值**（起点值），参考了下方 EDM 相对电平样例与声像/频率表，但不是任何单一文献的规定。
- **6/8 的 BPM 约定**（wotaiko-fast-6-8）：BPM 以附点四分音符计（每小节 2 个附点四分拍）；八分脉冲 = 3 × BPM。

## 来源清单

### 1. GM 规范与音色表
- https://en.wikipedia.org/wiki/General_MIDI — GM Level 1 program map（128 音色分组：0–7 钢琴、24–31 吉他、32–39 贝斯、80–87 synth lead、88–95 synth pad…）、Percussion Key Map（35–81 号 note 的 47 种打击乐）、通道 10 保留打击乐的规则、控制器 CC10 = pan。
- https://www.cs.cmu.edu/~music/cmp/archives/cmsip/readings/GMSpecs_PercMap.htm — GM Percussion Key Map（表格版，用于核对鼓件 note 号）。
- https://www.cs.cmu.edu/~music/cmp/archives/cmsip/readings/GMSpecs_Patches.htm — GM Instrument Patch Map（表格版）。
- https://musescore.org/sites/musescore.org/files/General%20MIDI%20Standard%20Percussion%20Set%20Key%20Map.pdf — GM 打击乐 key map（PDF 核对件）。
- https://www.polyphone.io/doc/files/RP-003_General_MIDI_System_Level_1_Specification_96-1-4_0.1.pdf — GM System Level 1 官方规范 PDF（RP-003）。

### 2. 混音频率 / EQ 对照
- https://www.izotope.com/community/blog/eq-cheat-sheet — 各乐器频率区间与问题区（本目录引用最多的一张表）：
  kick 60–80 冲击 / 100–200 knock / 200–500 箱感 / 1–5k 点击；snare 150–250 躯干 / 2–3.5k 攻击 / 8k+ air；
  hat 3–7k 刺耳区；bass 40–120 低端 / 120–250 躯干 / 300–500 mud / 800–1k presence；
  电吉他 100 以下可切 / 150–300 躯干 / 300–1.5k 主体 / 2–5k 侵略 / 3–6k 常削；
  钢琴 20–60 / 60–200 / 200–500 / 2–4k；人声 5–8k / 10k+ air；
  弦乐 100 以下可切 / 200–500 浑浊 / 1–4k / 10k+ air；铜管 150 以下可切 / 500–800 鼻音 / 1–5k。
- https://www.basnaudio.com/blogs/basn-blog/the-audio-frequency-spectrum-explained — 频段划分：sub-bass 20–60 / mid-bass 60–250 / low mids 250–500 / center mids 500–2k / upper mids 2–4k / presence 4–6 kHz。
- https://www.teachmeaudio.com/mixing/techniques/audio-spectrum — 经典频段表（bass 60–250、low midrange 250–500 等），与上一条互相印证。

### 3. 声像（Pan）
- https://audiospectra.net/instrument-panning-cheat-sheet/ — 声像默认值总表：kick/snare/bass/主唱居中（kick 与 bass 低于 100 Hz 无方向性、必须居中）；hat 15–30% 偏侧；
  双轨节奏吉他 80–100% 硬摆；单轨节奏吉他居中或 30% 偏侧；伴奏钢琴 20–40% 偏侧（钢琴独奏可居中）；
  木吉他节奏 30–50% 偏侧；pad 立体声铺满；铜管 20–40% 展开；弦乐按座位。
- https://www.izotope.com/community/blog/11-mixing-tips-for-panning-music-with-intention — LCR 思路与声像意图（补充参考）。
- https://www.masteringthemix.com/blogs/learn/guide-to-panning-and-stereo-width — LCR 声像方法（补充参考）。

### 4. 相对电平
- https://www.reddit.com/r/edmproduction/comments/s8ovk8/what_are_the_standard_volume_levels_of_key/ — 论坛转述的 EDM 常用起点：drums -8 dB / bass -10 dB / vocals -8 dB / pads-synths -10 dB（本目录以它的**比例关系**作相对电平的起点，绝对 dB 不直接采用）。
- https://www.soundonsound.com/techniques/5-biggest-mixing-mistakes-and-how-avoid-them — kick/bass 电平是最常见混音问题；混音平衡应以人声为参照。
- https://www.masteringthemix.com/blogs/learn/how-to-balance-all-the-elements-in-a-mix — 平衡思路（人声/鼓/音乐/贝斯的相对关系）。

### 5. 鼓型谱例 / 节奏
- https://www.youtube.com/watch?v=ELYU5_u7SzE — sightreaddrums《Basic 6/8 Drum Beats & Variations》：6/8 基础鼓型与变奏教学（wotaiko 预设的 6/8 骨架参考）。
- https://blog.bandlab.com/easy-drum-patterns/ — 8 类流派鼓型范式：rock（强调 2、4 拍 + kick/snare 骨架）、house（kick 每拍地板点、snare 2/4、8/16 分 hat、偶加 open hat）、trap（kick 1&4、snare 3）、hip hop（kick 1/3/4、snare 2/4、offbeat open hat）、jazz（ride 摆 8 分）等。
- https://www.drumeo.com/beat/how-to-read-drum-music/ — 鼓谱记号与网格阅读（辅助）。
- 注：本目录鼓型的**具体落点**（尤其 6/8 的 12 格方案）是以上范式 + 编曲经验加密而来，属**经验值**，不是某一份谱例的逐字转录。

### 6. 乐器音域
- https://compositionteaching.com/instrument-ranges/ — 管弦乐器音域对照表（concert pitch，middle C = C4）：Violin G3–E7、Viola C3–E6、Cello C2–C6、Double Bass E1–D4（发声音高）、Flute C4–D7、Oboe B♭3–G6、Clarinet B♭ D3–B♭6、Bassoon B♭1–E♭5、Horn in F B1–F5、Trumpet E3–C6、Trombone E2–F5、Tuba D1–F4、Harp C♭1–G♯7、Piano A0–C8、Timpani D2–A3。该站自述交叉核对了 Adler、Kennan & Grantham、Hugill、VSL、Philharmonia 等来源。
- https://www.orchestralibrary.com/reftables/rang.html — 管弦乐器音域备用核对表。
- https://joolsscott.co.uk/orchestration/ — 配器参考：各弦乐器的音色性格与音域（violin G3–A7 等，音域口径与该表略有出入，以 compositionteaching 表为准）。

### 7. 音色库（渲染环境）
- https://packages.debian.org/sid/fluid-soundfont-gm — “Fluid (R3) General MIDI SoundFont (GM)”，确认 FluidR3 是符合 GM 规范的 SoundFont。
- https://www.fluidsynth.org/wiki/SoundFont/ — FluidSynth 音色库说明（FluidR3 GM 140 MB 条目）。

## 各预设对应的主要来源

| 预设 | 主要来源 |
| --- | --- |
| wotaiko-fast-6-8 | GM 表（1）；iZotope EQ（2）；频段表（2）；audiospectra 声像（3）；Reddit EDM 电平比例（4）；sightreaddrums 6/8（5） |
| pop-band-standard | GM 表（1）；iZotope EQ（2）；audiospectra 声像（3）；BandLab 鼓型（5）；Reddit 电平比例（4） |
| rock-band | GM 表（1）；iZotope EQ（2）；audiospectra 声像（3）；BandLab 鼓型（5）；SOS 混音误区（4） |
| piano-ballad-duet | GM 表（1）；iZotope EQ（2）；audiospectra 声像（3）；音域表（6） |
| edm-electro-4-4 | GM 表（1）；BandLab house 范式（5）；iZotope EQ（2）；audiospectra 声像（3）；Reddit EDM 电平（4） |
| orchestral-basic | GM 表（1）；音域表（6）；Jools Scott 配器（6）；iZotope EQ（2）；audiospectra 声像（3） |

## 经验值声明（非文献来源的部分）

以下内容标注为「经验值」，来自通用编曲/混音实践，而非单一文献的直接数据：

1. wotaiko 预设的 6/8 高速鼓型 **12 格具体落点**（在常规 6/8 骨架 kick=八分格 1、4 / snare=八分格 4 / hat=六连八分 基础上加密）；fill 与副歌变型。
2. 所有预设的 `level_hint_db` **具体数值**与部分 pan 微调值（如吉他 -0.35、arp -0.3 之类的分配）。
3. 各风格 BPM 范围（190–210 / 100–132 / 110–160 / 58–80 / 122–132 / 60–120）。
4. synth bass 律动三型、吉他切分点、侧链挖槽量、build 手法、管弦乐音符重叠 10–20% 连奏法、定音鼓滚奏的 GM 近似写法。
5. 各种低切频点建议（如 pad 低切 150–200 Hz、吉他低切 100 Hz），依据是上表 iZotope 的“可切区”+ 实践微调。
6. 音色备选（如 pad 用 88/90/50 号）按 GM 表音色性格给出，音色实际观感在不同声音库会有差异。

## 遗留问题（供后续验证）

- **FluidR3 实际听感未验证**：本调研未执行渲染，有些 GM program 在 FluidR3 里的表现（如 47 Timpani 的响度、89 Pad 的音色厚度、119 Reverse Cymbal）需实测后微调电平。
- **6/8 BPM 计法**：需与作曲系统的音序器约定对齐（附点四分 vs 八分脉冲），否则速度会差 3 倍。
- **GM 音源的移调差异**：个别音源对 Contrabass（43）等音色默认降八度，实际使用需按听感校正音区。
- **相对电平仅给起点**：各预设的相对 dB 需要在实际混音里按整体响度校准（建议先渲染、再以鼓组/人声线为锚点统一）。
- **音域上下限**：管弦乐音域来自文献表，但 GM 采样在高/低极端的音质未逐一验证（如小提琴 E7、长笛 D7）。
