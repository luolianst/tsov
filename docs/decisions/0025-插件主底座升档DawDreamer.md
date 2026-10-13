# ADR-0025 · 插件主底座升档——DawDreamer（VST3Source 升档·批L）

- 日期：2026-10-14
- 状态：已接受（洛怜 grill 十题落定；R1-B / R2-A / R3-B / R4-B / R5-A / R6-A / R7-A）
- 决策人：洛怜

## 背景

v0.2 发布歌（美声金属风·夜愿式配器参考）需要更多音源兼容。现有 `VST3Source`（pedalboard 底座·毛胚）缺四能力：参数发现（范围/标签）、预设/状态存取、VST2 兼容、崩溃隔离。初步调研 = `docs/v0.2-批L-VST3Source升档-调研报告-2026-10-14.md`（接入四路线 / 操控四层 / 整合包接线 / 决策树）。

## 决策

1. **插件主底座 = DawDreamer**（GPLv3，与 tsov AGPLv3 兼容）：第三方插件（乐器 + 效果，VST2 + VST3）统一走其加载与渲染——参数描述/离散值域标签、全参快照 `get_patch`、状态与预设存取（`save_state` / `.fxp` / `.vstpreset`）、自动化、延迟上报。
2. **进程隔离 = 常驻 worker 子进程**：第三方插件一律在 worker 内加载（崩溃自动重启+重载；渲染前按「载预设 → 应用 diff → reset」回确定起点；顺带隔离 DawDreamer 与 torch 的 LLVM 导入冲突）。
3. **内置效果链保留 pedalboard**（自家功能不迁移）；**Reaper = 验证裁判/兜底**（不在主链）。
4. **数据层（ADR-0005 向后兼容增补）**：`Instrument` 增 `params`（参数 diff·归一值表）+ `preset`（预设引用）；**预设一等、参数只存 diff**。
5. **参数知识 = 两层表**：机器全量（introspection 生成·可重扫）+ 关键件人工策展层（中文别名/分组/建议值）；资产落 `presets/vst3-maps/<plugin-slug>.json`。
6. **首发兼容面**：直载型 VST3（gate，随发布歌选 5-6 件）；VST2 顺带烟测、记档不设 gate。

## 影响

- 依赖：+`dawdreamer`；pedalboard 保留（内置效果 + 现有预设链）。
- `tsov/host/instrument.py` 的 `VST3Source` 换底重写（`SoundSource` 接口签名不变，宿主/上层零改动）；新增 worker 进程管理件。
- 命令层（ADR-0017）：新增插件参数/预设类 op + MCP 镜像；验收须双通道（人 + agent 同一动作路径）。
- 插件本体**不入分发包**，只存引用（路径 + 参数对照表）——分发依赖用户自装（呼应 P52 四包制与 2026-10-14 分包分发路线记档）。
- 关联：ADR-0013（接口预留·本 ADR 为升档落点）、ADR-0005（增补规则）、批L 任务书。
