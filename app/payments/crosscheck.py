"""Second opinion on a payment before it's credited.

A payment is seen by our watcher through one provider (TronGrid, toncenter, a public RPC...). If that provider
glitches, lags behind a reorg, or lies, we'd credit money that never arrived. So right before crediting, the same
transfer is looked up at an INDEPENDENT provider (another operator) and must match exactly: the transaction
succeeded and is final, the money went to OUR address, the amount is the same to the last unit, and for USDT it's
the real USDT contract / mint / jetton.

Three outcomes:
- OK        — credit;
- MISMATCH  — the second provider sees something else: don't credit, hold for an admin;
- UNKNOWN   — it can't tell (not indexed yet, down, rate-limited): try again later, then ask an admin.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

import base58
import httpx

from ..config import settings
from .chains.tonutil import to_raw
from .methods import Method

log = logging.getLogger("sh.crosscheck")

TRANSFER = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class Verdict(str, Enum):
    OK = "ok"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"


@dataclass
class Expect:
    """What our watcher says arrived."""
    method: Method
    txid: str
    idx: str
    units: int
    address: str  # ours


class Unavailable(Exception):
    """The provider didn't answer usefully (network, 5xx, rate limit)."""


def _urls(raw: str) -> list[str]:
    return [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]


def tron_hex(addr: str) -> str:
    """T... (base58) → 41... (hex, as the node API shows it)."""
    return base58.b58decode_check(addr).hex().lower()


