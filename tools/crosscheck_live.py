"""Live check of the second-opinion providers (app/payments/crosscheck.py) on real, recent mainnet transfers.

    python tools\\crosscheck_live.py

For every chain it finds a fresh real transfer through the PRIMARY provider, then asks the second one:
- the true recipient and amount must come back OK;
- one unit more, or someone else's address, must come back MISMATCH.
Read-only public calls, no keys, nothing is sent anywhere. Run it after changing provider URLs, or when a chain's
payments start waiting "for the second source".
"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("SH_ENV_FILE", os.devnull)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import base58  # noqa: E402
import httpx  # noqa: E402

from app.payments.chains.tonutil import to_friendly  # noqa: E402
from app.payments.crosscheck import TRANSFER, Checker, Expect, Verdict  # noqa: E402
from app.payments.methods import METHODS  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 SupplierHub"}
results: list[tuple[str, bool, str]] = []


def b58(hex41: str) -> str:
    return base58.b58encode_check(bytes.fromhex(hex41)).decode()


async def rpc(c, url, method, params):
    r = await c.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    r.raise_for_status()
    return r.json()["result"]


async def judge(ck: Checker, name: str, e: Expect, other_addr: str):
    v, why = await ck.check(e)
    results.append((f"{name}: настоящий перевод → OK", v is Verdict.OK, f"{v.value}: {why}"))
    v2, why2 = await ck.check(replace(e, units=e.units + 1))
    results.append((f"{name}: сумма +1 → MISMATCH", v2 is Verdict.MISMATCH, f"{v2.value}: {why2}"))
    v3, why3 = await ck.check(replace(e, address=other_addr))
    results.append((f"{name}: чужой адрес → MISMATCH", v3 is Verdict.MISMATCH, f"{v3.value}: {why3}"))


async def tron(c, ck):
    m = METHODS["usdt_trc20"]
    ev = (await c.get(f"https://api.trongrid.io/v1/contracts/{m.token}/events",
                      params={"event_name": "Transfer", "only_confirmed": "true", "limit": 5})).json()["data"]
    e = next(x for x in ev if int(x["result"]["value"]) > 0)
    to = b58("41" + e["result"]["to"][2:])
    frm = b58("41" + e["result"]["from"][2:])
    await judge(ck, "TRON USDT", Expect(m, e["transaction_id"], "0", int(e["result"]["value"]), to), frm)
    now = (await c.post("https://api.trongrid.io/walletsolidity/getnowblock", json={})).json()
    n = now["block_header"]["raw_data"]["number"] - 20
    blk = (await c.post("https://api.trongrid.io/walletsolidity/getblockbynum", json={"num": n})).json()
    tx = next(t for t in blk["transactions"] if t["raw_data"]["contract"][0]["type"] == "TransferContract"
              and (t.get("ret") or [{}])[0].get("contractRet") == "SUCCESS")
    v = tx["raw_data"]["contract"][0]["parameter"]["value"]
    await judge(ck, "TRON TRX", Expect(METHODS["trx"], tx["txID"], "0", int(v["amount"]), b58(v["to_address"])),
                b58(v["owner_address"]))


async def evm(c, ck, chain, primary, code_token, code_native):
    m = METHODS[code_token]
    head = int(await rpc(c, primary, "eth_blockNumber", []), 16)
    deep = head - m.confirmations - 5
    logs = await rpc(c, primary, "eth_getLogs", [{"fromBlock": hex(deep - 3), "toBlock": hex(deep), "address": m.token,
                                                   "topics": ["0x" + TRANSFER]}])
    lg = next(x for x in logs if int(x["data"], 16) > 0)
    await judge(ck, f"{chain.upper()} USDT", Expect(m, lg["transactionHash"], str(int(lg["logIndex"], 16)),
                                                    int(lg["data"], 16), "0x" + lg["topics"][2][-40:]),
                "0x" + lg["topics"][1][-40:])
    if code_native:
        blk = await rpc(c, primary, "eth_getBlockByNumber", [hex(deep), True])
        tx = next(t for t in blk["transactions"] if t.get("to") and int(t.get("value") or "0x0", 16) > 0
                  and t.get("input") in ("0x", ""))
        await judge(ck, f"{chain.upper()} native", Expect(METHODS[code_native], tx["hash"], "0", int(tx["value"], 16), tx["to"]),
                    tx["from"])


async def utxo(c, ck, chain, primary, depth):
    m = METHODS[chain]
    tip = int((await c.get(f"{primary}/blocks/tip/height")).text)
    h = (await c.get(f"{primary}/block-height/{tip - depth}")).text.strip()
    txids = (await c.get(f"{primary}/block/{h}/txids")).json()
    for t in txids[1:30]:
        tx = (await c.get(f"{primary}/tx/{t}")).json()
        outs = [o for o in tx["vout"] if o.get("scriptpubkey_address") and int(o.get("value") or 0) > 0]
        if len(outs) >= 2 and outs[0]["scriptpubkey_address"] != outs[1]["scriptpubkey_address"]:
            addr = outs[0]["scriptpubkey_address"]
            units = sum(int(o["value"]) for o in outs if o["scriptpubkey_address"] == addr)
            await judge(ck, chain.upper(), Expect(m, t, "0", units, addr), outs[1]["scriptpubkey_address"])
            return
    results.append((f"{chain.upper()}: подходящая транзакция не найдена", False, ""))


async def ton(c, ck):
    m = METHODS["usdt_ton"]
    tr = (await c.get("https://toncenter.com/api/v3/jetton/transfers",
                      params={"jetton_master": m.token, "limit": 10, "sort": "desc"})).json()["jetton_transfers"]
    await asyncio.sleep(1.2)
    for t in tr:
        if int(t.get("amount") or 0) <= 0:
            continue
        w = (await c.get("https://toncenter.com/api/v3/jetton/wallets",
                         params={"owner_address": t["destination"], "jetton_address": m.token, "limit": 1})).json()["jetton_wallets"]
        await asyncio.sleep(1.2)
        if not w:
            continue
        txs = (await c.get("https://toncenter.com/api/v3/transactions",
                           params={"account": w[0]["address"], "limit": 10, "sort": "desc"})).json()["transactions"]
        await asyncio.sleep(1.2)
        import base64
        rx = next((x for x in txs if ((x.get("in_msg") or {}).get("message_content") or {}).get("decoded", {}).get("@type")
                   == "jetton_internal_transfer" and not (x.get("description") or {}).get("aborted")), None)
        if not rx:
            continue
        amount = int(rx["in_msg"]["message_content"]["decoded"]["amount"]["value"]) if isinstance(
            rx["in_msg"]["message_content"]["decoded"].get("amount"), dict) else int(rx["in_msg"]["message_content"]["decoded"]["amount"])
        owner = to_friendly(t["destination"])
        await judge(ck, "TON USDT", Expect(m, base64.b64decode(rx["hash"]).hex(), "j", amount, owner), to_friendly(t["source"]))
        await asyncio.sleep(1.2)
        own = (await c.get("https://toncenter.com/api/v3/transactions",
                           params={"account": t["destination"], "limit": 15, "sort": "desc"})).json()["transactions"]
        for x in own:
            im = x.get("in_msg") or {}
            if im.get("source") and int(im.get("value") or 0) > 0 and not im.get("bounced"):
                await judge(ck, "TON native", Expect(METHODS["ton"], base64.b64decode(x["hash"]).hex(), "0",
                                                     int(im["value"]), owner), to_friendly(t["source"]))
                return
        return
    results.append(("TON: подходящий перевод не найден", False, ""))


class _Wallet:
    """A payment method whose wallet is someone else's (the recipient of a live transfer we test on)."""

    def __init__(self, m, address):
        self._m, self.address = m, address

    def __getattr__(self, name):
        return getattr(self._m, name)


