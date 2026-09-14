# 效果链预设 —— 来源与映射笔记

范围：本目录的 13 个预设 JSON 供「本地离线音频渲染（Python + pedalboard 内置效果器）」使用。
白名单类型与键名：reverb / delay / compressor / chorus / distortion / gain / highpass / lowpass / limiter / phaser。
产出目录：`output\_research\effect-presets\`

诚实声明（先说清楚边界）：
- 下面引用的教程给出的是**音乐描述与常见起点值**（如「衰减 2 s」「湿度 20%」「3:1 / attack 10 ms」），把
  它们搬到 pedalboard 的键上**全部是近似映射**，不是任何插件的官方推荐值。
- 凡引号内的数值都来自对应链接；凡标「经验值」「档位划分为经验值」的是本文档自己的取值判断，没有权威出处。
- pedalboard 自带效果器的**实测数据**（下一节）是我们自己跑出来的，脚本见文末附录，可复现。

---

## 1. 实测：pedalboard 0.9.25 的真实行为（本套参数决策的地基）

环境：`uv run --no-project --with pedalboard python`（pedalboard 0.9.25，2026-09 本机实测）。

### 1.1 各插件默认值（构造时不传参）

| 类型 | 默认值 |
| --- | --- |
| Reverb | room_size 0.5, damping 0.5, wet_level 0.33, dry_level 0.4, width 1.0, freeze_mode 0.0 |
| Delay | delay_seconds 0.5, feedback 0.0, mix 0.5 |
| Compressor | threshold_db 0.0, ratio 1.0, attack_ms 1.0, release_ms 100.0 |
| Chorus | rate_hz 1.0, depth 0.25, centre_delay_ms 7.0, feedback 0.0, mix 0.5 |
| Distortion | drive_db 25.0 |
| Gain | gain_db 1.0 |
| HighpassFilter / LowpassFilter | cutoff_frequency_hz 50.0 |
| Limiter | threshold_db -10.0, release_ms 100.0 |
| Phaser | rate_hz 1.0, depth 0.5, centre_frequency_hz 1300.0, feedback 0.0, mix 0.5 |

关键推论：**Reverb 的默认 wet 0.33 / dry 0.4 与 Freeverb 的调音一致**（官方文档原话：使用基于 FreeVerb
的技术与调音）。Freeverb 血统意味着：**它没有 decay 时间键，也没有 pre-delay 键**，衰减只能靠 room_size
（内部分配的反馈量），高频衰减靠 damping，预延时要另想办法。

### 1.2 room_size → 实际衰减时间（冲激响应 T30 外推，damping 0.5，湿声 100%）

| room_size | 0.5 | 0.55 | 0.6 | 0.65 | 0.7 | 0.75 | 0.8 | 0.85 | 0.9 | 0.95 | 1.0 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T30 (s) | 0.94 | 1.05 | 1.15 | 1.27 | 1.45 | 1.70 | 1.91 | 2.31 | 2.78 | 3.82 | 6.30 |

（room_size 0.3 / 0.4 量到 0.70 / 0.78 s。曲线在 0.9 以后陡增。）

→ **本套的「衰减时间 → room_size」映射就按这张表查**：1.0 s≈0.55、1.2 s≈0.62、1.3 s≈0.65、
1.5 s≈0.7、1.7 s≈0.75、2.3 s≈0.85、2.8 s≈0.9。

### 1.3 damping → 高频吸收（room_size 0.7 固定）

| damping | 0.0 | 0.2 | 0.4 | 0.5 | 0.6 | 0.8 | 1.0 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 全频 T30 (s) | 1.99 | 1.57 | 1.42 | 1.45 | 1.46 | 1.30 | 1.19 |
| >4 kHz 频带 T30 (s) | 2.04 | 1.33 | 1.09 | 1.03 | 0.91 | 0.79 | 0.65 |

→ damping 主要作用是**缩短高频拖尾**（0.0 时高频与全频一样长；1.0 时高频比全频短约一半），
即「明亮/暗淡」旋钮。damping 越大 → 混响越暖、越不刺——与 iZotope/NI 对 damping 的定义一致。

### 1.4 width → 立体声去相关

| width | 0.0 | 0.25 | 0.5 | 0.75 | 1.0 |
| --- | --- | --- | --- | --- | --- |
| L/R 相关系数 | 1.000 | 0.888 | 0.616 | 0.303 | 0.025 |

→ width 0=完全单声道、1=几乎完全去相关（最宽）。所以「小房间/单声道兼容」→ 0.5–0.7，「大厅/pad」→ 1.0。

### 1.5 两个必须知道的坑（实测）

1. **dry_level 是 2 倍线性增益**：`dry_level=0.5` → 干声增益 1.0（unity）；`1.0` → 2.0（+6 dB）；
   `0.4`（默认）→ 0.8。**本套统一取 dry_level 0.5 保持干声电平不变**，然后只用 wet_level 加湿度。
   （如果照抄别家插件的「dry=100%」写法，会凭空多出 6 dB。）
2. **越界行为不统一**：Reverb / Delay 的构造器对越界**直接抛 ValueError**
   （`ValueError: Room Size value must be between 0.0 and 1.0.`），而 Compressor（ratio/attack/release）、
   Chorus（depth/rate）、Phaser（rate/depth）、Limiter（threshold/release）、Gain、Distortion、HP/LP
   的构造器**不报错也不说会截断**。所以「值会被截断到合法范围」这个假设只对部分插件成立——
   本套所有 JSON 都严格落在白名单声明的范围内，不依赖上层截断。
3. Distortion 的官方定义是 `tanh(x * db_to_gain(drive_db))` —— **drive_db 是送进 tanh 之前的输入增益（dB）**，
   不是「失真度百分比」。默认 25 dB 已是明显失真；本套用到 3 / 6 / 8 / 18 dB。
4. Limiter 的官方说明：**threshold_db 决定「何时开始压制」，输出在 0 dBFS 硬削，且自动施加 makeup 增益
   （不可关闭）**。因此它不能当「可设定天花板」的砖墙限幅器用（见 master-limiter 的 notes）。

---

## 2. 来源清单（链接 + 取用要点）

### 2.1 混响（参数语义与「衰减/预延时/湿度」的数字）

| 来源 | 取用的内容 |
| --- | --- |
| iZotope《What is reverb?》 https://www.izotope.com/community/blog/what-is-reverb | pre-delay 的物理含义（直达声与早期反射的间隔，大房间天然更长）；decay = RT60（声压衰减 60 dB 的时间）；damping = 混响里的高频吸收（低 damping→高频衰减慢→更亮）；扩散度；干湿平衡；混响容易在不同元素间造成掩蔽/浑浊，建议在混响返回上做 HP/LP |
| iZotope《Reverb pre-delay explained》 https://www.izotope.com/community/blog/reverb-pre-delay | 预延时的用途（防糊、加深度）；**人声 20–80 ms、吉他 40–100 ms、鼓 5–50 ms** 的经验区间；示例里人声用了 72 ms；「预延时能给不同乐器不同的前后景」 |
| Native Instruments《How to use reverb》 https://blog.native-instruments.com/how-to-use-reverb/ | room/hall/plate 的区别；size 与 decay 是决定「空间大小感知」的两个最重要参数；**多数混音场景 wet ≤ 50%，而且应该比你以为的再往下压几 dB**；混响返回上切 ~400 Hz 以下与 ~10 kHz 以上；**kick 与 sub bass 应保持干声**（低频很少从混响获益） |
| Sound on Sound《Improving The Sound Of Your Reverb》(Paul White) https://www.soundonsound.com/techniques/improving-sound-your-reverb | 具体硬件参数范例：**Plate 2.7 s、无预延时、低通 5 kHz、HF damping 10 kHz**（该 patch 明确说适合钢琴与木吉他）；**Drum Room 3.0 s、无预延时**；**Choral Room 2.5 s + 20 ms 预延时 + 低通 3 kHz**（「用于合唱/伴唱而不压倒它们」）；**Tiled Room 1 s、diffusion/density 30%**；「真实厅堂在 5 kHz 以上几乎没有内容，反射声比干声更暖」 |
| reverb-calculator.com《Reverb for Piano》 https://reverb-calculator.com/blog/reverb-for-piano/ | 钢琴速查：独奏 2–4 s / 30–80 ms / 15–30%；古典 1.8–3.5 s / 25–60 ms / 10–25%；**流行抒情 1.5–3 s / 20–50 ms / 10–20%**；全编曲里的钢琴 0.8–2 s / 15–40 ms / 8–20% |
| reverb-calculator.com《Reverb for Guitar》 https://reverb-calculator.com/blog/reverb-for-guitar/ | 木吉他 1–2.5 s / 20–50 ms / 10–25%；清音电吉他 1–3 s / 20–60 ms / 10–25%；主音电吉他 1.5–3 s / 30–80 ms / 15–30%；**失真节奏吉他 0.5–1.5 s / 10–30 ms / 5–15%**（失真本身密度高，混响多会丢冲击） |

### 2.2 压缩（比例 / attack / release）

| 来源 | 取用的内容 |
| --- | --- |
| Music Guy Mixing《Best Compressor Settings for Vocals》 https://www.musicguymixing.com/compressor-settings-for-vocals/ | **人声 attack 从 10 ms 起**（响度跳动的「咬劲」来源）；**release 从 50 ms 起**；第一级用高比例（8:1）串行压缩属他的个人做法，本套未照抄（我们只有单级压缩，用 3:1） |
| audiospectra.net《Vocal Compression Attack and Release》 https://audiospectra.net/vocal-compression-attack-and-release-settings/ | **3:1 + 降到 3–6 dB 增益衰减** 的人声起点 |
| Sound on Sound 论坛（压缩参数讨论） https://www.soundonsound.com/forum/viewtopic.php?t=60281 | 旁白/人声 3:1、attack 3 ms、release 300 ms、约 3 dB 增益衰减（慢 release 的另一派取值） |
| masteringthemix《The Secret To Compressor Attack And Release Time》 https://www.masteringthemix.com/blogs/learn/the-secret-to-compressor-attack-and-release-time | 鼓/打击：中等 attack 10–30 ms、快 release 50–100 ms（保冲击）；人声：慢 attack 20–40 ms、中 release 100–200 ms；两个压缩器串行可更透明 |
| peak-studios.de（release 计算器说明） https://www.peak-studios.de/en/audio-kompressor-release-zeit-rechner/ | **release 建议 ≥50 ms**，更短容易产生不自然的失真与泵动（本套所有 release 都 ≥60 ms） |
| Waves《How to Use an SSL Buss Compressor》 https://www.waves.com/how-to-use-ssl-bus-compressor-classic-mix-glue | 总线压缩档位表（**Mix Buss 2:1 / 30 ms；Drum Buss 4:1 / 10 ms**）；慢 attack 10–30 ms 保冲击；fast attack 会削掉瞬态；总线只做 **1–4 dB** 增益衰减；常见起点「30 ms attack、0.1 release、2:1」；SSL 的魅力在克制 |
| Music Guy Mixing《How to Process Your Drum Bus Like the Pros》 https://www.musicguymixing.com/drum-bus/ | 鼓总线顺序 **EQ → 饱和 → 胶合压缩**；HP 20 Hz 起（24 dB/oct 可到 40 Hz）；总线压缩目标 **平均 1–2 dB、峰值 2–3 dB**；饱和档位 drive 1–3、mix 20–35% |

### 2.3 链路顺序与乐器/合成器

| 来源 | 取用的内容 |
| --- | --- |
| Andertons《Beginner's Guide to Guitar Pedals》 https://www.andertons.co.uk/beginner-guitar-pedal-guide ；Chaos Audio《Ultimate Guide to Guitar Signal Chain Order》 https://chaosaudio.com/blogs/whats-new/the-ultimate-guide-to-guitar-signal-chain-order ；Wampler《Reverb into delay, delay into reverb…》 https://www.wamplerpedals.com/blog/talking-about-gear/2019/08/signal-chain-reverb-into-delay-delay-into-reverb-or-something-else/ | 踏板常规顺序：动态/失真 → 调制 → 时基（延迟→混响）；多级失真「低增益在前」 |
| Wampler《Compressors for guitar – a simple guide》 https://www.wamplerpedals.com/blog/talking-about-gear/2019/08/compressors-for-guitar-a-simple-guide/ | 吉他压缩踏板的 attack 决定压掉多少瞬态（清音用较慢 attack 保拨弦冲击） |
| Ableton《Pad It Out: 10 Ways to Make Distinctive Pad Sounds》 https://www.ableton.com/en/blog/pad-it-out-10-ways-make-distinctive-pad-sounds/ | pad 靠 unison/调制把立体声图像撑开（「a pad can open up a mix by widening the stereo image」） |
| iZotope《6 Creative Reverb Techniques》 https://www.izotope.com/community/blog/6-creative-reverb-techniques-in-music-production | **长衰减混响可以把大多数音色变成 ambient pad**（synth-pad 取大空间的依据） |
| iZotope《7 tips for mixing the low end》 https://www.izotope.com/community/blog/7-tips-for-mixing-the-low-end | 低端清澈靠分层与低频元素保持干净（贝斯不加混响的旁证） |
| Sound on Sound《Mixing Bass》 https://www.soundonsound.com/techniques/mixing-bass | 低端「先塑形再控制」的思路；非正弦 sub 会低通处理 |
| Ghostnote《The Rap Vocal Chain, In Order》 https://ghostnotesupply.com/blogs/magazine/rap-vocal-chain-order-settings | 人声 tempo delay：**点八分（dotted 1/8）、feedback 20–30%**、HP 400–500 Hz / LP 5 kHz 的发送处理 |
| MixingGPT《Best Delay Plugins》 https://mixinggpt.com/blog/best-delay-plugins-2026 | delay 时间惯例：**pad 用 1/4、人声用 1/8 附点，feedback 25–35%** |
| startcue.io《Learn music production》 https://www.startcue.io/learn/ | 木吉他 **high-pass ~100 Hz**、200 Hz 附近小幅削减箱声（本套用 highpass 95 Hz 近似） |
| Gearspace《low pass on guitars - essential?》 https://gearspace.com/threads/low-pass-on-guitars-essential.486178/ ；Fractal Audio 用户群讨论 https://www.facebook.com/groups/fractalaudio/posts/2544504195745024/ | 吉他/箱体的高频处理惯例：低通 5–8 kHz 以模拟喇叭频响（也有 10–14 kHz 的更保守取值） |
| Peak-Studios / EDMProd《How To Use A Limiter》 https://www.edmprod.com/how-to-use-a-limiter/ | 母带天花板习惯值 -0.1 dB 以避免inter-sample 削波（本套受 pedalboard Limiter 限制，见下） |

### 2.4 参考的免费插件参数习惯（说明「常见值」的背景，未直接搬用）

- **TAL-Reverb-4 / Dragonfly Reverb**：Dragonfly 系列显式提供 predelay、decay、room size、width、damping
  （High Cut / Late Damp 等），也就是「一套常规混响参数」的典型构成
  （https://bedroomproducersblog.com/free-vst-plugins/reverb/ ，https://deepwiki.com/michaelwillis/dragonfly-reverb/4.2-room-reverb-guide ）。
  → 结论：**别人的插件有 decay/predelay 键，我们只有 room_size/damping**，这就是本套 notes 里反复出现的
  「预延时无键→用干湿比与 room_size 近似」的由来。我们没有抄录任何具体插件的预设数值。
- pedalboard 官方 API 文档 https://spotify.github.io/pedalboard/reference/pedalboard.html ：Distortion 的
  tanh 定义、Reverb 基于 FreeVerb、Limiter 的自动 makeup + 0 dBFS 硬削说明均出自此处。

---

## 3. 映射规则速查（音乐描述 → 我们的键）

| 音乐描述 | 我们的键 | 规则 / 近似 |
| --- | --- | --- |
| 衰减 1.5 s / 2 s / 2.5 s | `reverb.room_size` | 查 §1.2 实测表：1.5 s→0.70、1.7 s→0.75、2.3 s→0.85、2.8 s→0.9。**近似**：room_size 同时改变空间感与衰减 |
| 混响明亮 / 暗淡 | `reverb.damping` | 亮=低 damping（0.2–0.35），暖/闷=高（0.5–0.65）；实测它主要缩短高频拖尾 |
| 湿度 15% / 20% | `reverb.wet_level`（配 `dry_level=0.5`） | dry_level 0.5 = 干声 unity，wet_level w 对应线性能量比 w/(1+w)：0.13→12%、0.16→14%、0.18→15%、0.20→17%、0.22→18%、0.26→21%、0.35→26%。**近似**：这是线性振幅比，不等于插件 UI 的 mix 刻度 |
| 单声道/窄 / 宽立体声 | `reverb.width` | 实测：0.5→L/R 相关 0.62、0.75→0.30、1.0→0.03；小空间 0.6、大厅/pad 1.0 |
| 预延时 20–50 ms | （无对应键）`dry_level` + `room_size`；可选 `delay(delay_seconds≈0.02, feedback=0, mix≈0.15)` | 本库 Reverb 是 Freeverb 血统，无 pre-delay。保起音清晰靠「干声 unity + 适中 room_size + damping」；piano-bright-hall 里额外用一个 22 ms 无反馈低 mix 的 delay 做「早反射近似」——**这是近似，不是真 pre-delay** |
| 3:1 压缩 / 3–6 dB 增益衰减 | `compressor.ratio` / `threshold_db` | ratio 直接搬；threshold 与素材电平绑定，本套统一假定**输入峰值 ≈ -6 dBFS、平均 ≈ -20 dBFS**，换素材请整体平移 threshold |
| attack 10 ms / release 50–300 ms | `compressor.attack_ms` / `release_ms` | 直接搬；本套 release 全 ≥60 ms（低于 50 ms 易泵动） |
| 失真度 / 过载量 | `distortion.drive_db` | 官方定义 `tanh(x·db_to_gain(drive_db))`；档位（**经验值**）：3 dB 极轻饱和、6–8 dB 轻饱和、18 dB 中重、25 dB（默认）明显失真 |
| 音量补偿 / makeup | `gain.gain_db` | pedalboard 的 Compressor / Distortion 没有输出音量键，压缩与饱和造成的电平损失用 gain 节点补 |
| 点八分延迟 | `delay.delay_seconds` | 60/BPM×0.75：70→0.643、76→0.592、80→0.562、90→0.500、100→0.450、120→0.375 s；八分=60/BPM/2 |
| 延迟反馈 20–35% | `delay.feedback` | 直接搬（人声 0.22、lead 0.35） |
| EQ 低切 / 喇叭高切 | `highpass` / `lowpass` | 人声 75–90 Hz、木吉他 95 Hz、钢琴 35–45 Hz、吉他箱体高切 5 kHz、总线高切 25–30 Hz |
| 安全天花板 -1 dBTP | `limiter.threshold_db` | **不可精确实现**：pedalboard Limiter 的 threshold 是压制门槛且自动 makeup、0 dBFS 硬削；只能取 -1.5 dB 做兜底（真天花板需 BrickwallLimiter，不在白名单） |

---

## 4. 预设 → 依据索引

| 预设 | 主要依据 |
| --- | --- |
| `piano-pop-reverb` | reverb-calculator 流行抒情钢琴表（1.5–3 s / 10–20%）；实测 room_size 0.75≈1.70 s、damping 0.5 高频≈1.0 s；NI「wet 宁少勿多」；2:1 + 慢 attack 保槌击（masteringthemix） |
| `piano-solo-intimate` | iZotope/NI 的 room reverb 描述（短衰减、近反射）；实测 0.5≈0.94 s、damping 0.6 高频≈0.91 s；iZotope「混响返回做 HP」 |
| `piano-bright-hall` | reverb-calculator 独奏/古典钢琴（2–4 s、1.8–3.5 s）；实测 0.85≈2.31 s、damping 0.2 高频 1.33 s（亮）；iZotope 预延时 20–80 ms（用 22 ms delay 近似）；SOS 厅堂/板式的高频处理思路 |
| `vocal-pop` | 3:1 + 3–6 dB（audiospectra）；attack 10 ms / release 50 ms（Music Guy Mixing）；人声低切（mixingmonster/startcue）；点八分 delay + feedback 20–30%（Ghostnote、MixingGPT）；实测 room_size 0.6≈1.15 s |
| `vocal-acappella` | SOS「Choral Room」的中等衰减 + HF 吸收意图（2.5 s / 20 ms / LP 3 kHz，我们只有 room_size/damping）；peak-studios release ≥50 ms；实测 0.65≈1.27 s |
| `guitar-acoustic` | startcue 木吉他 HP ~100 Hz；reverb-calculator 木吉他 1–2.5 s / 10–25%；iZotope 吉他预延时 40–100 ms；SOS 板式 patch「适合钢琴与木吉他」 |
| `guitar-electric-clean` | 踏板顺序惯例（Andertons / Chaos Audio / Wampler）；Wampler 吉他压缩；reverb-calculator 清音电吉他 1–3 s / 10–25%；0.4 s = 75 BPM 八分 |
| `guitar-electric-dist` | pedalboard 官方 tanh 定义；Gearspace/Fractal 的吉他低通 5–8 kHz；reverb-calculator 失真节奏 0.5–1.5 s / 5–15% |
| `synth-lead` | iZotope 低切/分层；peak-studios release ≥50 ms；MixingGPT delay 惯例（1/8 附点、feedback 25–35%）；实测 0.6≈1.15 s |
| `synth-pad` | iZotope「长衰减→ambient pad」；NI hall 适合宽阔编排；Ableton「pad 靠调制撑宽」；实测 0.85≈2.31 s、width 1.0→相关 0.03 |
| `synth-bass` | NI「kick 与 sub bass 保持干声」；SOS《Mixing Bass》先塑形再控制；Pheek/Music Guy Mixing 的 20–30 Hz 低切起点；peak-studios release ≥50 ms |
| `drum-bus-glue` | Waves SSL 档位表（Drum Buss 4:1 / 10 ms，总线 1–4 dB GR）；Music Guy Mixing 鼓总线顺序 EQ→饱和→压缩、目标 1–2 dB、饱和 drive 1–3 / mix 20–35%；实测无 makeup 键→用 gain 补 |
| `master-limiter` | pedalboard 官方 Limiter 说明（自动 makeup + 0 dBFS 硬削）；Music Guy Mixing 20–30 Hz 低切；peak-studios release ≥50 ms；EDMProd 天花板惯例 -0.1 dB（说明我们的差距） |
| `keys-rhodes-phase` | 附加预设：phaser 数值为**经验取值**（无权威来源），以本库默认值为基准向更慢/更含蓄收缩；空间部分同 keys 类 |

---

## 5. 遗留不确定项（已知的不足）

1. **预延时不可实现**：本库 Reverb 无 pre-delay 键。`piano-bright-hall` 里用 `delay(0.022 s, fb 0, mix 0.15)`
   近似早反射，但它同时会让 15% 的信号整体后移，**不等于**真正的 reverb 预延时（真做法需要并行的 reverb 总线）。
2. **湿度百分比是线性近似**：`wet_level` 与来源教程里「10–20% wet」的刻度含义不同（后者多为插件 UI 的干湿推子），
   本套给出的是线性能量比换算，**听感需要渲染后试听校准**。
3. **threshold_db 依赖素材电平**：所有压缩/限幅阈值都假定输入峰值 ≈ -6 dBFS。素材电平不同时，压缩量会整体偏移。
4. **dry_level = 2× 是实测结论**：可能随 pedalboard 版本改变（本机 0.9.25）。若升版后发现整体音量偏大 6 dB，先查这里。
5. **失真不是音箱模拟**：`distortion.drive_db` 只是 tanh 饱和，没有前级/后级/箱体/麦克风任何一层；`guitar-electric-dist`
   用 lowpass 5 kHz 近似喇叭滚降，音色上限不高。
6. **没有 de-esser / 动态 EQ / 中频 EQ / 齿音控制**：人声、lead 的 1–5 kHz 处理能力缺失（白名单只有 HP/LP）。
7. **没有真正的砖墙限幅与 LUFS**：`master-limiter` 只能兜底，不能保证 -1 dBTP 或特定 LUFS；响度目标需上层工具。
8. **`keys-rhodes-phase` 的 phaser 数值无来源**：本套唯一「经验取值」占主导的预设，请在渲染后自行听判。
9. **延迟时间与歌曲 BPM 绑定**：`vocal-pop`（0.5 s）按 90 BPM、`guitar-electric-clean`（0.4 s）按 75 BPM 计算，
   `synth-lead`（0.375 s）按 80 BPM；换歌请按 §3 的公式重算。
10. **各效果的听感阈值未经 BPM/调性/素材实测**：本任务范围内只做了 JSON 与参数层验证，未做音频渲染试听。

---

## 附录：可复现的实测与校验脚本（都在本目录 `scripts/`）

本机执行（不需要装进项目 venv，用 uv 临时环境）：

```bash
cd "output/_research/effect-presets"

