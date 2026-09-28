"""EVM chains (Ethereum, BNB Smart Chain) over plain JSON-RPC.

* ERC-20/BEP-20 USDT: eth_getLogs for Transfer(*, our_address) on the token contract.
* Native ETH: cheap balance/nonce probe every poll; blocks are only scanned when the balance grew.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ...config import settings
from ...kv import kv_get, kv_set
from .base import Incoming, Watcher

log = logging.getLogger("sh.chains.evm")

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class RpcError(Exception):
    pass


class EvmWatcher(Watcher):
    # chain -> (rpc url setting, getLogs chunk, max lookback blocks, native scan cap, rescan overlap)
    PARAMS = {"eth": ("eth_rpc_url", 100, 200, 200, 3), "bsc": ("bsc_rpc_url", 2000, 4000, 0, 30)}

    def __init__(self, chain: str, methods):
        super().__init__(methods)
        self.chain = chain
        url_key, self.chunk, self.max_lookback, self.native_cap, self.overlap = self.PARAMS[chain]
        self.url = getattr(settings, url_key)
        self._head: int | None = None
        self.gap_warnings: list[str] = []

    async def rpc(self, method: str, params: list):
        r = await self.http.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RpcError(f"{method}: {data['error']}")
        return data["result"]

    async def rpc_batch(self, calls: list[tuple[str, list]]) -> list:
        body = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(calls)]
        r = await self.http.post(self.url, json=body)
        r.raise_for_status()
        res = sorted(r.json(), key=lambda x: x["id"])
        for x in res:
            if "error" in x:
                raise RpcError(str(x["error"]))
        return [x["result"] for x in res]

    async def head(self) -> int | None:
        self._head = int(await self.rpc("eth_blockNumber", []), 16)
        return self._head

    async def poll(self) -> list[Incoming]:
        head = await self.head()
        out: list[Incoming] = []
        for m in self.methods:
            if m.token:
                out += await self._poll_token(m, head)
            else:
                out += await self._poll_native(m, head)
        return out

    async def _start_block(self, key: str, head: int) -> int:
        cursor = await kv_get(key)
        start = (int(cursor) + 1) if cursor is not None else head - min(self.max_lookback, 150)
        if start < head - self.max_lookback:
            msg = f"{self.chain}: cursor {start} is {head - start} blocks behind, scanning only last {self.max_lookback}"
            log.warning(msg)
            self.gap_warnings.append(msg)
            start = head - self.max_lookback
        return start

    async def _poll_token(self, m, head: int) -> list[Incoming]:
        key = f"evm:{self.chain}:{m.code}:cursor"
        # re-scan a few recent blocks every time in case the node indexed logs late (dupes are ignored)
        start = min(await self._start_block(key, head), head - self.overlap)
        topic_to = "0x" + "0" * 24 + m.address[2:].lower()
        out: list[Incoming] = []
        frm = start
        while frm <= head:
            to = min(frm + self.chunk - 1, head)
            logs = await self.rpc("eth_getLogs", [{"fromBlock": hex(frm), "toBlock": hex(to), "address": m.token,
                                                   "topics": [TRANSFER_TOPIC, None, topic_to]}])
            for lg in logs:
                if lg.get("removed"):
                    continue
                units = int(lg["data"], 16) if lg.get("data") not in (None, "0x") else 0
                if units <= 0:
                    continue
                bn = int(lg["blockNumber"], 16)
                ts = lg.get("blockTimestamp")
                out.append(Incoming(
                    method=m.code, txid=lg["transactionHash"], idx=str(int(lg["logIndex"], 16)),
                    to_address=m.address, units=units, from_address="0x" + lg["topics"][1][-40:],
                    block_number=bn,
                    block_time=datetime.fromtimestamp(int(ts, 16), timezone.utc).replace(tzinfo=None) if ts else None,
                    confirmations=head - bn + 1, final=head - bn + 1 >= m.confirmations))
            await kv_set(key, to)
            frm = to + 1
        return out

    async def _poll_native(self, m, head: int) -> list[Incoming]:
        key = f"evm:{self.chain}:{m.code}"
        state = await kv_get(key + ":state") or {}
        bal_hex, nonce_hex = await self.rpc_batch([("eth_getBalance", [m.address, hex(head)]),
                                                   ("eth_getTransactionCount", [m.address, hex(head)])])
        bal, nonce = int(bal_hex, 16), int(nonce_hex, 16)
        out: list[Incoming] = []
        last_block = state.get("block")
        grew = bal > int(state.get("balance", bal)) or nonce != int(state.get("nonce", nonce))
        if last_block is not None and grew:
            start = max(int(last_block) + 1, head - self.native_cap)
            if start > int(last_block) + 1:
                self.gap_warnings.append(f"{self.chain}: native scan skipped blocks {int(last_block) + 1}..{start - 1}")
            found = 0
            for frm in range(start, head + 1, 10):
                blocks = await self.rpc_batch([("eth_getBlockByNumber", [hex(n), True])
                                               for n in range(frm, min(frm + 10, head + 1))])
                for b in blocks:
                    if not b:
                        continue
                    bn, ts = int(b["number"], 16), int(b["timestamp"], 16)
                    for tx in b.get("transactions") or []:
                        if (tx.get("to") or "").lower() != m.address.lower():
                            continue
                        value = int(tx.get("value") or "0x0", 16)
                        if value <= 0:
                            continue
                        found += 1
                        out.append(Incoming(
                            method=m.code, txid=tx["hash"], idx="0", to_address=m.address, units=value,
                            from_address=tx.get("from"), block_number=bn,
                            block_time=datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None),
                            confirmations=head - bn + 1, final=False))  # receipt status checked in verify()
            if not found and bal > int(state.get("balance", bal)):
                self.gap_warnings.append(
                    f"{self.chain}: balance of {m.address} grew by {bal - int(state['balance'])} wei in blocks "
                    f"{start}..{head} but no direct tx found (internal transfer?) — check manually")
        await kv_set(key + ":state", {"block": head, "balance": str(bal), "nonce": nonce})
        return out

    async def verify(self, txid: str, block_number: int | None) -> bool:
        rc = await self.rpc("eth_getTransactionReceipt", [txid])
        return bool(rc) and int(rc.get("status", "0x0"), 16) == 1
