# VIS-REASCRIPT-USAGE：Reaper 导入 tsov MIDI 最小闭环使用说明

> 日期：2026-08-14 | 状态：待用户实测
> 配套脚本：`scripts\reaper\tsov_import_reascript.py`

## 一、一次性准备（装 Reaper Python 解释器）

Reaper 7.78 便携版已部署在 `D:\tools\Reaper\app\`（reaper.ini 与 exe 同目录 = 便携模式，配置全在本地）。

**Reaper 的 Python 支持 = 嵌入 python3.dll**（不是跑脚本解释器），所以必须让 Reaper 指向一个**完整的 Python 3.12 安装目录**（要含 `python3.dll` + `python312.dll` + 完整 stdlib）。本项目 tsov 的 venv 没有 stdlib，不能直接给 Reaper 用（详见 ADR-0010 与第四节"踩过的坑"）。

操作步骤：

1. 打开 Reaper：`D:\tools\Reaper\app\reaper.exe`
2. 菜单 Options → Preferences → **Plug-Ins → ReaScript**（顶部搜索框输入 "ReaScript" 最快）
3. 找到 Python 一栏，填写解释器路径为：
   ```
   %USERPROFILE%\AppData\Roaming\uv\python\cpython-3.12-windows-x86_64-none
   ```
   （这是 uv 托管的完整 CPython 3.12.13，`python3.dll`/`python312.dll`/`Lib` 全在此目录。若提示需填 python3.dll 具体路径，填同目录下的 `python3.dll`。）
4. 点 OK / Apply。Reaper 会在 Preferences 里记住该路径（写入 reaper.ini）。

> 验证是否配置成功：Actions 窗口（按 `?`）→ 新建 ReaScript（`.py`）→ 写 `RPR_APITest()` → Run，弹出 "Test OK" 即成功。

## 二、安装脚本

1. Actions 窗口（快捷键 `?`）
2. 点 **ReaScript: Load...**（左下角）
3. 选择 `scripts\reaper\tsov_import_reascript.py`
4. 脚本会出现在动作列表里（名字带文件路径）

## 三、使用步骤（每次跑一遍）

1. 在动作列表选中 `tsov_import_reascript.py` → 点 **Run**（或双击）
2. 弹出文件选择框，默认目录已指向 `output\`：
   - 手动导航到 M8 产物目录，如 `output\m3-closed-loop\2026-08-13\m8-hum04\`
   - 选中 `song.mid`（或任意 tsov 产物 `.mid`）→ 打开
3. 脚本自动：
   - 在轨道列表顶部新建轨道，命名 **"tsov melody"**
   - 把选中的 .mid 导入该轨道
   - 给轨道挂 **ReaSynth**（Reaper 内置合成器，无需安装任何东西）
   - 若有同名 `stage-01-voice.json`，自动按 segments 在时间线加乐句区域 marker
   - 开始播放（等效按下播放键）
4. 听声：耳机里应听到 ReaSynth 播放 MIDI 旋律
5. 远程验证：每步日志已写入
   ```
   output\reaper-script-log.txt
   ```

## 四、故障排查

| 症状 | 原因 | 处理 |
|---|---|---|
| 跑脚本报 "REAPER cannot find the Python library" | ReaScript 里 Python 解释器路径没配/配错 | 回第一节步骤 3，确认路径是完整 Python 目录（含 python3.dll） |
| 导入成功但**没声** | ReaSynth 没挂上（名称匹配失败）或系统音量问题 | 看日志里 "ReaSynth added" 行；再看 Windows 右下角音量合成器里 REAPER 滑块是否拉到 0 |
| 轨道出现但没播放 | 脚本播放逻辑在个别版本不生效 | 手动按空格播放即可（脚本只是自动触发，不影响结果） |
| Reaper 启动闪 `[audio device closed]` | 音频设备掉线 | Preferences → Audio → Device 点应用/重选（WASAPI Shared + Valeton GP-50 已调通，**别改配置**） |
| 弹框找不到 output | 默认目录路径没进 dialog 首参 | 手动在对话框地址栏粘贴 `output\` |

## 五、脚本功能明细（对应任务书 3.2）

1. 文件对话框：`GetUserFileNameForRead`，初始目录 `output\`
2. 新建轨道：`InsertTrackAtIndex(0, 1)`，命名 "tsov melody"
3. 导入：`InsertMedia`（先试 mode=1 选中轨导入，退回 mode=0）
4. 挂效果：`TrackFX_AddByName`（候选名依次试：`ReaSynth (Cockos)` / `VST: ReaSynth (Cockos)` / `VST3: ReaSynth (Cockos)` / `ReaSynth`）
5. 播放：`GetPlayState` 判断 + `OnPlayButton` 触发（本版 Python 绑定无 `transport_Play`，见任务报告）
6. 日志：每步成功/失败 + 时间戳 → `output\reaper-script-log.txt`

加分项已做：读 MIDI 同目录 `stage-01-voice.json`（或 `stage-04-score.json`）→ `AddProjectMarker` 按 segments 标乐句区域。

## 六、给监工的验证清单（对应任务书第五节）

- [ ] 脚本在 Reaper 跑通：选 MIDI → 自动建轨挂 ReaSynth → 播放出声
- [ ] `output\reaper-script-log.txt` 各步骤 OK（agent 读日志核对）
- [ ] 用户试听：音高/节奏对照原哼唱
- [ ] 本使用说明落盘 ✓