class Checker:
    def __init__(self, http: httpx.AsyncClient | None = None):
        self.http = http or httpx.AsyncClient(timeout=15, headers={"User-Agent": "Mozilla/5.0 SupplierHub"})
        self._jetton_wallets: dict[str, str] = {}

    async def check(self, e: Expect, *, skip: str | None = None) -> tuple[Verdict, str]:
        """`skip`: a provider base URL not to use (the one our watcher read the payment from)."""
        fn = getattr(self, f"_{e.method.chain}", None)
        if fn is None:
            return Verdict.OK, "нет второго источника для этой сети"
        try:
            return await fn(e, skip)
        except Unavailable as err:
            return Verdict.UNKNOWN, f"второй источник недоступен: {err}"
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as err:
            return Verdict.UNKNOWN, f"второй источник ответил непонятно: {type(err).__name__}: {str(err)[:150]}"

    # ------------------------------------------------------------------ plumbing
    async def _json(self, method: str, url: str, **kw):
        r = await (self.http.post(url, **kw) if method == "POST" else self.http.get(url, **kw))
        if r.status_code == 404:
            return None
        if r.status_code == 429 or r.status_code >= 500:
            raise Unavailable(f"{url.split('/')[2]}: HTTP {r.status_code}")
        r.raise_for_status()
        return r.json()

    async def _rpc(self, url: str, method: str, params: list):
        data = await self._json("POST", url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if data is None or "error" in data:
            raise Unavailable(f"{url.split('/')[2]}: {(data or {}).get('error')}")
        return data.get("result")

    async def _first(self, urls: list[str], skip: str | None, fn):
        """Ask the providers in order (skipping ours); the first one that answers decides."""
        errors = []
        for u in urls:
            if skip and u.rstrip("/") == skip.rstrip("/"):
                continue
            try:
                return await fn(u)
            except Unavailable as err:
                errors.append(str(err))
        raise Unavailable("; ".join(errors) or "нет независимого источника")

    # ------------------------------------------------------------------ TRON (node API, solidified data only)
    async def _tron(self, e: Expect, skip):
        base = settings.tron_check_url.rstrip("/")
        ours = tron_hex(e.address)
        if e.method.token:
            info = await self._json("POST", f"{base}/walletsolidity/gettransactioninfobyid", json={"value": e.txid})
            if not info:
                return Verdict.UNKNOWN, "второй источник ещё не видит транзакцию подтверждённой"
            if (info.get("receipt") or {}).get("result") != "SUCCESS":
                return Verdict.MISMATCH, f"транзакция не прошла: {(info.get('receipt') or {}).get('result')}"
            token = tron_hex(e.method.token)[2:]
            got = [int(lg.get("data") or "0", 16) for lg in info.get("log") or []
                   if (lg.get("address") or "").lower() == token and (lg.get("topics") or [""])[0] == TRANSFER
                   and len(lg.get("topics") or []) > 2 and lg["topics"][2][-40:].lower() == ours[2:]]
            if e.units in got or (got and sum(got) == e.units):
                return Verdict.OK, "TRON: подтверждено вторым источником"
            return Verdict.MISMATCH, f"на наш адрес по этой транзакции USDT: {got or 'ничего'}, ждали {e.units}"
        tx = await self._json("POST", f"{base}/walletsolidity/gettransactionbyid", json={"value": e.txid})
        if not tx:
            return Verdict.UNKNOWN, "второй источник ещё не видит транзакцию подтверждённой"
        if ((tx.get("ret") or [{}])[0]).get("contractRet") != "SUCCESS":
            return Verdict.MISMATCH, "транзакция не прошла"
        ct = tx["raw_data"]["contract"][0]
        v = ct["parameter"]["value"]
        if ct.get("type") != "TransferContract" or (v.get("to_address") or "").lower() != ours:
            return Verdict.MISMATCH, "TRX ушли не на наш адрес"
        if int(v.get("amount") or 0) != e.units:
            return Verdict.MISMATCH, f"сумма {v.get('amount')}, ждали {e.units}"
        return Verdict.OK, "TRON: подтверждено вторым источником"

    # ------------------------------------------------------------------ Ethereum / BNB Smart Chain (JSON-RPC)
    async def _eth(self, e, skip):
        return await self._first(_urls(settings.eth_check_urls), skip, lambda u: self._evm(u, e))

    async def _bsc(self, e, skip):
        return await self._first(_urls(settings.bsc_check_urls), skip, lambda u: self._evm(u, e))

    async def _evm(self, url: str, e: Expect):
        head = int(await self._rpc(url, "eth_blockNumber", []), 16)
        rc = await self._rpc(url, "eth_getTransactionReceipt", [e.txid])
        if not rc:
            return Verdict.UNKNOWN, "второй источник ещё не видит транзакцию"
        if int(rc.get("status") or "0x0", 16) != 1:
            return Verdict.MISMATCH, "транзакция не прошла (status 0)"
        confs = head - int(rc["blockNumber"], 16) + 1
        ours = e.address.lower()
        if e.method.token:
            lg = next((x for x in rc.get("logs") or [] if str(int(x["logIndex"], 16)) == e.idx), None)
            if not lg:
                return Verdict.MISMATCH, f"в транзакции нет перевода №{e.idx}"
            ok = (lg["address"].lower() == e.method.token.lower() and lg["topics"][0].lower().endswith(TRANSFER)
                  and lg["topics"][2][-40:].lower() == ours[2:] and int(lg["data"], 16) == e.units)
            if not ok:
                return Verdict.MISMATCH, "перевод не того токена, не на наш адрес или на другую сумму"
        else:
            tx = await self._rpc(url, "eth_getTransactionByHash", [e.txid])
            if not tx or (tx.get("to") or "").lower() != ours or int(tx.get("value") or "0x0", 16) != e.units:
                return Verdict.MISMATCH, "не на наш адрес или на другую сумму"
        if confs < e.method.confirmations:
            return Verdict.UNKNOWN, f"у второго источника {confs} подтверждений из {e.method.confirmations}"
        return Verdict.OK, f"{e.method.chain.upper()}: подтверждено вторым источником ({confs} подтв.)"

    # ------------------------------------------------------------------ Bitcoin / Litecoin
    async def _btc(self, e, skip):
        return await self._first(_urls(settings.btc_check_urls), skip, lambda u: self._utxo(u, e))

    async def _ltc(self, e, skip):
        return await self._first(_urls(settings.ltc_check_urls), skip, lambda u: self._utxo(u, e))

    async def _utxo(self, base: str, e: Expect):
        if "blockcypher" in base:
            tx = await self._json("GET", f"{base}/txs/{e.txid}")
            if not tx:
                return Verdict.UNKNOWN, "второй источник ещё не видит транзакцию"
            got = sum(int(o.get("value") or 0) for o in tx.get("outputs") or [] if e.address in (o.get("addresses") or []))
            confs = int(tx.get("confirmations") or 0)
        elif "/api/v2" in base:  # Blockbook
            tx = await self._json("GET", f"{base}/tx/{e.txid}")
            if not tx or tx.get("error"):
                return Verdict.UNKNOWN, "второй источник ещё не видит транзакцию"
            got = sum(int(o.get("value") or 0) for o in tx.get("vout") or [] if e.address in (o.get("addresses") or []))
            confs = int(tx.get("confirmations") or 0)
        else:  # esplora
            tx = await self._json("GET", f"{base}/tx/{e.txid}")
            if not tx:
                return Verdict.UNKNOWN, "второй источник ещё не видит транзакцию"
            got = sum(int(o.get("value") or 0) for o in tx.get("vout") or [] if o.get("scriptpubkey_address") == e.address)
            st = tx.get("status") or {}
            confs = 0
            if st.get("confirmed"):
                tip = await self.http.get(f"{base}/blocks/tip/height")
                if tip.status_code != 200:
                    raise Unavailable(f"{base}: tip height HTTP {tip.status_code}")
                confs = int(tip.text) - int(st["block_height"]) + 1
        if got != e.units:
            return Verdict.MISMATCH, f"на наш адрес пришло {got}, ждали {e.units}"
        if confs < e.method.confirmations:
            return Verdict.UNKNOWN, f"у второго источника {confs} подтверждений из {e.method.confirmations}"
        return Verdict.OK, f"{e.method.chain.upper()}: подтверждено вторым источником ({confs} подтв.)"

    # ------------------------------------------------------------------ TON (tonapi)
    def _tonapi_headers(self) -> dict:
        return {"Authorization": f"Bearer {settings.tonapi_key}"} if settings.tonapi_key else {}

    async def jetton_wallet(self, owner: str, master: str) -> str:
        """Our jetton wallet for this master, worked out by the second provider itself (not taken from the first)."""
        key = f"{owner}|{master}"
        if key not in self._jetton_wallets:
            base = settings.tonapi_url.rstrip("/")
            data = await self._json("GET", f"{base}/v2/accounts/{to_raw(owner)}/jettons/{to_raw(master)}",
                                    headers=self._tonapi_headers())
            if not data or not (data.get("wallet_address") or {}).get("address"):
                raise Unavailable("tonapi не назвал наш jetton-кошелёк")
            self._jetton_wallets[key] = data["wallet_address"]["address"].lower()
        return self._jetton_wallets[key]

    async def _ton(self, e: Expect, skip):
        base = settings.tonapi_url.rstrip("/")
        tx = await self._json("GET", f"{base}/v2/blockchain/transactions/{e.txid}", headers=self._tonapi_headers())
        if not tx:
            return Verdict.UNKNOWN, "второй источник ещё не видит транзакцию"
        im = tx.get("in_msg") or {}
        account = ((tx.get("account") or {}).get("address") or "").lower()
        if e.method.token:
            jw = await self.jetton_wallet(e.address, e.method.token)
            if account != jw:
                return Verdict.MISMATCH, "это не наш USDT-кошелёк (подделка жетона?)"
            if im.get("decoded_op_name") != "jetton_internal_transfer" or not tx.get("success") or tx.get("aborted"):
                return Verdict.MISMATCH, "это не успешное поступление USDT"
            amount = int(((im.get("decoded_body") or {}).get("amount")) or 0)
            if amount != e.units:
                return Verdict.MISMATCH, f"USDT пришло {amount}, ждали {e.units}"
            return Verdict.OK, "TON: подтверждено вторым источником"
        if account != to_raw(e.address).lower():
            return Verdict.MISMATCH, "транзакция не на нашем кошельке"
        if im.get("bounced") or int(im.get("value") or 0) != e.units:
            return Verdict.MISMATCH, f"пришло {im.get('value')}, ждали {e.units}"
        src = ((im.get("source") or {}).get("address") or "").lower()
        if tx.get("aborted") and any(((o.get("destination") or {}).get("address") or "").lower() == src
                                     for o in tx.get("out_msgs") or []):
            return Verdict.MISMATCH, "монеты вернулись отправителю"
        return Verdict.OK, "TON: подтверждено вторым источником"

    # ------------------------------------------------------------------ Solana (JSON-RPC, finalized)
    async def _sol(self, e: Expect, skip):
        url = settings.sol_check_url
        tx = await self._rpc(url, "getTransaction", [e.txid, {"encoding": "jsonParsed", "commitment": "finalized",
                                                              "maxSupportedTransactionVersion": 1}])
        if not tx:
            return Verdict.UNKNOWN, "второй источник ещё не видит транзакцию финальной"
        meta = tx.get("meta") or {}
        if meta.get("err") is not None:
            return Verdict.MISMATCH, "транзакция не прошла"
        if e.method.token:
            def bal(entries):
                return {b["accountIndex"]: int(b["uiTokenAmount"]["amount"]) for b in entries or []
                        if b.get("mint") == e.method.token and b.get("owner") == e.address}
            pre, post = bal(meta.get("preTokenBalances")), bal(meta.get("postTokenBalances"))
            delta = sum(post.get(i, 0) - pre.get(i, 0) for i in set(pre) | set(post))
        else:
            keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
            if e.address not in keys:
                return Verdict.MISMATCH, "наш адрес не участвует в транзакции"
            i = keys.index(e.address)
            delta = meta["postBalances"][i] - meta["preBalances"][i]
        if delta != e.units:
            return Verdict.MISMATCH, f"на наш адрес пришло {delta}, ждали {e.units}"
        return Verdict.OK, "Solana: подтверждено вторым источником"


checker = Checker()
