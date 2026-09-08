"""Application entry point.

Run it with:

    uvicorn app.main:app --reload --port 8000    # from the backend/ directory

Deliberately thin: it wires configuration, logging, middleware and routers
together and owns no business logic of its own.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import newsletter, trends
from app.core.config import configure_logging, get_settings
from app.models.schemas import ErrorResponse
from app.services.newsletter_service import get_newsletter_service

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Configure logging and report the effective config once, at startup."""
    configure_logging()
    settings = get_settings()
    log.info("%s v%s starting", settings.app_name, settings.app_version)
    log.info("configuration: %s", settings.describe())

    if not settings.openrouter_enabled:
        log.warning(
            "OPENROUTER_API_KEY is not set - LLM features will be unavailable. "
            "Copy .env.example to .env and add your key."
        )
    yield
    log.info("%s shutting down", settings.app_name)


settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    description="AI-powered startup intelligence newsletter.",
    version=settings.app_version,
    docs_url="/docs",
    lifespan=lifespan,
)

# Permissive so a separate dev frontend can call this backend. Tighten before
# exposing the service to anything but localhost.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(newsletter.router)
app.include_router(trends.router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last line of defence.

    The traceback goes to the log, never to the client: an unexpected error
    must not leak internals - or anything read from the environment - into an
    HTTP response.
    """
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(detail="Internal server error").model_dump(),
    )


@app.get("/api", tags=["meta"], summary="Service metadata")
def service_info() -> dict[str, object]:
    """What this service is, and where to look next.

    This lived at `/` until the dashboard was added; `/` now serves the UI, so
    the machine-readable index moved here rather than disappearing.
    """
    return {
        "service": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs",
        "ui": "/trends",
        "endpoints": [
            "/health",
            "/api/newsletter/health",
            "/api/newsletter/generate",
            "/api/trends",
            "/api/trends/discover",
            "/api/trends/health",
        ],
    }


# --------------------------------------------------------------------------
# Frontend
#
# The dashboard is plain HTML/CSS/JS served by this app: no Node, no bundler,
# no second process to run. `/` and `/trends` return the same document -
# there is one page today, and the route exists so the URL is meaningful.
# --------------------------------------------------------------------------
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
@app.get("/trends", include_in_schema=False)
def dashboard() -> FileResponse:
    """The trend discovery dashboard."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health", tags=["meta"], summary="Application health")
def health() -> dict[str, object]:
    """Liveness plus the effective configuration. Never returns the API key."""
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": settings.app_version,
        "config": settings.describe(),
        "llm": get_newsletter_service().llm_status(),
    }