# 1) 各插件默认值
uv run --no-project --with pedalboard python scripts/pb_defaults.py

# 2) §1.2–1.4 的 room_size / damping / width 实测表 + 越界行为
uv run --no-project --with pedalboard python scripts/pb_reverb_map2.py

# 3) dry_level 线性（2×）与各插件对越界值的反应
uv run --no-project --with pedalboard python scripts/pb_range_check.py

# 4) 预设 JSON 校验（结构字段 + 键名白名单 + 取值区间）
python scripts/validate_presets.py
```

`validate_presets.py` 的最近一次运行结果：**14 个 JSON 全部解析通过，必需 13 个预设齐全，
0 错误（键名全在白名单内、取值无越界）**；额外预设 1 个（`keys-rhodes-phase`）。
类型使用统计：highpass 14、compressor 11、reverb 11、delay 5、distortion 4、gain 3、chorus 2、
lowpass 2、limiter 1、phaser 1。

脚本要点（也可在本文档核对数值时重跑）：
- 冲激响应法：单采样脉冲 → Reverb(wet=1, dry=0) → 包络（5 ms 滑动平均）→ 取 -5 dB 到 -25 dB 的斜率外推到 -60 dB，得 T30。
- 高频带：把混响输出再过 `HighpassFilter(cutoff_frequency_hz=4000)` 后同样测 T30。
- 立体声宽度：对 L/R 求相关系数。
- 越界：对每个插件构造越界参数实例，看是否抛 `ValueError`。
