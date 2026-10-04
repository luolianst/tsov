"""LLM 客户端公共层（F4 收口，2026-09-22；E2 设置档层，2026-10-04）：端点/模型/key 解析 + HTTP POST + JSON 抽取。

消费方：agent/llm（工具调用）、analysis/llm（只读分析）、analysis/edit（改谱）用 POST 与解析；
web 流式代理用解析函数与 key 解析。各客户端的领域逻辑（消息组装/重试/降级/流式语义）各留原处，
本层只收公共底座。

配置解析链（更早者优先）——「WebUI ⚙ 设置 → 对话 / LLM」写的就是**设置档层**（保存即生效，动态解析）：
- endpoint: 调用参数 > TSOV_LLM_ENDPOINT > 设置档 llm.endpoint > 默认（DEEPSEEK_BASE_URL > 官方 api.deepseek.com）
- model:    调用参数 > TSOV_LLM_MODEL > 设置档 llm.model > "deepseek-v4-flash"
- key:      调用参数 > TSOV_LLM_API_KEY > DEEPSEEK_API_KEY > OPENCODE_GO_API_KEY（旧网关兜底）
            > 设置档 llm.api_key > dsh 凭据库 > ""

设置档 = 仓库根 `tsov-settings.json`（gitignored；TSOV_SETTINGS_PATH 可覆盖路径；与 host.state 同约定）。
key 不落库、不外传；WebUI 回显只给掩码（见 `llm_settings_view`）。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import requests

# LLM 端点/模型/key 配置（2026-08-15 起主通道 = dsh 的 DeepSeek 官方 API，deepseek-v4-flash 含 vision 能力）
# 动态解析链（E2，2026-10-04——WebUI ⚙ 设置写的就是「设置档」层；保存即生效，无模块级常量缓存）：
# - endpoint: 参数 > TSOV_LLM_ENDPOINT > 设置档 llm.endpoint > 默认（DEEPSEEK_BASE_URL > api.deepseek.com 官方）
# - model:    参数 > TSOV_LLM_MODEL > 设置档 llm.model > DEFAULT_MODEL
# - key:      见 resolve_api_key（key 不落库、不外传；WebUI 只回掩码）
DEFAULT_MODEL = "deepseek-v4-flash"
LLM_TIMEOUT_SEC = 240.0


def _deepseek_base() -> str:
    """默认端点基底：DEEPSEEK_BASE_URL（dsh 部署环境提供）> api.deepseek.com 官方。"""
    return (os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")


def _default_endpoint() -> str:
    return f"{_deepseek_base()}/v1/chat/completions"


def settings_path() -> Path:
    """设置档路径：TSOV_SETTINGS_PATH 覆盖 > 仓库根 `tsov-settings.json`（与 host.state 同约定）。"""
    env = (os.environ.get("TSOV_SETTINGS_PATH") or "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[1] / "tsov-settings.json"


def _file_llm() -> dict:
    """设置档里的 llm 段（缺失/坏文件 → {}；读失败静默，不影响运行）。"""
    try:
        d = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    llm = d.get("llm") if isinstance(d, dict) else None
    return llm if isinstance(llm, dict) else {}


class LlmRequestError(RuntimeError):
    """HTTP/网络/响应解析层失败（消息 = "<异常类型>: <详情>"）。

    调用方自行决定重试、降级或终止（各客户端语义不同，本层不吞错、不重试）。
    """


def _dsh_credential(name: str) -> str:
    """从 dsh 的凭据库（$DSH_HOME/.credentials.yaml 的 refs）取 key——env 全缺时的最后兜底。

    只读不回显、不入库；等洛怜在 dsh Web UI Settings→Models 的 DeepSeek 卡片填一次 key
    （dsh 会写入该文件），tsov 即自动接上官方 DeepSeek 通道。
    """
    dsh_home = os.environ.get("DSH_HOME", "").strip()
    if not dsh_home:
        return ""
    cred_file = os.path.join(dsh_home, ".credentials.yaml")
    if not os.path.isfile(cred_file):
        return ""
    try:
        with open(cred_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(name + ":"):
                    return line.split(":", 1)[1].strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def resolve_endpoint(*, endpoint: str | None = None, **_kw) -> str:
    """端点解析：调用参数 > TSOV_LLM_ENDPOINT > 设置档 > 默认（每次调用动态解析——UI 改完即生效）。"""
    from .env import load_env

    load_env()
    return (endpoint or os.environ.get("TSOV_LLM_ENDPOINT")
            or str(_file_llm().get("endpoint") or "") or _default_endpoint())


def resolve_model(*, model: str | None = None, **_kw) -> str:
    """模型解析：调用参数 > TSOV_LLM_MODEL > 设置档 > 默认。"""
    from .env import load_env

    load_env()
    return (model or os.environ.get("TSOV_LLM_MODEL")
            or str(_file_llm().get("model") or "") or DEFAULT_MODEL)


def resolve_api_key(**params) -> str:
    """LLM key 解析链：调用参数 > TSOV_LLM_API_KEY > DEEPSEEK_API_KEY > OPENCODE_GO_API_KEY（旧网关兜底）
    > 设置档 llm.api_key > dsh 凭据库（$DSH_HOME/.credentials.yaml 的 DEEPSEEK_API_KEY/TSOV_LLM_API_KEY）。

    环境变量在读取前先补一轮 .env（项目根，gitignored）。
    """
    from .env import load_env

    load_env()
    return (
        params.get("api_key")
        or os.environ.get("TSOV_LLM_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY")
        or os.environ.get("OPENCODE_GO_API_KEY")
        or str(_file_llm().get("api_key") or "")
        or _dsh_credential("DEEPSEEK_API_KEY")
        or _dsh_credential("TSOV_LLM_API_KEY")
        or ""
    )


def mask_key(key: str) -> str:
    """脱敏回显：sk-***abcd（太短只给 ***）。"""
    k = (key or "").strip()
    if not k:
        return ""
    if len(k) <= 8:
        return "***"
    return f"{k[:3]}***{k[-4:]}"


def llm_settings_view() -> dict:
    """WebUI 设置面板视图（只读；key 只回掩码 + 来源，绝不明文回显）。

    来源口径与解析链一致：env（环境变量/.env）> file（设置档）> dsh / default / none。
    """
    from .env import load_env

    load_env()
    fl = _file_llm()
    key = resolve_api_key()
    if os.environ.get("TSOV_LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENCODE_GO_API_KEY"):
        key_src = "env"
    elif str(fl.get("api_key") or ""):
        key_src = "file"
    elif _dsh_credential("DEEPSEEK_API_KEY") or _dsh_credential("TSOV_LLM_API_KEY"):
        key_src = "dsh"
    else:
        key_src = "none"
    ep_src = "env" if os.environ.get("TSOV_LLM_ENDPOINT") else ("file" if str(fl.get("endpoint") or "") else "default")
    md_src = "env" if os.environ.get("TSOV_LLM_MODEL") else ("file" if str(fl.get("model") or "") else "default")
    return {
        "key": {"configured": bool(key), "hint": mask_key(key), "source": key_src},
        "endpoint": {"active": resolve_endpoint(), "source": ep_src},
        "model": {"active": resolve_model(), "source": md_src},
        "defaults": {"endpoint": _default_endpoint(), "model": DEFAULT_MODEL},
        "file": {
            "path": str(settings_path()),
            "endpoint": str(fl.get("endpoint") or ""),
            "model": str(fl.get("model") or ""),
            "key_set": bool(str(fl.get("api_key") or "")),
        },
    }


def chat_post_json(payload: dict, *, api_key: str, endpoint: str | None = None,
                   timeout: float = LLM_TIMEOUT_SEC) -> dict:
    """POST chat/completions（统一 Authorization 头 + 错误包装）→ 响应 JSON。

    HTTP/网络/解析失败统一抛 LlmRequestError（消息 = "<异常类型>: <详情>"）。
    """
    endpoint = endpoint or resolve_endpoint()
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        resp = requests.post(endpoint, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:  # noqa: BLE001 网络/协议/解析失败 → 统一包装
        raise LlmRequestError(f"{type(e).__name__}: {e}") from e


def strip_fences(text: str) -> str:
    """去掉 markdown 代码块围栏（```json ... ```）。"""
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(), flags=re.IGNORECASE)


def load_json_loose(text: str) -> Any:
    """宽松解析 JSON：去围栏 → 整体解析 → 首个 `{` 到末个 `}` 兜底；失败返回 None。"""
    s = strip_fences(text)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(s[start : end + 1])
        except json.JSONDecodeError:
            pass
    return None


def extract_json_object(text: str) -> dict:
    """从文本抽 JSON 对象（宽松）；失败或非 dict → {}。"""
    data = load_json_loose(text)
    return data if isinstance(data, dict) else {}
