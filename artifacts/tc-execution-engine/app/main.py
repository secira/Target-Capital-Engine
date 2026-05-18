"""FastAPI application factory."""
from __future__ import annotations

import logging
import os
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.routers import admin, health, orders
from shared.db import run_startup_self_test

_STATIC_DIR = Path(__file__).parent / "static"

logger = logging.getLogger(__name__)
_access_logger = logging.getLogger("tc.access")


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
        # Always have a request_id, even if the caller didn't send one. Makes
        # log lines correlatable end-to-end.
        request_id = request.headers.get("X-TC-Request-ID", "") or f"auto-{uuid.uuid4().hex[:10]}"
        request.state.request_id = request_id

        start = time.perf_counter()
        method = request.method
        path = request.url.path
        try:
            response = await call_next(request)
        except Exception:
            # Let the global exception handler render the response, but make
            # sure we log the access line ourselves.
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            _access_logger.exception(
                "http %s %s -> EXC %.1fms request_id=%s",
                method, path, elapsed_ms, request_id,
            )
            raise

        elapsed_ms = (time.perf_counter() - start) * 1000.0
        # /healthz and admin static assets are too chatty for INFO.
        quiet = path in ("/healthz", "/version") or path.startswith("/admin/static")
        level = logging.DEBUG if quiet else logging.INFO
        _access_logger.log(
            level,
            "http %s %s -> %d %.1fms request_id=%s",
            method, path, response.status_code, elapsed_ms, request_id,
        )
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
    # `force=True` so we beat uvicorn's pre-configured root handler and the
    # whole engine logs through one consistent formatter.
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )
    # Quiet noisy third-party loggers unless we explicitly asked for DEBUG.
    if log_level != "DEBUG":
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logger.info("Logging configured at level=%s", log_level)
