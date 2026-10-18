"""S4.1 音源库 REST：scan/list/favorite/relink（无真实设备依赖）。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tsov.sources import SourceLibrary
from tsov.web import create_app


def _fake_tree(root: Path) -> None:
    bundle = root / "vst3" / "Fake.vst3" / "Contents" / "Resources"
    bundle.mkdir(parents=True)
    (bundle / "moduleinfo.json").write_text(
        '{"Name": "Fake Synth", "Classes": [{"CID": "ABCDEF0123456789",}]}',
        encoding="utf-8",
    )
    (root / "sfz").mkdir()
    (root / "sfz" / "piano.sfz").write_text("<region> sample=a.wav key=60\n", encoding="utf-8")


def test_sources_api_scan_list_favorite_relink(tmp_path, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    _fake_tree(root)
    store = tmp_path / "sources.json"
    monkeypatch.setenv("TSOV_SOURCES_PATH", str(store))

    app = create_app(tmp_path / "output")
    with TestClient(app) as c:
        r = c.post("/api/sources/scan", json={"roots": [str(root)], "force": True})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["total"] == 2

        r = c.get("/api/sources", params={"q": "fake"})
        assert r.status_code == 200, r.text
        rows = r.json()["sources"]
        assert len(rows) == 1
        sid = rows[0]["id"]
        assert rows[0]["kind"] == "vst3"

        r = c.post(f"/api/sources/{sid}/favorite", json={"on": True})
        assert r.status_code == 200, r.text
        assert r.json()["source"]["favorite"] is True

        sfz = next(s for s in SourceLibrary(store).sources.values() if s.kind == "sfz")
        bad = root / "sfz" / "bad.sfz"
        bad.write_text("<region> sample=b.wav key=60\n", encoding="utf-8")
        r = c.post("/api/sources/relink", json={"source_id": sfz.id, "path": str(bad)})
        assert r.status_code == 400
        assert "哈希不符" in r.json()["error"]
