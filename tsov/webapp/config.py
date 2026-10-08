"""Web 壳常量与路径（F5：自 tsov/web.py 拆出）。

运行期可调点集中于此（测试打桩 / 环境覆盖）——
- AGENT_SESSION_DIR / SSE_HEARTBEAT_SEC / AGENT_MAX_TURNS / RENDER_WAV_NAME / EDITED_SCORE_NAME
  由消费方以 `config.X` 形式动态读取（模块前缀引用），保证单点覆盖对全部读点生效；
  旧打桩点 `tsov.web.X` 已作废（web.py 仅做取值快照的兼容再导出）。
"""

from __future__ import annotations

import os
from pathlib import Path

WEB_VERSION = "0.1.5"
AGENT_SESSION_DIR = "output/agent-sessions"
# agent 单条消息内最大工具循环轮数（2026-10-03 洛怜拍板：默认 12 → 60——长链编曲任务频繁触线）；
#   仍可用环境变量 TSOV_AGENT_MAX_TURNS 覆盖（Web server 启动前设置）
AGENT_MAX_TURNS = int(os.environ.get("TSOV_AGENT_MAX_TURNS", "60"))
RENDER_WAV_NAME = "render.wav"  # 工程目录内试听 wav（WAV 不入库，工程 .gitignore 已挡）
EDITED_SCORE_NAME = "agent-edited-score.json"  # agent 高层编辑工具的落盘约定（tsov/agent/tools.py）
SSE_HEARTBEAT_SEC = 15.0

STATIC_DIR = Path(__file__).parent.parent / "web" / "static"  # 契约 docs/05 §四：tsov/web/static/（随包分发）
