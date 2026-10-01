"""Solana: SOL and USDT(SPL) over JSON-RPC, finalized commitment only.

Transactions come in versions legacy, 0 and 1: asking for less than the newest the RPC can return makes it
refuse the whole call (one payment from a new wallet used to stop every Solana payment), so we ask for 1."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from ...config import settings
from ...kv import kv_get, kv_set
from .base import Incoming, Watcher

log = logging.getLogger("sh.chains.sol")


class RpcError(Exception):
    pass


class SolanaWatcher(Watcher):
    chain = "sol"

    def __init__(self, methods):
        super().__init__(methods)
        self.url = settings.sol_rpc_url

    async def rpc(self, method: str, params: list):
        for attempt in range(4):
            r = await self.http.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            if r.status_code == 429:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                raise RpcError(f"{method}: {data['error']}")
            return data["result"]
        raise RpcError(f"{method}: rate limited")

    async def poll(self) -> list[Incoming]:
        out: list[Incoming] = []
        for m in self.methods:
            if m.token:
                accs = await self.rpc("getTokenAccountsByOwner", [m.address, {"mint": m.token},
                                                                  {"encoding": "jsonParsed", "commitment": "finalized"}])
                for acc in accs.get("value") or []:
                    out += await self._scan(m, acc["pubkey"])
            else:
                out += await self._scan(m, m.address)
        return out

    async def _scan(self, m, watch_addr: str) -> list[Incoming]:
        key = f"sol:cursor:{m.code}:{watch_addr}"
        until = await kv_get(key)
        opts = {"limit": 50, "commitment": "finalized"}
        if until:
            opts["until"] = until
        sigs = await self.rpc("getSignaturesForAddress", [watch_addr, opts])
        if not sigs:
            return []
        min_time = time.time() - 3 * 3600
        out: list[Incoming] = []
        for s in reversed(sigs):  # oldest first
            if s.get("err") or (not until and (s.get("blockTime") or 0) < min_time):
                continue
            tx = await self.rpc("getTransaction", [s["signature"], {"encoding": "jsonParsed", "commitment": "finalized",
                                                                    "maxSupportedTransactionVersion": 1}])
            await asyncio.sleep(0.15)
            if not tx or (tx.get("meta") or {}).get("err"):
                continue
            inc = self._parse(m, s["signature"], tx)
            if inc:
                out.append(inc)
        await kv_set(key, sigs[0]["signature"])
        return out

    def _parse(self, m, sig: str, tx: dict) -> Incoming | None:
        meta = tx["meta"]
        raw_keys = tx["transaction"]["message"]["accountKeys"]
        keys = [k["pubkey"] if isinstance(k, dict) else k for k in raw_keys]
        # our own actions (swaps, closing accounts...) are not customer payments
        if any(isinstance(k, dict) and k.get("signer") and k.get("pubkey") == m.address for k in raw_keys):
            return None
        bt = tx.get("blockTime")
        when = datetime.fromtimestamp(bt, timezone.utc).replace(tzinfo=None) if bt else None
        if m.token:
            def bal(entries):
                return {e["accountIndex"]: (e.get("owner"), int(e["uiTokenAmount"]["amount"]))
                        for e in entries or [] if e.get("mint") == m.token}
            pre, post = bal(meta.get("preTokenBalances")), bal(meta.get("postTokenBalances"))
            delta, sender = 0, None
            for idx in set(pre) | set(post):
                owner = (post.get(idx) or pre.get(idx))[0]
                d = post.get(idx, (owner, 0))[1] - pre.get(idx, (owner, 0))[1]
                if owner == m.address:
                    delta += d
                elif d < 0:
                    sender = owner
            if delta <= 0:
                return None
            return Incoming(method=m.code, txid=sig, idx="t", to_address=m.address, units=delta, from_address=sender,
                            block_number=tx.get("slot"), block_time=when, confirmations=1, final=True)
        try:
            i = keys.index(m.address)
        except ValueError:
            return None
        delta = meta["postBalances"][i] - meta["preBalances"][i]
        if delta <= 0:
            return None
        return Incoming(method=m.code, txid=sig, idx="0", to_address=m.address, units=delta,
                        from_address=keys[0] if keys else None, block_number=tx.get("slot"), block_time=when,
                        confirmations=1, final=True)
