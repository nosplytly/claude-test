"""TON + USDT(TON) via toncenter v3.

USDT is detected on OUR real jetton wallet (derived from the USDT master via get_wallet_address),
so fake "USDT" jettons can't be credited.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from datetime import datetime, timezone

from ...config import settings
from ...kv import kv_get, kv_set
from .base import Incoming, Watcher
from .tonutil import address_from_boc, address_to_boc, to_friendly, to_raw

log = logging.getLogger("sh.chains.ton")

SKIP_OPCODES = {"0x7362d09c", "0xd53276db", "0x178d4519"}  # jetton_notify, excesses, internal_transfer


def _find_comment(obj) -> str | None:
    if isinstance(obj, dict):
        if obj.get("@type") == "text_comment":
            return obj.get("comment") or obj.get("text")
        for v in obj.values():
            c = _find_comment(v)
            if c:
                return c
    return None


def _hex_hash(b64: str) -> str:
    try:
        return base64.b64decode(b64).hex()
    except Exception:  # noqa: BLE001
        return b64


class TonWatcher(Watcher):
    chain = "ton"

    def __init__(self, methods):
        super().__init__(methods)
        self.base = settings.toncenter_url.rstrip("/")
        if settings.toncenter_api_key:
            self.http.headers["X-API-Key"] = settings.toncenter_api_key
        self._min_gap = 0.35 if settings.toncenter_api_key else 1.1  # free tier ~1 rps
        self._last = 0.0
        self._jetton_wallets: dict[str, str | None] = {}

    async def _get(self, path: str, params: dict) -> dict:
        for attempt in range(3):
            wait = self._last + self._min_gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
            r = await self.http.get(f"{self.base}{path}", params=params)
            if r.status_code == 429:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return {}

    async def jetton_wallet(self, owner: str, master: str) -> str | None:
        key = f"{owner}|{master}"
        if self._jetton_wallets.get(key):
            return self._jetton_wallets[key]
        cached = await kv_get(f"ton:jw:{key}")
        if cached:
            self._jetton_wallets[key] = cached
            return cached
        v2 = self.base.rsplit("/api/v3", 1)[0] + "/api/v2/runGetMethod"
        r = await self.http.post(v2, json={"address": master, "method": "get_wallet_address",
                                           "stack": [["tvm.Slice", address_to_boc(owner)]]})
        r.raise_for_status()
        res = r.json()["result"]
        if res.get("exit_code") != 0:
            raise RuntimeError(f"get_wallet_address exit {res.get('exit_code')}")
        jw = address_from_boc(res["stack"][0][1]["bytes"])
        self._jetton_wallets[key] = jw
        await kv_set(f"ton:jw:{key}", jw)
        log.info("USDT jetton wallet for %s is %s", to_friendly(owner), to_friendly(jw, bounceable=True))
        return jw

    async def _since(self, key: str) -> int:
        last = await kv_get(f"ton:since:{key}")
        return int(last) - 900 if last else int(time.time()) - 3 * 3600

    async def poll(self) -> list[Incoming]:
        out: list[Incoming] = []
        for m in self.methods:
            owner = to_raw(m.address)
            if m.token:
                jw = await self.jetton_wallet(owner, m.token)
                out += await self._poll_jetton(m, owner, jw)
            else:
                out += await self._poll_native(m, owner)
        return out

    async def _poll_native(self, m, owner: str) -> list[Incoming]:
        since = await self._since(m.code)
        data = await self._get("/transactions", {"account": owner, "limit": 100, "sort": "desc", "start_utime": since})
        items, newest = [], since
        for tx in data.get("transactions", []):
            newest = max(newest, int(tx["now"]))
            im = tx.get("in_msg") or {}
            if not im.get("source") or im.get("bounced") or im.get("opcode") in SKIP_OPCODES:
                continue
            if (tx.get("description") or {}).get("aborted"):
                # an undeployed wallet "aborts" but keeps the coins; only skip if they bounced back
                if any((o or {}).get("destination") == im["source"] for o in tx.get("out_msgs") or []):
                    continue
            if tx.get("finality") not in (None, "finalized"):
                continue
            value = int(im.get("value") or 0)
            if value <= 0:
                continue
            decoded = (im.get("message_content") or {}).get("decoded")
            items.append(Incoming(
                method=m.code, txid=_hex_hash(tx["hash"]), idx="0", to_address=m.address, units=value,
                from_address=im["source"], comment=_find_comment(decoded),
                block_time=datetime.fromtimestamp(int(tx["now"]), timezone.utc).replace(tzinfo=None),
                confirmations=1, final=True))
        await kv_set(f"ton:since:{m.code}", newest)
        return items

    async def _poll_jetton(self, m, owner: str, jw: str) -> list[Incoming]:
        since = await self._since(m.code)
        data = await self._get("/transactions", {"account": jw, "limit": 100, "sort": "desc", "start_utime": since})
        items, newest = [], since
        for tx in data.get("transactions", []):
            newest = max(newest, int(tx["now"]))
            im = tx.get("in_msg") or {}
            decoded = (im.get("message_content") or {}).get("decoded") or {}
            if decoded.get("@type") != "jetton_internal_transfer" or im.get("bounced"):
                continue
            desc = tx.get("description") or {}
            if desc.get("aborted") or not (desc.get("compute_ph") or {}).get("success", False):
                continue
            if tx.get("finality") not in (None, "finalized"):
                continue
            units = int(((decoded.get("amount") or {}).get("value")) or 0)
            if units <= 0:
                continue
            frm = decoded.get("from") or {}
            from_addr = f"{frm.get('workchain_id', 0)}:{frm['address']}" if frm.get("address") else None
            items.append(Incoming(
                method=m.code, txid=_hex_hash(tx["hash"]), idx="j", to_address=m.address, units=units,
                from_address=from_addr, comment=_find_comment(decoded.get("forward_payload")),
                block_time=datetime.fromtimestamp(int(tx["now"]), timezone.utc).replace(tzinfo=None),
                confirmations=1, final=True))
        await kv_set(f"ton:since:{m.code}", newest)
        return items
