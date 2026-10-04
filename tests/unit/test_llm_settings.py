"""LLM 设置档层单测（E2，2026-10-04）：解析链 / 掩码 / 视图 / 路由（写读清 + 校验 + 自检映射）。

约定：
- 不用 pytest tmp_path（已知坑 81）——output/<uuid> + teardown rmtree_force
- 设置档路径统一以 TSOV_SETTINGS_PATH 指向隔离文件（解析链与写盘共用该覆盖）
- key 解析用例把 TSOV_LLM_API_KEY/DEEPSEEK_API_KEY/OPENCODE_GO_API_KEY 置空串
  （挡 .env 注入与真实环境串味），并清 DSH_HOME 兜底
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tsov import llm_client
from tsov.llm_client import mask_key, resolve_api_key, resolve_endpoint, resolve_model
from tsov.web import create_app

from unit._cleanup import rmtree_force


@pytest.fixture()
def env(monkeypatch):
    """隔离目录 + 设置档路径覆盖 + key 环境变量全部置空（挡 .env/dsh 串味）。"""
    d = Path("output") / f"llmtest-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    settings = d / "tsov-settings.json"
    monkeypatch.setenv("TSOV_SETTINGS_PATH", str(settings))
    for name in ("TSOV_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENCODE_GO_API_KEY",
                 "TSOV_LLM_ENDPOINT", "TSOV_LLM_MODEL"):
        monkeypatch.setenv(name, "")
    monkeypatch.delenv("DSH_HOME", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    try:
        yield {"dir": d, "settings": settings}
    finally:
        rmtree_force(d)


@pytest.fixture()
def client(env):
    return TestClient(create_app(output_dir=env["dir"]))


# ---------------------------------------------------------------------------
# 解析链 / 掩码 / 视图
# ---------------------------------------------------------------------------


def test_mask_key():
    assert mask_key("") == ""
    assert mask_key("short") == "***"
    assert mask_key("sk-1234567890abcd") == "sk-***abcd"


def test_resolve_chain_precedence(env, monkeypatch):
    """四级链：参数 > env > 设置档 > 默认。"""
    # 默认层
    assert resolve_endpoint() == "https://api.deepseek.com/v1/chat/completions"
    assert resolve_model() == "deepseek-v4-flash"
    assert resolve_api_key() == ""
    # 设置档层
    env["settings"].write_text(json.dumps({"llm": {
        "endpoint": "https://file.example/v1/chat/completions",
        "model": "file-model", "api_key": "sk-file-abcdef123456"}}), encoding="utf-8")
    assert resolve_endpoint() == "https://file.example/v1/chat/completions"
    assert resolve_model() == "file-model"
    assert resolve_api_key() == "sk-file-abcdef123456"
    # env 层 > 设置档层
    monkeypatch.setenv("TSOV_LLM_ENDPOINT", "https://env.example/v1/chat/completions")
    monkeypatch.setenv("TSOV_LLM_MODEL", "env-model")
    monkeypatch.setenv("TSOV_LLM_API_KEY", "sk-env-123456")
    assert resolve_endpoint() == "https://env.example/v1/chat/completions"
    assert resolve_model() == "env-model"
    assert resolve_api_key() == "sk-env-123456"
    # 参数层 > env 层
    assert resolve_endpoint(endpoint="https://param.example") == "https://param.example"
    assert resolve_model(model="param-model") == "param-model"
    assert resolve_api_key(api_key="sk-param") == "sk-param"


def test_view_sources(env, monkeypatch):
    v = llm_client.llm_settings_view()
    assert v["key"]["configured"] is False and v["key"]["source"] == "none"
    assert v["endpoint"]["source"] == "default" and v["model"]["source"] == "default"
    # 仅设置档
    env["settings"].write_text(json.dumps({"llm": {
        "endpoint": "https://f.example/v1/chat/completions",
        "model": "m1", "api_key": "sk-file-abcdef123456"}}), encoding="utf-8")
    v = llm_client.llm_settings_view()
    assert v["key"]["source"] == "file" and v["key"]["hint"] == "sk-***3456"
    assert v["endpoint"]["source"] == "file" and v["model"]["source"] == "file"
    assert v["file"]["endpoint"] == "https://f.example/v1/chat/completions"
    assert v["file"]["key_set"] is True
    # env 压过设置档
    monkeypatch.setenv("TSOV_LLM_API_KEY", "sk-envkey1234567")
    v = llm_client.llm_settings_view()
    assert v["key"]["source"] == "env" and v["key"]["hint"] == "sk-***4567"


# ---------------------------------------------------------------------------
# 路由（走真 HTTP 栈）
# ---------------------------------------------------------------------------


def test_route_get_and_write_validation(env, client):
    r = client.get("/api/llm/settings")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["key"]["configured"] is False
    # 非法地址 → 400
    r = client.post("/api/llm/settings", json={"endpoint": "ftp://x.example"})
    assert r.status_code == 400
    # 合法写 → 落盘 + 来源=file
    r = client.post("/api/llm/settings", json={
        "endpoint": "https://ui.example/v1/chat/completions", "model": "ui-model"})
    assert r.status_code == 200
    v = r.json()["view"]
    assert v["endpoint"]["source"] == "file" and v["model"]["source"] == "file"
    data = json.loads(env["settings"].read_text(encoding="utf-8"))
    assert data["llm"]["endpoint"] == "https://ui.example/v1/chat/completions"
    assert data["llm"]["model"] == "ui-model"
    # 空串=清除覆盖 → 回默认
    r = client.post("/api/llm/settings", json={"endpoint": "", "model": ""})
    v = r.json()["view"]
    assert v["endpoint"]["source"] == "default" and v["model"]["source"] == "default"


def test_route_key_set_and_clear(env, client):
    r = client.post("/api/llm/settings", json={"api_key": "sk-ui-test-3456"})
    v = r.json()["view"]
    assert v["key"]["configured"] is True and v["key"]["source"] == "file"
    assert v["key"]["hint"] == "sk-***3456"
    assert v["file"]["key_set"] is True
    r = client.post("/api/llm/settings", json={"clear_key": True})
    v = r.json()["view"]
    assert v["key"]["configured"] is False and v["file"]["key_set"] is False


def test_route_test_no_key(env, client):
    j = client.post("/api/llm/test", json={}).json()
    assert j["ok"] is False and "未配置" in j["message"]


def test_route_test_success_and_error_mapping(env, client, monkeypatch):
    # 成功（mock 掉真实网络调用）
    monkeypatch.setattr(llm_client, "chat_post_json", lambda *a, **k: {"ok": 1})
    j = client.post("/api/llm/test", json={"api_key": "sk-x-123456"}).json()
    assert j["ok"] is True and "连接成功" in j["message"]
    # 401 → Key 提示
    def _raise401(*a, **k):
        raise llm_client.LlmRequestError("HTTPError: 401 Client Error: Unauthorized for url: ...")
    monkeypatch.setattr(llm_client, "chat_post_json", _raise401)
    j = client.post("/api/llm/test", json={"api_key": "sk-x-123456"}).json()
    assert j["ok"] is False and "Key 无效" in j["message"]
    # 404 → 地址提示
    def _raise404(*a, **k):
        raise llm_client.LlmRequestError("HTTPError: 404 Client Error: Not Found for url: ...")
    monkeypatch.setattr(llm_client, "chat_post_json", _raise404)
    j = client.post("/api/llm/test", json={"api_key": "sk-x-123456"}).json()
    assert "404" in j["message"] and "接口地址" in j["message"]
    # 超时 → 中文提示
    def _raise_timeout(*a, **k):
        raise llm_client.LlmRequestError("Timeout: HTTPSConnectionPool(host='x', port=443): Read timed out.")
    monkeypatch.setattr(llm_client, "chat_post_json", _raise_timeout)
    j = client.post("/api/llm/test", json={"api_key": "sk-x-123456"}).json()
    assert "超时" in j["message"]
