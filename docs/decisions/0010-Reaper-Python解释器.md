# ADR-0010 · Reaper ReaScript Python 解释器：指向 uv base 完整 CPython

- 日期：2026-08-14
- 状态：已取代（2026-08-15，见 ADR-0011）
- 决策人：洛怜 & Teto（VIS-REASCRIPT-TASK 调研结论）

## 背景

可视化方向 B 第一个里程碑：Reaper ReaScript 跑通「tsov MIDI → Reaper 轨道 → ReaSynth 播放」。Reaper 的 Python 支持是**嵌入 python3.dll**（不是跑独立 python.exe），要求指向一个**完整 CPython 安装目录**：含 `python3.dll`（稳定 ABI 转发）、`python312.dll`、完整 stdlib（`Lib\os.py`、`Lib\encodings` 等）。

任务书 3.1 预案：把 `python312.dll` + `python3.dll` 复制进 `tsov\.venv\DLLs`（"只补 dll，不动 venv 结构"）。opencode 用 C# P/Invoke（LoadLibrary python3.dll → Py_Initialize → 执行 Python 代码）实测，结论与预案不符。

## 备选方案

1. **venv\DLLs 只补两个 DLL（任务书预案 a）** —— 实测失败：CPython 嵌入时 stdlib 的发现路径**由 DLL 所在目录推导**，venv 里没有 stdlib，`sys.prefix` 被解析到项目根，`import encodings` 直接 `ModuleNotFoundError` → `init_fs_encoding` 致命错误，脚本根本起不来。venv 的 python.exe 能用是因为它自带 pyvenv.cfg 指回 base；但 Reaper 是宿主进程嵌 python3.dll，不吃 pyvenv.cfg 这套。❌
2. **指向 uv base 完整 CPython（本 ADR 定案）** —— uv 托管的 `%USERPROFILE%\AppData\Roaming\uv\python\cpython-3.12-windows-x86_64-none` 就是完整 CPython 3.12.13：`python3.dll`+`python312.dll`+`Lib` 齐全，与 tsov venv 同版本（venv 就是它创建的）。C# 嵌入实测：Py_Initialize → import sys / encodings / os / json 全部成功，编译执行 Python 代码 OK。只改 Reaper 一个配置项，不动任何安装。✅
3. **用户装官方 Python 3.12（任务书预案 b，动系统）** —— 兜底项，优先级最低。uv base 就是官方 CPython 构建，没必要再装一份。

## 决策

Reaper 的 ReaScript → Python 解释器路径，填 **uv base 完整 CPython 目录**：

```
%USERPROFILE%\AppData\Roaming\uv\python\cpython-3.12-windows-x86_64-none
```

脚本本身**不依赖 tsov venv 的第三方包**（只用 stdlib + RPR_* API），所以 Reaper 用 base 解释器跑脚本没有依赖缺失问题。`sys.path.insert(0, 项目根)` 保留，为后续里程碑 import tsov 铺路（届时 base 解释器需要能 import tsov 的依赖，另议）。

## 影响

- venv\DLLs 复制 DLL 的预案废弃，venv 结构保持原样（opencode 实测后已把临时 DLLs 目录删掉）
- 用户需在 Preferences → ReaScript 手动填一次路径（GUI 操作，任务书已预期"用户配合一次"）
- 风险：uv base 路径属于用户级缓存，若 uv 重建/换版本路径可能变——届时重填即可，不影响项目代码
- 命令执行约束不变：opencode 不写仓库外文件，只读参考 uv base 目录、并把路径写进配置说明文档

## 参考

- 实测证据：opencode 的 `embed_test`/`diag` C# P/Invoke 脚本（临时目录，不入库）
- 使用说明：`docs/VIS-REASCRIPT-USAGE.md`
