"""Bitcoin / Litecoin. Several free public providers, tried in order until one answers:
esplora-style (mempool.space, blockstream.info, litecoinspace.org), BlockCypher, Blockchair."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import httpx

from ...config import settings
from .base import Incoming, Watcher

log = logging.getLogger("sh.chains.utxo")


def _ts(x) -> datetime | None:
    if not x:
        return None
    if isinstance(x, (int, float)):
        return datetime.fromtimestamp(x, timezone.utc).replace(tzinfo=None)
    try:  # "2026-09-27T06:20:15Z" / "2026-09-27 06:20:15"
        return datetime.fromisoformat(str(x).replace("Z", "+00:00").replace(" ", "T")).replace(tzinfo=None)
    except ValueError:
        return None


class UtxoWatcher(Watcher):
    def __init__(self, chain: str, methods):
        super().__init__(methods)
        self.chain = chain
        if chain == "btc":
            self.providers = [("esplora", settings.btc_api_url.rstrip("/")), ("esplora", "https://blockstream.info/api"),
                              ("blockchair", "https://api.blockchair.com/bitcoin")]
        else:
            self.providers = [("esplora", settings.ltc_api_url.rstrip("/")),
                              ("blockcypher", "https://api.blockcypher.com/v1/ltc/main"),
                              ("blockchair", "https://api.blockchair.com/litecoin")]
        self.http.timeout = httpx.Timeout(10.0)
        self.active = 0  # index of the provider that worked last
        self._failed_at: dict[int, float] = {}

    async def _try(self, fn_name: str, *args):
        errors = []
        # providers in priority order, but skip ones that failed in the last 10 minutes (retry them last)
        now = time.monotonic()
        healthy = [i for i in range(len(self.providers)) if now - self._failed_at.get(i, -1e9) > 600]
        order = healthy + [i for i in range(len(self.providers)) if i not in healthy]
        for i in order:
            kind, base = self.providers[i]
            try:
                res = await getattr(self, f"_{fn_name}_{kind}")(base, *args)
                if i != self.active:
                    log.warning("%s: using provider %s", self.chain, base)
                self.active = i
                self._failed_at.pop(i, None)
                return res
            except Exception as e:  # noqa: BLE001
                self._failed_at[i] = now
                errors.append(f"{base}: {type(e).__name__} {str(e)[:120]}")
        raise RuntimeError(f"{self.chain}: all providers failed: " + " | ".join(errors))

    async def poll(self) -> list[Incoming]:
        out: list[Incoming] = []
        for m in self.methods:
            out += await self._try("poll", m)
        return out

    async def verify(self, txid: str, block_number: int | None) -> bool:
        return await self._try("verify", txid)

    # ---------- esplora (mempool.space / blockstream / litecoinspace) ----------
    async def _get_json(self, url: str, **params):
        r = await self.http.get(url, params=params or None)
        r.raise_for_status()
        return r.json()

    async def _poll_esplora(self, base: str, m) -> list[Incoming]:
        r = await self.http.get(f"{base}/blocks/tip/height")
        r.raise_for_status()
        head = int(r.text.strip())
        out = []
        for tx in await self._get_json(f"{base}/address/{m.address}/txs"):
            # our own spends (change back to us) are not payments
            if any(((vin.get("prevout") or {}).get("scriptpubkey_address")) == m.address for vin in tx.get("vin", [])):
                continue
            units = sum(int(v.get("value") or 0) for v in tx.get("vout", []) if v.get("scriptpubkey_address") == m.address)
            if units <= 0:
                continue
            st = tx.get("status") or {}
            bn = st.get("block_height") if st.get("confirmed") else None
            confs = head - bn + 1 if bn else 0
            sender = ((tx.get("vin") or [{}])[0].get("prevout") or {}).get("scriptpubkey_address")
            out.append(Incoming(method=m.code, txid=tx["txid"], idx="0", to_address=m.address, units=units,
                                from_address=sender, block_number=bn, block_time=_ts(st.get("block_time")),
                                confirmations=confs, final=confs >= m.confirmations))
        return out

    async def _verify_esplora(self, base: str, txid: str) -> bool:
        r = await self.http.get(f"{base}/tx/{txid}/status")
        if r.status_code in (400, 404):
            return False
        r.raise_for_status()
        return bool(r.json().get("confirmed"))

    # ---------- BlockCypher ----------
    async def _poll_blockcypher(self, base: str, m) -> list[Incoming]:
        data = await self._get_json(f"{base}/addrs/{m.address}/full", limit=50)
        out = []
        for tx in data.get("txs") or []:
            if any(m.address in (i.get("addresses") or []) for i in tx.get("inputs") or []):
                continue
            units = sum(int(o.get("value") or 0) for o in tx.get("outputs") or [] if m.address in (o.get("addresses") or []))
            if units <= 0:
                continue
            confs = int(tx.get("confirmations") or 0)
            bn = tx.get("block_height") if (tx.get("block_height") or -1) > 0 else None
            sender = ((tx.get("inputs") or [{}])[0].get("addresses") or [None])[0]
            out.append(Incoming(method=m.code, txid=tx["hash"], idx="0", to_address=m.address, units=units,
                                from_address=sender, block_number=bn, block_time=_ts(tx.get("confirmed")),
                                confirmations=confs, final=confs >= m.confirmations))
        return out

    async def _verify_blockcypher(self, base: str, txid: str) -> bool:
        r = await self.http.get(f"{base}/txs/{txid}")
        if r.status_code == 404:
            return False
        r.raise_for_status()
        return int(r.json().get("confirmations") or 0) > 0

    # ---------- Blockchair ----------
    async def _poll_blockchair(self, base: str, m) -> list[Incoming]:
        data = await self._get_json(f"{base}/dashboards/address/{m.address}", transaction_details="true", limit=50)
        head = int(((data.get("context") or {}).get("state")) or 0)
        info = (data.get("data") or {}).get(m.address) or {}
        out = []
        for tx in info.get("transactions") or []:
            units = int(tx.get("balance_change") or 0)
            if units <= 0:
                continue
            bn = tx.get("block_id") if (tx.get("block_id") or -1) > 0 else None
            confs = head - bn + 1 if bn and head else 0
            out.append(Incoming(method=m.code, txid=tx["hash"], idx="0", to_address=m.address, units=units,
                                block_number=bn, block_time=_ts(tx.get("time")), confirmations=confs,
                                final=confs >= m.confirmations))
        return out

    async def _verify_blockchair(self, base: str, txid: str) -> bool:
        data = await self._get_json(f"{base}/dashboards/transaction/{txid}")
        tx = ((data.get("data") or {}).get(txid) or {}).get("transaction") or {}
        return (tx.get("block_id") or -1) > 0
