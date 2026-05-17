"""FastAPI application factory."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.routers import admin, health, orders
from shared.db import run_startup_self_test

_STATIC_DIR = Path(__file__).parent / "static"

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(
        title="tc-execution-engine",
        description="HMAC-signed order execution engine for Target Capital",
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # ------------------------------------------------------------------
    # Structured logging — inject request_id into every log record
    # ------------------------------------------------------------------

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get("X-TC-Request-ID", "")
        # Make request_id available on request.state for dependencies
        if not hasattr(request.state, "request_id"):
            request.state.request_id = request_id
        response = await call_next(request)
        if request_id:
            response.headers["X-TC-Request-ID"] = request_id
        return response

    # ------------------------------------------------------------------
    # Global error handlers
    # ------------------------------------------------------------------

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", "")
        logger.exception("Unhandled exception request_id=%s: %s", request_id, exc)
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "message": "An unexpected error occurred", "request_id": request_id},
        )

    # ------------------------------------------------------------------
    # Routers
    # ------------------------------------------------------------------

    app.include_router(health.router)
    app.include_router(orders.router)
    app.include_router(admin.router)

    # ------------------------------------------------------------------
    # Admin dashboard (static HTML at /admin)
    # ------------------------------------------------------------------

    @app.get("/admin", include_in_schema=False)
    async def admin_index():
        index_file = _STATIC_DIR / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        return JSONResponse({"error": "admin UI not bundled"}, status_code=404)

    @app.get("/", include_in_schema=False)
    async def root_redirect():
        return RedirectResponse(url="/admin")

    if _STATIC_DIR.exists():
        app.mount("/admin/static", StaticFiles(directory=_STATIC_DIR), name="admin-static")

    # ------------------------------------------------------------------
    # Startup / shutdown events
    # ------------------------------------------------------------------

    @app.on_event("startup")
    async def on_startup() -> None:
        _setup_logging()
        logger.info("tc-execution-engine starting up…")
        db_url = os.environ.get("DATABASE_URL", "")
        if db_url:
            # Hard-fail: if self-test raises, the exception propagates and
            # uvicorn/gunicorn will exit — engine does not accept traffic.
            run_startup_self_test()
        else:
            logger.warning("DATABASE_URL not set — skipping DB self-test (dev only)")

    @app.on_event("shutdown")
    async def on_shutdown() -> None:
        logger.info("tc-execution-engine shutting down")

    return app


def _setup_logging() -> None:
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s request_id=%(request_id)s %(message)s"
        if False  # custom filter needed — use simple format for now
        else "%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
