from __future__ import annotations

import json
from typing import Any

from .db import session_scope
from .models import KV


async def kv_get(key: str, default: Any = None) -> Any:
    async with session_scope() as s:
        row = await s.get(KV, key)
        return json.loads(row.value) if row else default


async def kv_set(key: str, value: Any) -> None:
    async with session_scope() as s:
        row = await s.get(KV, key)
        data = json.dumps(value)
        if row:
            row.value = data
        else:
            s.add(KV(key=key, value=data))
