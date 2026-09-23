"""tsov 可视化宿主 web 壳（F5：域拆分；对外入口仍然 = tsov/web.py）。

- config：常量与路径（测试打桩点：AGENT_SESSION_DIR / SSE_HEARTBEAT_SEC）
- state：EventBus + WebState（工程懒加载单例 / 锁）
- models / helpers：请求模型 / 工程辅助
- record：全局单录音状态
- agent_session：对话框 agent 会话线程（_stream_chat / _run_agent_session / _retention_tick）
- routes/*：八个域模块（register(app) 式注册；函数体逐行保留原 web.py 行为）
- app.create_app：组装（异常处理器 + 路由注册 + 静态挂载）
"""

from __future__ import annotations

from . import config
from .app import create_app

__all__ = ["create_app", "config"]
