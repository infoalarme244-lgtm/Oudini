import logging
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import db as dbm
from .config import settings
from .logging_setup import setup as setup_logging
from .web import BASE, templates

log = logging.getLogger("shopforge.http")


def create_app() -> FastAPI:
    setup_logging()
    dbm.init_db()
    if settings.sentry_dsn:
        try:
            import sentry_sdk
            sentry_sdk.init(dsn=settings.sentry_dsn, traces_sample_rate=0.0, send_default_pii=False)
        except ImportError:
            log.warning("SENTRY_DSN défini mais sentry-sdk non installé")
    app = FastAPI(title="ShopForge", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        request.state.request_id = rid
        t0 = time.perf_counter()
        try:
            resp = await call_next(request)
        except Exception:
            log.exception("unhandled error", extra={"request_id": rid, "path": request.url.path})
            raise
        resp.headers["x-request-id"] = rid
        resp.headers["x-content-type-options"] = "nosniff"
        resp.headers["referrer-policy"] = "strict-origin-when-cross-origin"
        resp.headers["x-frame-options"] = "DENY"
        log.info("request", extra={"request_id": rid, "path": request.url.path, "status": resp.status_code,
                                   "ms": round((time.perf_counter() - t0) * 1000)})
        return resp

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if exc.status_code in (301, 302, 303) and exc.headers and "Location" in exc.headers:
            return RedirectResponse(exc.headers["Location"], status_code=303)
        if request.url.path.startswith(("/api", "/webhooks")) or "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return HTMLResponse(templates.get_template("error.html").render(code=exc.status_code, detail=exc.detail),
                            status_code=exc.status_code)

    from .routers import auth, merchant, merchant_catalog, merchant_growth, merchant_sales, store, webhooks
    for r in (auth, merchant, merchant_catalog, merchant_sales, merchant_growth, store, webhooks):
        app.include_router(r.router)
    app.include_router(webhooks.one_click)

    @app.get("/healthz")
    def health():
        from sqlalchemy import text
        try:
            with dbm.engine.connect() as c:
                c.execute(text("SELECT 1"))
        except Exception:
            return JSONResponse({"ok": False, "db": "unreachable"}, status_code=503)
        return {"ok": True, "db": "ok", "email_provider": settings.email_provider, "ai": settings.ai_enabled}

    return app
