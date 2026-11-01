"""Локальное приложение только для чтения."""

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from productpulse.analytics import compare, report
from productpulse.exports import ledger_csv
from productpulse.store import MAX_OFFSET, Store

STATIC = Path(__file__).parent / "static"


class UnicodeSafeJSONResponse(JSONResponse):
    """JSON-экранирование сохраняет даже карантинный текст с одиночным суррогатом."""

    def render(self, content):
        return json.dumps(
            content, ensure_ascii=True, allow_nan=False, separators=(",", ":")
        ).encode("ascii")


def create_app(store=None):
    own_store = store is None
    db = store or Store(os.environ.get("PRODUCTPULSE_DATABASE_URL", "sqlite:///productpulse.db"))

    @asynccontextmanager
    async def lifespan(app):
        db.initialize()
        yield
        if own_store:
            db.close()

    app = FastAPI(
        title="ProductPulse · аналитика аренды",
        version="0.1.0",
        lifespan=lifespan,
        default_response_class=UnicodeSafeJSONResponse,
        docs_url=None,
        redoc_url=None,
        description="Синтетическая лаборатория проверяемых показателей. Только чтение.",
    )
    app.state.store = db

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_parameters(request, exc):
        return JSONResponse(
            status_code=422,
            content={
                "detail": "Проверьте обязательные параметры и допустимые значения.",
                "fields": [".".join(str(part) for part in error["loc"]) for error in exc.errors()],
            },
        )

    @app.get("/docs", include_in_schema=False)
    def reference():
        return FileResponse(STATIC / "api.html")

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        return JSONResponse(
            status_code=503,
            content={"detail": "База данных недоступна. Проверьте подключение и выполните init."},
        )

    @app.get("/api/health")
    def health():
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "database": db.engine.dialect.name, "policy_version": "rental-v1"}

    @app.get("/api/report")
    def get_report(
        start: str,
        end: str,
        as_of: str | None = None,
        channel: str | None = None,
        category: str | None = None,
    ):
        return report(db, start=start, end=end, as_of=as_of, channel=channel, category=category)

    @app.get("/api/compare")
    def get_compare(
        start: str,
        end: str,
        before: str,
        after: str,
        channel: str | None = None,
        category: str | None = None,
    ):
        return compare(
            db, start=start, end=end, before=before, after=after, channel=channel, category=category
        )

    @app.get("/api/receipts")
    def get_receipts(
        status: str | None = None,
        event_id: str | None = None,
        as_of: str | None = None,
        offset: int = Query(0, ge=0, le=MAX_OFFSET),
        limit: int = Query(50, ge=1, le=200),
    ):
        return db.receipts(
            status=status, event_id=event_id, as_of=as_of, offset=offset, limit=limit
        )

    @app.get("/api/export")
    def export(
        start: str,
        end: str,
        as_of: str | None = None,
        channel: str | None = None,
        category: str | None = None,
    ):
        snapshot = report(db, start=start, end=end, as_of=as_of, channel=channel, category=category)
        return Response(
            content="\ufeff" + ledger_csv(snapshot),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="productpulse-ledger.csv"',
                "X-Snapshot-Fingerprint": snapshot["fingerprint"],
            },
        )

    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
