"""请求模型（F5：自 tsov/web.py 拆出；字段与默认值逐字保留）。"""

from __future__ import annotations

from pydantic import BaseModel


class SettingsIn(BaseModel):
    """留存设置补丁（M-V7 D2）：不动字段可省略；`timed_favorite` 为 {"enabled", "interval_min"}。"""

    auto_favorite_iters: int | None = None
    timed_favorite: dict | None = None


class WindowJumpIn(BaseModel):
    """快照点跳转（M-V7 D3）：恢复到窗口内第 cursor 个位置。"""

    cursor: int


class ProjectCreate(BaseModel):
    name: str
    score: dict | None = None


class ImportIn(BaseModel):
    source: str              # output/ 内相对路径（.json 文件，或含 score.json 的目录）
    name: str | None = None  # 工程名（缺省 = 从文件名推导）


class BatchIn(BaseModel):
    label: str = ""
    commands: list[dict]
    commit_message: str | None = None


class RollbackIn(BaseModel):
    rev: str = "HEAD~1"


class RenderIn(BaseModel):
    out: str | None = None
    rev: str | None = None   # 议题 ④：A/B 对比试听旧版（git show，不动 HEAD）


class PlayIn(BaseModel):
    """宿主播放参数（M-V8 E1）：播放轴定位 + 循环区间。"""

    start: float | None = None          # 起始秒（播放轴定位）
    loop: list[float] | None = None     # [a, b] 秒——区间循环（阻塞直到 /play/stop）


class ExportIn(BaseModel):
    """导出矩阵选项（修正轮2：UI 导出弹窗 → host export_matrix）。

    E6 段2：bit_depth 16/24/32f（默认 16）；range=[起, 止] 秒 → 音频产物选段导出（MIDI 维持全曲）。
    """
    mix: bool = True
    stems: bool = True
    buses: bool = False
    midi: bool = True
    midi_stems: bool = False
    bit_depth: int | str = 16
    range: list[float] | None = None


class ChatIn(BaseModel):
    project: str
    message: str
    base_rev: str = "HEAD"   # 议题 ④：当前编辑目标版本（注入 agent prompt，A 路不允许旧版分叉）
    # M-V3（交互闭环）：人工标注确定性先行（最高优先级、不走 LLM）+ 选区上下文（「这里/这段」指代）
    annotations: list[dict] | None = None
    selection: dict | None = None
    # 批B B1-4：用户手动操作摘要（自上次对话以来；回流进 agent 上下文）
    user_actions: list[str] | None = None


class ChatResetIn(BaseModel):
    project: str


class TitleIn(BaseModel):
    title: str


class SessionLoadIn(BaseModel):
    project: str
    name: str


class LlmSettingsIn(BaseModel):
    """LLM 接入设置补丁（E2）：api_key 非空 = 设置；clear_key = 清除设置档里的 key；
    endpoint/model 传空串 = 清除该项覆盖（回默认），不传 = 不改。"""

    api_key: str | None = None
    endpoint: str | None = None
    model: str | None = None
    clear_key: bool = False


class LlmTestIn(BaseModel):
    """LLM 连通性自检入参（E2）：缺省项走解析链当前值（不落盘、不改配置）。"""

    api_key: str | None = None
    endpoint: str | None = None
    model: str | None = None
