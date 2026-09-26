"""路由域模块（F5 拆分）：basic / projects / render / audio / retention / playback / events / chat /
chain（E3）/ staging、agents（E4 段1）。

每个模块提供 `register(app)`（函数体逐行保留原 web.py 行为；`st()` 闭包读 app.state.tsov）。
注册顺序与原 web.py 一致（链/暂存/上下文为追加末位）；静态前端挂载由 app.py 收尾（永远最后）。
"""

from __future__ import annotations

from . import agents, audio, basic, chain, chat, events, playback, projects, render, retention, staging

ROUTE_MODULES = (basic, projects, render, audio, retention, playback, events, chat, chain, staging,
                 agents)
