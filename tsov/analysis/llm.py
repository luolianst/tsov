"""LLM 分析调用（M3 实现）：requests 直调 OpenAI 兼容端点。

- 端点 https://opencode.ai/zen/go/v1/chat/completions，Authorization: Bearer $OPENCODE_GO_API_KEY
- 只读语义层数据，不加工；输出结构固定 {key_candidates, suspicious_notes, phrase_suggestions, playback_notes}
- 失败降级：返回空结构 + error 字段，**绝不阻塞管线**（ADR-0004 双向拟合）
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

import requests

from .prompt import SYSTEM_PROMPT, build_prompt

# LLM 端点/模型配置（2026-08-15 起主通道 = dsh 的 DeepSeek 官方 API，deepseek-v4-flash 含 vision 能力）
# - 端点：TSOV_LLM_ENDPOINT > DEEPSEEK_BASE_URL（dsh 部署环境提供）> api.deepseek.com 官方默认
# - key 解析链见 resolve_api_key（key 不落盘、不入库，只走环境变量/参数）
_DEEPSEEK_BASE = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
LLM_ENDPOINT = os.environ.get("TSOV_LLM_ENDPOINT", f"{_DEEPSEEK_BASE}/v1/chat/completions")
LLM_MODEL = os.environ.get("TSOV_LLM_MODEL", "deepseek-v4-flash")
LLM_TIMEOUT_SEC = 240.0


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
    from ..env import load_env

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

EMPTY_ANALYSIS = {
    "key_candidates": [],
    "suspicious_notes": [],
    "phrase_suggestions": [],
    "playback_notes": "",
}


@dataclass
class AnalysisResult:
    """分析层输出：规则语义层 + LLM 分析（可降级）。"""

    key_candidates: list[dict] = field(default_factory=list)  # [{key, confidence}]
    suspicious_notes: list[dict] = field(default_factory=list)  # [{index, reason}]
    phrase_suggestions: list[str] = field(default_factory=list)
    playback_notes: str = ""
    error: str = ""  # 非空表示 LLM 调用降级/失败
    llm_used: bool = True
    semantic_dataset: dict = field(default_factory=dict)
    prompt_used: str = ""

    def to_dict(self) -> dict:
        return {
            "key_candidates": self.key_candidates,
            "suspicious_notes": self.suspicious_notes,
            "phrase_suggestions": self.phrase_suggestions,
            "playback_notes": self.playback_notes,
            "error": self.error,
            "llm_used": self.llm_used,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AnalysisResult":
        return cls(**{k: data[k] for k in ("key_candidates", "suspicious_notes", "phrase_suggestions", "playback_notes", "error") if k in data})


def _extract_json(text: str) -> dict:
    """从 LLM 响应文本里提取 JSON 对象（先整体，再找 JSON 块/代码块）。"""
    text = (text or "").strip()
    # 去掉 markdown 代码块围栏
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 兜底：匹配第一个 { 到最后一个 } 的子串
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise ValueError(f"响应不是有效 JSON：{text[:200]!r}")


def _extract_content(body: dict) -> str:
    """从 chat.completion 响应取助手正文；content 为空时回退 reasoning_content。"""
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return ""
    content = message.get("content") or ""
    if not content.strip():
        content = message.get("reasoning_content") or ""
    return content


def _merge_analysis(raw: dict) -> dict:
    """把 LLM 原始 JSON 归一到输出结构（非法字段丢弃）。"""
    out = {**EMPTY_ANALYSIS}
    if not isinstance(raw, dict):
        return out
    kc = raw.get("key_candidates") or []
    if isinstance(kc, list):
        cleaned = []
        for k in kc:
            if isinstance(k, dict) and k.get("key"):
                cleaned.append({"key": str(k["key"]), "confidence": float(k.get("confidence", 0.5))})
        out["key_candidates"] = cleaned
    sn = raw.get("suspicious_notes") or []
    if isinstance(sn, list):
        cleaned = []
        for s in sn:
            if isinstance(s, dict) and s.get("index") is not None:
                cleaned.append({"index": int(s["index"]), "reason": str(s.get("reason", ""))})
        out["suspicious_notes"] = cleaned
    ps = raw.get("phrase_suggestions") or []
    out["phrase_suggestions"] = [str(p) for p in ps] if isinstance(ps, list) else []
    pn = raw.get("playback_notes")
    out["playback_notes"] = str(pn) if pn else ""
    return out


def call_llm(semantic_dataset: dict[str, Any], **params) -> dict:
    """调 LLM 分析语义层数据，返回归一化分析 dict。

    - 永不抛异常：任何失败都返回空结构 + {"error": ...}
    - 可用参数：api_key / endpoint / model / timeout / prompt_builder
    """
    api_key = resolve_api_key(**params)
    endpoint = params.get("endpoint") or LLM_ENDPOINT
    model = params.get("model") or LLM_MODEL
    timeout = float(params.get("timeout", LLM_TIMEOUT_SEC))
    builder = params.get("prompt_builder", build_prompt)

    if not api_key:
        return {**EMPTY_ANALYSIS, "error": "缺少 OPENCODE_GO_API_KEY（LLM 分析跳过）"}

    try:
        user_prompt = builder(semantic_dataset, **params.get("prompt_kwargs", {}))
    except Exception as e:  # noqa: BLE001 降级
        return {**EMPTY_ANALYSIS, "error": f"提示词构建失败：{e}"}

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": params.get("temperature", 0.2),
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    retries = int(params.get("retries", 2))  # 空响应/解析失败重试（偶发网络抖动）
    last_error = ""
    for attempt in range(retries + 1):
        try:
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=timeout)
            resp.raise_for_status()
            body = resp.json()
            content = _extract_content(body)
            if not content.strip():
                last_error = f"LLM 空响应（第 {attempt + 1} 次）"
                continue
            raw = _extract_json(content)
            return _merge_analysis(raw)
        except Exception as e:  # noqa: BLE001 网络/解析失败进入下一轮重试
            last_error = f"{type(e).__name__}: {e}"
            continue
    return {**EMPTY_ANALYSIS, "error": f"LLM 调用失败（管线降级，重试 {retries} 次后放弃）：{last_error}"}
