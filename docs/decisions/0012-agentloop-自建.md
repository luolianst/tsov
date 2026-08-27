# ADR-0012 · agentloop 自建：纯 Python 移植 pi loop 结构 + dsh 机制（不依赖 harness）

- 日期：2026-08-15
- 状态：已接受
- 决策人：洛怜（拍板选项 B：移植 pi 结构 + dsh 机制，纯 Python 自建）

## 背景

目标：tsov 跑通**不依赖 dsh/opencode 的独立最小闭环**——agentloop（对话/派单 → 工具调用 → 观测回喂 → 循环）。用户设想「从 pi 或 dsh 切一段现有框架，填自己的提示词和工具链，不重复造轮子」。调研结论：

- dsh（本机包已检查）：可切的是**机制**——`config/agent-presets/*/`（preset.yml + agent.cordis.yml + `skills/*/SKILL.md`）、工具注册表（JSON schema 工具 + 观测回喂）、append-only 会话/trajectory 持久化；但运行时是 TypeScript + Cordis，与 tsov（纯 Python 库，ADR-0005）语言不通，嵌入只能 subprocess 桥接。
- pi（badlogic/pi-mono）：单二进制 C 实现，loop 极简（上下文组装 → 模型工具调用 → 执行 → 观测 → 续循环，几百行），证明独立 agentloop 的最小完备形态，但代码同样不可嵌入。
- 两者都是 developer preview / 迭代中——把产品闭环绑在它们上面违背「独立最小闭环」。

## 备选方案

1. **subprocess 复用 dsh headless**（`tsov agent` 内调 `dsh --profile headless`）——最快，但没脱离 harness；dsh 会 BREAKING CHANGES；多进程复杂度。❌
2. **移植 pi 结构 + dsh 机制，纯 Python 自建（本 ADR 定案）**——切架构与接口形态，不切二进制；~300-500 行重现于 `tsov/agent/`，直接复用 `tsov/analysis/llm.py` 的 OpenAI 兼容端点/重试/降级。✅
3. Python 原生框架（LangGraph / smolagents）——重依赖，通用 agent 抽象与音乐领域 loop 不贴合，框架自身迭代风险。❌

## 决策

新增 `tsov/agent/`（纯 Python，零新运行时依赖）：

- `registry.py`：`ToolRegistry` / `ToolSpec`（name/description/JSON schema parameters/handler），输出 OpenAI 工具格式；工具执行结果（观测）回喂。
- `loop.py`：`AgentLoop.run(task, max_turns)`——系统提示 → 模型调用（**原生 function calling 优先，JSON 决策兜底**：内容里 `{"tool","arguments"}` 或 `{"answer"}`）→ 执行工具 → 观测回喂 → 续循环；无工具调用即视为最终回答。
- `session.py`：`AgentSession`——会话消息 append 到 `output/agent-sessions/<ts>.jsonl`（append-only，同 dsh 会话哲学；与 ADR-0005 落盘镜像同源）。
- `tools.py`：tsov 领域工具集——`load_score` / `edit_score`（复用 tsov.analysis.edit，标注入参可选）/ `render_wav` / `play_score`（走 tsov/host，ADR-0013）/ `transcribe` / `understand`（MOSS）/ `list_dir`。
- `prompt.py`：agent 系统提示（角色 + 工具协议 + JSON 兜底说明）。
- LLM 端点/模型/key：沿用 `TSOV_LLM_ENDPOINT` / `TSOV_LLM_MODEL` / `OPENCODE_GO_API_KEY` 环境变量（同 analysis/llm.py），不硬编码。
- CLI：`tsov agent run <task> [--max-turns N] [--model ...]`。

## 影响

- 「不依赖 harness」指编排自持；LLM 调用仍走 OpenAI 兼容端点（key 环境变量），与 dsh 无运行时耦合。
- skill 机制（SKILL.md 模式）随里程碑演进引入（首版先内置系统提示，不单开 skill 目录，避免过度设计）。
- DoD：**无 dsh 环境**下 `tsov agent run "读 stage-04-score.json → 改成 D dorian → 导出 → 宿主回放"` 跑通（LLM key 在场时真跑；自动化测试用脚本化 LLM 验证全链路）。
