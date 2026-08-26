# ADR-0011 · Reaper ReaScript 交付物转向 Lua（Python 解释器方案卡死，退路启用）

- 日期：2026-08-15
- 状态：已接受
- 决策人：洛怜 & Teto（VIS-REASCRIPT-TASK 实测结论）

## 背景

可视化方向 B 第一个里程碑（`docs/VIS-REASCRIPT-TASK.md` §3.1）已预留退路：优先 Python，Python 配不通则退 Lua。ADR-0010 定案「ReaScript Python 解释器指向 uv base 完整 CPython」，但用户实测仍报 "No compatible version of Python was found"，卸载重装 Reaper 后未解决——Python 解释器配置在用户侧卡死，阻塞验收。

## 备选方案

1. 继续折腾 Python 解释器（官方 Python 3.12 / 重装 Reaper / 注册表调试）—— 已多次尝试未通，继续投入不确定，持续阻塞用户实测。❌
2. 退 Lua —— Reaper 内置 Lua 解释器，零外部依赖、零 GUI 配置，脚本改 Lua 写；JSON 标注功能砍掉或简化（任务书 §3.1 原文）。✅
3. 换可视化方向（放弃 Reaper）—— 成本最高。❌

## 决策

ReaScript 交付物改为 **Lua**：`scripts/reaper/tsov_import.lua`（v2）。Python 脚本 `tsov_import_reascript.py` 保留作参考（后续里程碑若需 `import tsov` 复用 Python 侧能力再议回 Python）。

同时实测修正了一个 Lua 语义：`InsertMedia(mode=1)` = 自动新建轨道 + 插入 MIDI（轨道名=文件名），**预建轨道会被无视**——故 Lua 版不再 `InsertTrackAtIndex` 预建轨，改为直接 `InsertMedia(mode=1)` 后按插入前轨道数取新轨道句柄。

## 影响

- 用户无需再配 Python 解释器：`Actions → ReaScript: Load → tsov_import.lua → Run` 即可。
- Lua 版自带手写简易 JSON 解析器（只支持对象/数组/字符串/数字/布尔/null；`\u` 转义不转 UTF-8）——当前乐句 marker 只用 ASCII 命名和数字，够用；后续读中文 JSON 需补 UTF-8 转义。
- ADR-0010 状态改为「已取代（见 ADR-0011）」；uv base CPython 路径仍留档，供未来需要 Python ReaScript 的里程碑使用。
