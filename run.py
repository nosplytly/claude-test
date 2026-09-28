import uvicorn

from app.config import settings

if __name__ == "__main__":
    # single process on purpose: background workers (chain monitor, order processor, bot) live inside the app
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, workers=1, proxy_headers=True,
                forwarded_allow_ips="127.0.0.1", log_level="info", access_log=False)
