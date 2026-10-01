from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException

from .i18n import t

_hits: dict[str, deque] = defaultdict(deque)


def hit(key: str, limit: int, per: float) -> None:
    """Sliding-window limiter (in-process; the app runs as a single process)."""
    now = time.monotonic()
    q = _hits[key]
    while q and now - q[0] > per:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, t("err.rate_limited"))
    q.append(now)
    if len(_hits) > 50_000:
        _hits.clear()
