# ADR-0014 · 可视化宿主 UI：Web 本地壳（零构建 vanilla + 模块化）

- 日期：2026-08-15
- 状态：已接受
- 决策人：洛怜（拍板：先走 WebUI；前端先 vanilla、做好模块化）

## 背景

可视化 + 可交互宿主界面（docs/04 需求拆解 R3/R5）：钢琴卷帘/轨道/时间线/乐句 markers/播放指示/选择集 + 对话框语义交互。三条路径备选（Web 本地壳 / PySide6 桌面 / Reaper 深化）。

## 备选方案

1. Web 本地壳（FastAPI + WebSocket/SSE + 浏览器 UI）——开发最快、跨平台；钢琴卷帘有现成 JS 组件生态；ATRI_AGENT / K.G.Studio 同路先例；对话框/流式/可视化天然契合。✅
2. PySide6/Qt 桌面——DAW 手感原生，但 Python 生态音乐组件少、开发慢。❌
3. Reaper 深化（ReaScript + ReaWeb）——借全功能 DAW，但绑定 Reaper，产品不独立（与「独立最小闭环」冲突）。❌

## 决策

- UI = **Web 本地壳**：FastAPI 静态服务 + WebSocket/SSE 事件推流 + 浏览器单页。
- 前端 = **零构建 vanilla JS**（ES modules 按职责拆文件：卷帘/轨道/时间线/diff 视图/聊天面板各自成模块），不引 Vite/React/重型框架；组件画 canvas。
- **宿主核心与 UI 完全解耦**（协议先行）：宿主只暴露 JSON 协议（工程状态/命令/diff/播放事件），UI 是纯消费者；日后换桌面壳不动核心。
- 音频回放走后端宿主（HostEngine 预渲染缓冲 + sounddevice），前端不做 WebAudio 合成（第一版）；后续再议浏览器端试听（可选 wav 流式）。
- 端口/路径：`tsov web` 启动（默认 127.0.0.1:8790，配置可覆盖），会话目录沿用 `output/agent-sessions/`。

## 影响

- 新增依赖 FastAPI + uvicorn（pip/uv 官方源，装进 tsov/.venv）；前端静态文件随包分发（tsov/web/static/）。
- 里程碑顺序不变：M-V1 宿主命令层（无 UI，ADR-0015）→ M-V2 Web 壳最小版 → M-V3 交互闭环。
- 安全：本地回环绑定；不暴露公网；git 工程目录权限沿用系统账户。