async def sol(c, ck):
    """Through the production parser (SolanaWatcher._parse) and then the second opinion — on live transactions,
    including the new version-1 ones."""
    from app.payments.chains.solana import SolanaWatcher

    m = METHODS["usdt_sol"]
    url = "https://api.mainnet-beta.solana.com"
    sigs = await rpc(c, url, "getSignaturesForAddress", [m.token, {"limit": 40, "commitment": "finalized"}])
    # the second RPC serves "finalized" history ~10-15 s behind: in production the first look says UNKNOWN and the
    # retry a few seconds later says OK; here we just let the transfers age first
    await asyncio.sleep(45)
    w = SolanaWatcher([])
    done: dict[str, bool] = {"usdt": False, "sol": False}
    versions = set()
    for s in sigs:
        if s.get("err") or all(done.values()):
            continue
        r = await c.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "getTransaction", "params": [
            s["signature"], {"encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 1}]})
        await asyncio.sleep(0.4)
        tx = r.json().get("result") if r.status_code == 200 else None
        if not tx or (tx.get("meta") or {}).get("err"):
            continue
        versions.add(str(tx.get("version")))
        meta = tx["meta"]
        keys = tx["transaction"]["message"]["accountKeys"]
        if not done["usdt"]:
            gains: dict[str, int] = {}
            for side, sign in (("postTokenBalances", 1), ("preTokenBalances", -1)):
                for b in meta.get(side) or []:
                    if b.get("mint") == m.token and b.get("owner"):
                        gains[b["owner"]] = gains.get(b["owner"], 0) + sign * int(b["uiTokenAmount"]["amount"])
            up = [o for o, d in gains.items() if d > 0 and not any(k.get("signer") and k["pubkey"] == o for k in keys)]
            down = [o for o, d in gains.items() if d < 0]
            if up and down:
                wm = _Wallet(m, up[0])
                inc = w._parse(wm, s["signature"], tx)
                if inc:
                    await judge(ck, f"SOL USDT (v{tx.get('version')})", Expect(wm, s["signature"], inc.idx, inc.units, up[0]),
                                down[0])
                    done["usdt"] = True
        if not done["sol"]:
            for i, k in enumerate(keys):
                d = meta["postBalances"][i] - meta["preBalances"][i]
                if not k.get("signer") and d > 0 and k["pubkey"] != m.token:
                    wm = _Wallet(METHODS["sol"], k["pubkey"])
                    inc = w._parse(wm, s["signature"], tx)
                    if inc:
                        await judge(ck, f"SOL native (v{tx.get('version')})", Expect(wm, s["signature"], inc.idx, inc.units,
                                                                                     k["pubkey"]), keys[0]["pubkey"])
                        done["sol"] = True
                    break
    results.append((f"SOL: разобраны транзакции версий {sorted(versions)}", bool(versions), ""))
    if not all(done.values()):
        results.append((f"SOL: нет живого перевода {done}", False, f"{len(sigs)} подписей"))

async def main():
    c = httpx.AsyncClient(timeout=25, headers=UA)
    ck = Checker()
    jobs = [("TRON", tron(c, ck)),
            ("ETH", evm(c, ck, "eth", "https://ethereum-rpc.publicnode.com", "usdt_erc20", "eth")),
            ("BSC", evm(c, ck, "bsc", "https://bsc-rpc.publicnode.com", "usdt_bep20", None)),
            ("BTC", utxo(c, ck, "btc", "https://mempool.space/api", 3)),
            ("LTC", utxo(c, ck, "ltc", "https://litecoinspace.org/api", 6)),
            ("TON", ton(c, ck)), ("SOL", sol(c, ck))]
    for name, job in jobs:
        try:
            await job
        except Exception as e:  # noqa: BLE001
            results.append((f"{name}: не удалось найти живой перевод", False, f"{type(e).__name__}: {str(e)[:160]}"))
    bad = 0
    for title, ok, detail in results:
        bad += not ok
        print(f"  {'✓' if ok else '✗'} {title}" + (f"   [{detail}]" if not ok or "--v" in sys.argv else ""))
    print(f"\n{'ALL GOOD' if not bad else 'PROBLEMS'}: {len(results) - bad} ok, {bad} failed")
    sys.exit(1 if bad else 0)


asyncio.run(main())
