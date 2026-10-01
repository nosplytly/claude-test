from __future__ import annotations

import asyncio
import atexit
import hashlib
import logging
import queue
from contextlib import asynccontextmanager
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import bot as bot_mod
from . import fail2ban as f2b
from . import health, i18n, outbox, webhooks
from .api import router
from .backup import run_backups
from .report import run_reports
from .partner_api import ApiError, api_error_response
from .schemas import site_message
from .partner_api import router as partner_router
from .config import settings
from .db import engine, init_db
from .nervixy import nervixy
from .orders import run_processor
from .payments import monitor as monitor_mod
from .prices import price_feed
from .utils import supervised

WEB = Path(__file__).parent / "web"


def setup_logging() -> None:
    """Log records go through a queue to a background thread that writes the file and stderr. A slow disk or a
    full stderr pipe (the service wrapper not reading fast enough) must never stall the event loop — a stalled loop
    stalls every request and every open database transaction with it."""
    logs = settings.data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(logs / "app.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    q: queue.SimpleQueue = queue.SimpleQueue()
    listener = QueueListener(q, fh, sh)
    listener.start()
    atexit.register(listener.stop)
    root.handlers = [QueueHandler(q)]
    for noisy in ("httpx", "aiogram.event", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


setup_logging()
log = logging.getLogger("sh")


def asset_version() -> str:
    h = hashlib.sha1()
    for p in sorted(WEB.rglob("*")):  # the pages too: an edited index.html is served without a restart
        if p.is_file():
            h.update(p.name.encode())
            h.update(str(p.stat().st_mtime_ns).encode())
    return h.hexdigest()[:10]


async def _settle_only() -> None:
    """Monitor disabled (local dev): still settle simulated transfers."""
    while True:
        health.beat("settle")
        await monitor_mod.settle({})
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await f2b.load()
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
             asyncio.create_task(supervised("backup", run_backups, restart_delay=300)),
             asyncio.create_task(supervised("fail2ban", f2b.run)),
             asyncio.create_task(supervised("report", run_reports, restart_delay=300)),
             asyncio.create_task(supervised("watchdog", health.watchdog)),
             asyncio.create_task(supervised("outbox", outbox.run)),  # Telegram news about money, with retries
             asyncio.create_task(supervised("webhooks", webhooks.run))]  # partners' webhooks, off the order path
    if settings.monitor_enabled:
        tasks.append(asyncio.create_task(supervised("monitor", monitor_mod.monitor.run)))
    else:
        tasks.append(asyncio.create_task(supervised("settle", _settle_only)))
    if settings.bot_token:
        tasks.append(asyncio.create_task(supervised("bot", bot_mod.run_bot, restart_delay=15)))
    log.info("SupplierHub started (%s), wallets: %s", settings.env,
             ", ".join(monitor_mod.monitor.watchers) or "none")
    yield
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await f2b.flush()
    await engine.dispose()  # close the SQLite connections cleanly: the service stops within WinSW's 20 s


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
async def api_docs(request: Request, lang: str | None = None):
    """The partner docs in Russian or English: the RU/EN link on the page (?lang=, remembered in a cookie) → the
    language the site was last shown in (the same cookie, set by the site) → the browser's language."""
    chosen = lang if lang in i18n.LANGS else None
    lang = chosen or (request.cookies.get("sh_lang") if request.cookies.get("sh_lang") in i18n.LANGS else None) \
        or i18n.lang_of((request.headers.get("accept-language") or "").split(",")[0] or None)
    v = asset_version()
    page = "docs.en.html" if lang == "en" else "docs.html"
    html = (WEB / page).read_text(encoding="utf-8").replace("{{v}}", v).replace("{{base}}", settings.base_url)
    resp = HTMLResponse(html, headers={"Cache-Control": "no-cache", "Content-Language": lang, "Vary": "Cookie, Accept-Language"})
    if chosen:
        resp.set_cookie("sh_lang", chosen, max_age=365 * 86400, samesite="lax", secure=settings.base_url.startswith("https"))
    return resp

_index_cache: dict[str, str] = {}


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def index():
    v = asset_version()
    if v not in _index_cache:
        _index_cache.clear()
        _index_cache[v] = (WEB / "index.html").read_text(encoding="utf-8").replace("{{v}}", v)
    return HTMLResponse(_index_cache[v], headers={"Cache-Control": "no-cache"})


@app.api_route("/healthz", methods=["GET", "HEAD"])
async def healthz():
    """For uptime monitors: 200 {"ok": true} when everything that moves money is alive, 503 with the list if not."""
    bad = await health.problems()
    return JSONResponse({"ok": not bad, "problems": [health.NAMES.get(b, b) for b in bad],
                         "chains": monitor_mod.monitor.status() if monitor_mod.monitor else {},
                         "nervixy_error": nervixy.last_error}, status_code=503 if bad else 200)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    """Everything the schemas (app/schemas.py) turned down. Nothing of the request reaches a handler."""
    path, errors = request.url.path, exc.errors()
    partner = path.startswith("/api/v1")
    if path.startswith("/auth/"):  # a damaged one-time login link: just the home page, as for an expired one
        return RedirectResponse("/", status_code=303)
    if errors and all(e.get("loc", ("",))[0] == "path" for e in errors):  # /orders/<not an id>: there's no such thing
        return JSONResponse({"ok": False, "error": i18n.t("err.not_found")} if partner else {"detail": i18n.t("err.not_found")},
                            status_code=404)
    if not partner:
        return JSONResponse({"detail": site_message(errors)}, status_code=422)
    if any(e.get("type") == "json_invalid" for e in errors):
        return JSONResponse({"ok": False, "error": i18n.t("err.bad_json")}, status_code=400)
    fields = {".".join(str(x) for x in e.get("loc", [])[1:]) or "body": e.get("msg", "") for e in errors}
    return JSONResponse({"ok": False, "error": i18n.t("err.fields", fields=", ".join(sorted(fields))),
                         "fields": fields}, status_code=400)


@app.middleware("http")
async def request_language(request: Request, call_next):
    """Errors and texts in the language the caller reads: the site sends X-SH-Lang, partners Accept-Language."""
    i18n.current.set(i18n.lang_of_request(request.headers))
    return await call_next(request)


# telegram.org: the Mini App SDK script; web.telegram.org may show the site in an iframe (the Mini App in Telegram
# Web) — nobody else may frame it
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
       "script-src 'self' https://telegram.org; font-src 'self'; connect-src 'self'; "
       "frame-ancestors 'self' https://web.telegram.org https://*.web.telegram.org; base-uri 'none'; form-action 'self'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if settings.secure_cookies:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if request.url.path.startswith("/assets/"):
        # versioned URLs (?v=) never change; everything else (ES-module imports like bg.js/intro.js,
        # coin icons, fonts) must be revalidated, otherwise updates stay hidden behind the browser cache
        resp.headers["Cache-Control"] = ("public, max-age=31536000, immutable" if request.query_params.get("v")
                                         else "no-cache")
    return resp


@app.middleware("http")
async def fail2ban_guard(request: Request, call_next):
    """Outermost middleware (registered last): banned IPs are turned away before anything else runs."""
    ip = request.client.host if request.client else None
    left = f2b.banned_for(ip)
    if left:
        return f2b.blocked_response(request.url.path, left, i18n.lang_of_request(request.headers))
    resp = await call_next(request)
    f2b.observe(ip, request.method, request.url.path, resp.status_code)
    return resp
