"""TRON: USDT-TRC20 and TRX via TronGrid. Solidified (irreversible) txs are final."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import base58

from ...config import settings
from ...kv import kv_get, kv_set
from .base import Incoming, Watcher

log = logging.getLogger("sh.chains.tron")


def hex_to_b58(h: str) -> str:
    try:
        return base58.b58encode_check(bytes.fromhex(h)).decode()
    except Exception:  # noqa: BLE001
        return h


def _ts(ms: int | None) -> datetime | None:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).replace(tzinfo=None) if ms else None


class TronWatcher(Watcher):
    chain = "tron"

    def __init__(self, methods):
        super().__init__(methods)
        self.base = settings.trongrid_url.rstrip("/")
        if settings.trongrid_api_key:
            self.http.headers["TRON-PRO-API-KEY"] = settings.trongrid_api_key

    async def _pages(self, url: str, params: dict, max_pages: int = 5) -> list[dict]:
        out: list[dict] = []
        next_url: str | None = url
        for _ in range(max_pages):
            r = await self.http.get(next_url, params=params if next_url == url else None)
            if r.status_code == 429:
                await asyncio.sleep(2)
                continue
            r.raise_for_status()
            data = r.json()
            out += data.get("data") or []
            next_url = ((data.get("meta") or {}).get("links") or {}).get("next")
            if not next_url:
                break
            await asyncio.sleep(0.3)
        return out

    async def poll(self) -> list[Incoming]:
        items: list[Incoming] = []
        for m in self.methods:
            key = f"tron:since:{m.code}"
            since = int(await kv_get(key) or (time.time() - 3 * 3600) * 1000)
            min_ts = since - 15 * 60 * 1000
            started = int(time.time() * 1000)
            for confirmed in (True, False):
                params = {"only_to": "true", "limit": 200, "min_timestamp": min_ts,
                          ("only_confirmed" if confirmed else "only_unconfirmed"): "true"}
                if m.token:
                    rows = await self._pages(f"{self.base}/v1/accounts/{m.address}/transactions/trc20",
                                             {**params, "contract_address": m.token})
                    items += self._parse_trc20(m, rows, confirmed)
                else:
                    rows = await self._pages(f"{self.base}/v1/accounts/{m.address}/transactions", params)
                    items += self._parse_trx(m, rows, confirmed)
                await asyncio.sleep(0.3)
            await kv_set(key, started)
        # a tx seen both as unconfirmed and confirmed in one poll: keep the confirmed one
        best: dict[str, Incoming] = {}
        for it in items:
            if it.uid not in best or it.final:
                best[it.uid] = it
        return list(best.values())

    def _parse_trc20(self, m, rows: list[dict], confirmed: bool) -> list[Incoming]:
        out = []
        for t in rows:
            if t.get("type") != "Transfer" or (t.get("token_info") or {}).get("address") != m.token:
                continue
            if t.get("to") != m.address:
                continue
            units = int(t.get("value") or 0)
            if units <= 0:
                continue
            out.append(Incoming(method=m.code, txid=t["transaction_id"], idx="0", to_address=m.address, units=units,
                                from_address=t.get("from"), block_time=_ts(t.get("block_timestamp")),
                                confirmations=1 if confirmed else 0, final=confirmed))
        return out

    def _parse_trx(self, m, rows: list[dict], confirmed: bool) -> list[Incoming]:
        out = []
        for t in rows:
            try:
                ct = t["raw_data"]["contract"][0]
            except (KeyError, IndexError):
                continue
            if ct.get("type") != "TransferContract":
                continue
            if (t.get("ret") or [{}])[0].get("contractRet") not in (None, "SUCCESS"):
                continue
            v = ct["parameter"]["value"]
            if hex_to_b58(v.get("to_address", "")) != m.address:
                continue
            units = int(v.get("amount") or 0)
            if units <= 0:
                continue
            out.append(Incoming(method=m.code, txid=t["txID"], idx="0", to_address=m.address, units=units,
                                from_address=hex_to_b58(v.get("owner_address", "")), block_number=t.get("blockNumber"),
                                block_time=_ts(t.get("block_timestamp")), confirmations=1 if confirmed else 0,
                                final=confirmed))
        return out
