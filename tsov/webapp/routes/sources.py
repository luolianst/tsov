"""音源库 REST（S4.1）：列表/搜索、扫描、缺失 relink、收藏/最近。

所有错误走统一契约 `{"error": "..."}`（见 app.py 的 HTTPException handler）。
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ...sources import SourceError, SourceLibrary
from ..models import SourceFavoriteIn, SourceRelinkIn, SourceScanIn


def _lib() -> SourceLibrary:
    return SourceLibrary()


def _source_error(exc: Exception) -> HTTPException:
    return HTTPException(400, str(exc))


def register(app: FastAPI) -> None:
    @app.get("/api/sources")
    def list_sources(q: str = "", kind: str | None = None, favorites: bool = False,
                     limit: int = 200) -> dict:
        lib = _lib()
        rows = lib.search(q, kind=kind, favorites_only=favorites, limit=limit)
        return {"summary": lib.summary(), "sources": [s.to_dict() for s in rows]}

    @app.post("/api/sources/scan")
    def scan_sources(body: SourceScanIn | None = None) -> dict:
        lib = _lib()
        b = body or SourceScanIn()
        try:
            result = lib.scan(roots=b.roots, force=b.force)
        except SourceError as exc:
            raise _source_error(exc) from exc
        return {"ok": True, **result, "summary": lib.summary()}

    @app.get("/api/sources/{source_id}")
    def get_source(source_id: str) -> dict:
        try:
            return {"source": _lib().get(source_id).to_dict()}
        except SourceError as exc:
            raise _source_error(exc) from exc

    @app.post("/api/sources/{source_id}/favorite")
    def favorite_source(source_id: str, body: SourceFavoriteIn | None = None) -> dict:
        lib = _lib()
        try:
            src = lib.set_favorite(source_id, (body or SourceFavoriteIn()).on)
        except SourceError as exc:
            raise _source_error(exc) from exc
        lib.save()
        return {"ok": True, "source": src.to_dict()}

    @app.post("/api/sources/{source_id}/recent")
    def recent_source(source_id: str) -> dict:
        lib = _lib()
        try:
            lib.mark_recent(source_id)
        except SourceError as exc:
            raise _source_error(exc) from exc
        lib.save()
        return {"ok": True}

    @app.post("/api/sources/relink")
    def relink_source(body: SourceRelinkIn) -> dict:
        lib = _lib()
        try:
            src = lib.relink(body.source_id, body.path)
        except (SourceError, OSError) as exc:
            raise _source_error(exc) from exc
        return {"ok": True, "source": src.to_dict()}
