from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .bot import runner as bot_runner
from .api import router
from .backup import run_backups
from .partner_api import ApiError, api_error_response
from .partner_api import router as partner_router
from .config import settings
from .db import init_db
from .nervixy import nervixy
from .orders import run_processor
from .payments import monitor as monitor_mod
from .prices import price_feed
from .utils import supervised

WEB = Path(__file__).parent / "web"


def setup_logging() -> None:
    logs = settings.data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(logs / "app.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.handlers = [fh, sh]
    for noisy in ("httpx", "aiogram.event", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


setup_logging()
log = logging.getLogger("sh")


def asset_version() -> str:
    h = hashlib.sha1()
    for p in sorted((WEB / "assets").rglob("*")):
        if p.is_file():
            h.update(p.name.encode())
            h.update(str(p.stat().st_mtime_ns).encode())
    return h.hexdigest()[:10]


async def _settle_only() -> None:
    """Monitor disabled (local dev): still settle simulated transfers."""
    while True:
        await monitor_mod.settle({})
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    monitor_mod.monitor = monitor_mod.Monitor()
    await price_feed.refresh()
    try:
        await nervixy.rates()
        acc = await nervixy.account()
        log.info("nervixy ok: balance %s$, discount %s%%", acc["balance"], acc["discount"])
    except Exception as e:  # noqa: BLE001
        log.warning("nervixy not reachable at startup: %s", e)
    tasks = [asyncio.create_task(supervised("prices", price_feed.run)),
             asyncio.create_task(supervised("orders", run_processor)),
             asyncio.create_task(supervised("backup", run_backups, restart_delay=300))]
    if settings.monitor_enabled:
        tasks.append(asyncio.create_task(supervised("monitor", monitor_mod.monitor.run)))
    else:
        tasks.append(asyncio.create_task(supervised("settle", _settle_only)))
    if settings.bot_token:
        tasks.append(asyncio.create_task(supervised("bot", bot_runner.run_bot, restart_delay=15)))
    log.info("SupplierHub started (%s), wallets: %s", settings.env,
             ", ".join(monitor_mod.monitor.watchers) or "none")
    yield
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="SupplierHub", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(router)
app.include_router(partner_router)
app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")


@app.exception_handler(ApiError)
async def partner_api_error(request: Request, exc: ApiError):
    return api_error_response(exc)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/api/v1"):
        return JSONResponse({"ok": False, "error": str(exc.detail)}, status_code=exc.status_code)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))


@app.get("/api-docs", response_class=HTMLResponse)
async def api_docs():
    v = asset_version()
    html = (WEB / "docs.html").read_text(encoding="utf-8").replace("{{v}}", v).replace("{{base}}", settings.base_url)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

_index_cache: dict[str, str] = {}


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def index():
    v = asset_version()
    if v not in _index_cache:
        _index_cache.clear()
        _index_cache[v] = (WEB / "index.html").read_text(encoding="utf-8").replace("{{v}}", v)
    return HTMLResponse(_index_cache[v], headers={"Cache-Control": "no-cache"})


@app.get("/healthz")
async def healthz():
    return {"ok": True, "chains": monitor_mod.monitor.status() if monitor_mod.monitor else {},
            "nervixy_error": nervixy.last_error}


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    if request.url.path.startswith("/api/v1"):
        errors = exc.errors()
        if any(e.get("type") == "json_invalid" for e in errors):
            return JSONResponse({"ok": False, "error": "Тело запроса — некорректный JSON"}, status_code=400)
        fields = sorted({".".join(str(x) for x in e.get("loc", [])[1:]) for e in errors})
        return JSONResponse({"ok": False, "error": f"Некорректные или отсутствующие поля: {', '.join(fields) or 'body'}"},
                            status_code=400)
    return JSONResponse({"detail": "Проверьте введённые данные"}, status_code=422)


CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if settings.secure_cookies:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if request.url.path.startswith("/assets/"):
        # versioned URLs (?v=) never change; everything else (ES-module imports like bg.js/intro.js,
        # coin icons, fonts) must be revalidated, otherwise updates stay hidden behind the browser cache
        resp.headers["Cache-Control"] = ("public, max-age=31536000, immutable" if request.query_params.get("v")
                                         else "no-cache")
    return resp
