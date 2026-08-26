# VIS-REASCRIPT-USAGE：Reaper 导入 tsov MIDI 最小闭环使用说明

> 日期：2026-08-14（Python 版）→ 2026-08-15（Lua v2，当前交付物）
> 配套脚本：`scripts\reaper\tsov_import.lua`（**推荐，无需配置解释器**）；`tsov_import_reascript.py`（Python 版，保留参考）

## 一、用哪个脚本

| 脚本 | 语言 | 是否需要配置解释器 | 状态 |
|------|------|------------------|------|
| `tsov_import.lua` | Lua | ❌ 不需要（Reaper 内置） | ✅ 当前交付物 |
| `tsov_import_reascript.py` | Python | ✅ 需要（见 ADR-0010） | 保留参考 |

用户实测 Python 解释器配置卡死（"No compatible version of Python was found"）→ 转向 Lua（ADR-0011）。

## 二、安装脚本（Lua）

1. 打开 Reaper：`D:\tools\Reaper\app\reaper.exe`（便携模式，配置全在程序目录）
2. Actions 窗口（快捷键 `?`）
3. 点 **ReaScript: Load...**（左下角）
4. 选择 `scripts\reaper\tsov_import.lua`
5. 脚本会出现在动作列表里（名字带文件路径）

## 三、使用步骤（每次跑一遍）

1. 在动作列表选中 `tsov_import.lua` → 点 **Run**（或双击）
2. 弹出文件选择框，默认目录已指向 `output\`：
   - 手动导航到 M8 产物目录，如 `output\m3-closed-loop\2026-08-13\m8-hum04\`
   - 选中 `song.mid`（或任意 tsov 产物 `.mid`）→ 打开
3. 脚本自动：
   - `InsertMedia(mode=1)`：自动新建轨道并导入 MIDI
   - 命名轨道 **"tsov melody"**
   - 给轨道挂 **ReaSynth**（Reaper 内置合成器，无需安装任何东西）
   - 若有同目录 `stage-01-voice.json`，按 segments 在时间线加乐句区域 marker
   - 开始播放（等效按下播放键）
4. 听声：耳机里应听到 ReaSynth 播放 MIDI 旋律
5. 远程验证：每步日志已写入
   ```
   output\reaper-script-log.txt
   ```

## 四、故障排查

| 症状 | 原因 | 处理 |
|------|------|------|
| 导入成功但**没声** | ReaSynth 没挂上（名称匹配失败）或系统音量问题 | 看日志里 "ReaSynth added" 行；再看 Windows 音量合成器里 REAPER 滑块是否拉到 0 |
| 轨道出现但没播放 | 脚本播放逻辑在个别版本不生效 | 手动按空格播放即可（脚本只是自动触发，不影响结果） |
| Reaper 启动闪 `[audio device closed]` | 音频设备掉线 | Preferences → Audio → Device 点应用/重选（WASAPI Shared + Valeton GP-50 已调通，**别改配置**） |
| 弹框找不到 output | 默认目录路径没进 dialog 首参 | 手动在对话框地址栏粘贴 `output\` |

## 五、脚本功能明细

1. 文件对话框：`GetUserFileNameForRead`，初始目录 `output\`
2. 导入：`InsertMedia(mode=1)`（自动新建轨道 + 插入 MIDI，v2 实测语义）
3. 命名：`GetSetMediaTrackInfo_String` 设 "tsov melody"
4. 挂效果：`TrackFX_AddByName`（候选名依次试：`ReaSynth (Cockos)` / `VST: ReaSynth (Cockos)` / `VST3: ReaSynth (Cockos)` / `ReaSynth`）
5. 播放：`GetPlayState` 判断 + `OnPlayButton` 触发
6. 日志：每步成功/失败 + 时间戳 → `output\reaper-script-log.txt`

加分项已做：读 MIDI 同目录 `stage-01-voice.json`（或 `stage-04-score.json`）→ `AddProjectMarker` 按 segments 标乐句区域。

## 六、给监工的验证清单

- [ ] 脚本在 Reaper 跑通：选 MIDI → 自动建轨挂 ReaSynth → 播放出声
- [ ] `output\reaper-script-log.txt` 各步骤 OK（agent 读日志核对）
- [ ] 用户试听：音高/节奏对照原哼唱
- [ ] 本使用说明落盘 ✓

## 七、Python 版（保留参考）

若后续里程碑需要在 ReaScript 里 `import tsov`（复用 Python 侧能力），可切回 Python 版 `tsov_import_reascript.py`，需先在 Preferences → ReaScript 配解释器为 uv base 完整 CPython（见 ADR-0010）：

```
%USERPROFILE%\AppData\Roaming\uv\python\cpython-3.12-windows-x86_64-none
```
