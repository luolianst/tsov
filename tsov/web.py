"""tsov/web.py —— M-V2 可视化宿主 Web 壳（**薄壳**；F5 域拆分后）。

实现已迁入 `tsov/webapp/` 包（config / state / models / helpers / record / agent_session / routes 八域），
本文件只保留：组装入口（`app = create_app()`）、`tsov web` CLI 入口（`main`）、旧引用面的兼容再导出。

FastAPI 胶水层定位不变（ADR-0014 + ADR-0015，docs/05 前端接口契约；默认 127.0.0.1:8790，仅本地回环）：
- REST：工程 CRUD / state / batch（前端一切编辑走事务）/ undo-redo-rollback / log / summary
  / render / play / wav 试听 / 音频·录音 / 留存（收藏·快照窗口·设置）/ 导出
- SSE：每工程一条事件流（agent_turn / agent_tool / state_delta / diff_applied / agent_answer
  / agent_error / playback_start / playback_stop …）
- 对话框：POST /api/chat 起后台 agent 会话（tsov/webapp/agent_session.py）
- 纪律：一次只跑一个 agent 会话 / 一个后端播放（锁互斥，忙时 409）

F5 零行为变化：43 条 /api/* 路径、载荷与响应形状逐字未动；运行期可调点（测试打桩）以
`tsov.webapp.config` 为准（如 AGENT_SESSION_DIR / SSE_HEARTBEAT_SEC；本文件的同名常量仅为取值快照）。
"""

from __future__ import annotations

from .webapp import config
from .webapp.agent_session import (_load_session_messages, _read_json_safe,  # noqa: F401  兼容再导出
                                   _retention_tick, _run_agent_session)
from .webapp.app import create_app
from .webapp.helpers import _resolve_project_audio, _score_from_dict, project_state, score_duration  # noqa: F401
from .webapp.models import (BatchIn, ChatIn, ChatResetIn, ExportIn, ImportIn, PlayIn,  # noqa: F401
                            ProjectCreate, RenderIn, RollbackIn, SessionLoadIn, SettingsIn,
                            TitleIn, WindowJumpIn)
from .webapp.record import _Recorder, _recorder  # noqa: F401
from .webapp.state import (EventBus, WebState, _default_project_name,  # noqa: F401
                           _resolve_import_source, _validate_project_name)

# 兼容再导出（F5）：旧引用面 tsov.web.X 保持可导入（取值快照；运行期覆盖改 tsov.webapp.config.X）
AGENT_SESSION_DIR = config.AGENT_SESSION_DIR
AGENT_MAX_TURNS = config.AGENT_MAX_TURNS
RENDER_WAV_NAME = config.RENDER_WAV_NAME
EDITED_SCORE_NAME = config.EDITED_SCORE_NAME
SSE_HEARTBEAT_SEC = config.SSE_HEARTBEAT_SEC
STATIC_DIR = config.STATIC_DIR
WEB_VERSION = config.WEB_VERSION

app = create_app()


def main(host: str = "127.0.0.1", port: int = 8790) -> None:
    """`tsov web` 入口（ADR-0014：默认 127.0.0.1:8790，仅本地回环）。"""
    import uvicorn

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
