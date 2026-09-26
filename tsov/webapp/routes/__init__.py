"""路由域模块（F5 拆分）：basic / projects / render / audio / retention / playback / events / chat /
chain（E3）/ staging、agents（E4 段1）/ tune（E4 段2）/ arrange（E4 段3）。

每个模块提供 `register(app)`（函数体逐行保留原 web.py 行为；`st()` 闭包读 app.state.tsov）。
注册顺序与原 web.py 一致（链/暂存/上下文/调参为追加末位）；静态前端挂载由 app.py 收尾（永远最后）。
"""

from __future__ import annotations

from . import (agents, arrange, audio, basic, chain, chat, events, playback, projects, render,
               retention, staging, tune)

ROUTE_MODULES = (basic, projects, render, audio, retention, playback, events, chat, chain, staging,
                 agents, tune, arrange)
