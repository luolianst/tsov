"""LLM 客户端公共层（F4 收口，2026-09-22）：端点/模型/key 解析 + HTTP POST + JSON 抽取。

消费方：agent/llm（工具调用）、analysis/llm（只读分析）、analysis/edit（改谱）用 POST 与解析；
web 流式代理用常量与 key 解析。各客户端的领域逻辑（消息组装/重试/降级/流式语义）各留原处，
本层只收公共底座——行为零变化。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import requests

# LLM 端点/模型配置（2026-08-15 起主通道 = dsh 的 DeepSeek 官方 API，deepseek-v4-flash 含 vision 能力）
# - 端点：TSOV_LLM_ENDPOINT > DEEPSEEK_BASE_URL（dsh 部署环境提供）> api.deepseek.com 官方默认
# - key 解析链见 resolve_api_key（key 不落盘、不入库，只走环境变量/参数）
_DEEPSEEK_BASE = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
LLM_ENDPOINT = os.environ.get("TSOV_LLM_ENDPOINT", f"{_DEEPSEEK_BASE}/v1/chat/completions")
LLM_MODEL = os.environ.get("TSOV_LLM_MODEL", "deepseek-v4-flash")
LLM_TIMEOUT_SEC = 240.0


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


def resolve_api_key(**params) -> str:
    """LLM key 解析链：调用参数 > TSOV_LLM_API_KEY > DEEPSEEK_API_KEY > OPENCODE_GO_API_KEY（旧网关兜底）
    > dsh 凭据库（$DSH_HOME/.credentials.yaml 的 DEEPSEEK_API_KEY/TSOV_LLM_API_KEY）。

    环境变量在读取前先补一轮 .env（项目根，gitignored）。
    """
    from .env import load_env

    load_env()
    return (
        params.get("api_key")
        or os.environ.get("TSOV_LLM_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY")
        or os.environ.get("OPENCODE_GO_API_KEY")
        or _dsh_credential("DEEPSEEK_API_KEY")
        or _dsh_credential("TSOV_LLM_API_KEY")
        or ""
    )


def chat_post_json(payload: dict, *, api_key: str, endpoint: str | None = None,
                   timeout: float = LLM_TIMEOUT_SEC) -> dict:
    """POST chat/completions（统一 Authorization 头 + 错误包装）→ 响应 JSON。

    HTTP/网络/解析失败统一抛 LlmRequestError（消息 = "<异常类型>: <详情>"）。
    """
    endpoint = endpoint or LLM_ENDPOINT
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
