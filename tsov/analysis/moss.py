"""MOSS-Music 参考曲目理解器客户端（M5）。

服务端：D:\\tools\\MOSS-Music\\serve_moss.py（Flask @ 127.0.0.1:8300，4bit 模型常驻）。
客户端只负责 POST /understand 并归一化结果——**不硬编码进 pipeline**（外部服务依赖，独立命令先跑通）。

输出 schema（洛怜拍板，先简单后扩展）：
{description: ≤200字中文, tags: [≤5], lyrics: 中文歌词ASR（无则为空）, error?, elapsed_s?}
"""

from __future__ import annotations

import os

import requests

DEFAULT_URL = os.environ.get("TSOV_MOSS_URL", "http://127.0.0.1:8300")
TIMEOUT_SEC = 320.0  # 服务端单请求上限 300s + 余量

EMPTY = {"description": "", "tags": [], "lyrics": "", "error": ""}


def health(url: str = DEFAULT_URL, timeout: float = 10.0) -> dict:
    """GET /health：{status: loading|ready|error, ...}。服务没起则抛 ConnectionError。"""
    resp = requests.get(url.rstrip("/") + "/health", timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def understand(audio_path: str, url: str = DEFAULT_URL, timeout: float = TIMEOUT_SEC) -> dict:
    """POST /understand {audio_path} → schema dict。

    - 服务 503（模型加载中）→ 返回 {..., error: "模型加载中，稍后再试"}
    - 服务未起 / 网络失败 → 抛 ConnectionError（调用方决定）
    """
    url = url.rstrip("/")
    try:
        resp = requests.post(
            url + "/understand", json={"audio_path": audio_path}, timeout=timeout,
        )
    except requests.exceptions.ConnectTimeout:
        return {**EMPTY, "error": "MOSS 服务连接超时"}
    except requests.exceptions.ConnectionError:
        raise
    if resp.status_code == 503:
        return {**EMPTY, "error": "模型加载中，稍后再试"}
    if resp.status_code != 200:
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            data = {}
        return {**EMPTY, **data, "error": data.get("error") or f"HTTP {resp.status_code}"}
    return resp.json()
