"""C4：422 请求校验错误也走统一 {"error"} 契约。"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from tsov.web import create_app

from unit._cleanup import rmtree_force


def test_422_returns_error_contract():
    d = Path("output") / f"contract-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        client = TestClient(create_app(output_dir=d))
        r = client.post("/api/projects", json={})   # 缺 name
        assert r.status_code == 422
        body = r.json()
        assert "error" in body and "请求参数不合法" in body["error"]
        assert "details" in body
    finally:
        rmtree_force(d)
