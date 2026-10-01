"""Second opinion on every payment before it's credited (app/payments/crosscheck.py + monitor).

Run:  .venv\\Scripts\\python.exe tests\\test_crosscheck.py
The providers are fakes answering in the formats recorded from mainnet (tools/crosscheck_live.py checks the real
ones). Throw-away database, mock supplier: nothing leaves the machine.
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import base58

ME_TRON = base58.b58encode_check(bytes.fromhex("41" + "ab" * 20)).decode()  # valid checksum, nobody's wallet
OTHER_TRON = base58.b58encode_check(bytes.fromhex("41" + "cd" * 20)).decode()
os.environ.update({"SH_ENV_FILE": os.devnull, "ENV": "prod", "DATA_DIR": tempfile.mkdtemp(prefix="sh-xcheck-"),
                   "NERVIXY_MOCK": "true", "BOT_TOKEN": "", "ADMIN_TG_IDS": "999", "MONITOR_ENABLED": "false",
                   "BASE_URL": "https://sh.test", "WALLET_USDT_TRC20": ME_TRON})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import outbox  # noqa: E402
from app import orders as O  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import Invoice, OutboxMessage, Transfer, User  # noqa: E402
from app.payments import crosscheck as X  # noqa: E402
from app.payments import monitor as M  # noqa: E402
from app.payments.chains.base import Incoming  # noqa: E402
from app.payments.crosscheck import TRANSFER, Checker, Expect, Verdict  # noqa: E402
from app.payments.methods import METHODS  # noqa: E402
from app.utils import utcnow  # noqa: E402

OK = 0
ME_EVM = "0x000000000000000000000000000000000000dead"


def check(cond, msg):
    global OK
    if not cond:
        raise AssertionError(msg)
    OK += 1
    print("  ✓", msg)


def provider(routes):
    """A fake HTTP world: routes = {(method, url-substring): response | callable(request)}."""
    calls = []

    def handler(req: httpx.Request):
        calls.append(str(req.url))
        for (meth, part), resp in routes.items():
            if req.method == meth and part in str(req.url):
                if callable(resp):
                    resp = resp(req)
                if isinstance(resp, httpx.Response):
                    return resp
                return httpx.Response(200, json=resp)
        return httpx.Response(404, json={})
    return Checker(httpx.AsyncClient(transport=httpx.MockTransport(handler))), calls


def rpc(results: dict):
    """JSON-RPC answers by method name."""
    def answer(req):
        body = json.loads(req.content)
        r = results.get(body["method"])
        if isinstance(r, httpx.Response):
            return r
        return {"jsonrpc": "2.0", "id": 1, "result": r}
    return answer


async def unit():
    print("1. TRON")
    usdt = METHODS["usdt_trc20"]
    me = X.tron_hex(ME_TRON)
    token = X.tron_hex(usdt.token)[2:]
    log = {"address": token, "topics": [TRANSFER, "0" * 24 + "2a68baf67f1c497d9a4a609276a90dcd6ea77444", "0" * 24 + me[2:]],
           "data": f"{98_500_000:064x}"}
    info = {"id": "abc", "blockNumber": 1, "receipt": {"result": "SUCCESS"}, "log": [log]}
    ck, _ = provider({("POST", "gettransactioninfobyid"): info})
    e = Expect(usdt, "abc", "0", 98_500_000, ME_TRON)
    check((await ck.check(e))[0] is Verdict.OK, "USDT на наш адрес, та сумма, тот контракт → OK")
    check((await ck.check(Expect(usdt, "abc", "0", 98_500_001, ME_TRON)))[0] is Verdict.MISMATCH, "сумма на 1 больше → MISMATCH")
    fake = {**info, "log": [{**log, "address": "1" * 40}]}
    ck, _ = provider({("POST", "gettransactioninfobyid"): fake})
    check((await ck.check(e))[0] is Verdict.MISMATCH, "«USDT» другого контракта (подделка токена) → MISMATCH")
    ck, _ = provider({("POST", "gettransactioninfobyid"): {**info, "receipt": {"result": "REVERT"}}})
    check((await ck.check(e))[0] is Verdict.MISMATCH, "транзакция откатилась → MISMATCH")
    ck, _ = provider({("POST", "gettransactioninfobyid"): {}})
    check((await ck.check(e))[0] is Verdict.UNKNOWN, "второй узел ещё не видит её подтверждённой → UNKNOWN (подождём)")
    ck, _ = provider({("POST", "gettransactioninfobyid"): httpx.Response(429, json={})})
    check((await ck.check(e))[0] is Verdict.UNKNOWN, "второй узел ограничил запросы → UNKNOWN, а не «не совпало»")
    trx = {"ret": [{"contractRet": "SUCCESS"}], "raw_data": {"contract": [{"type": "TransferContract", "parameter": {
        "value": {"owner_address": "41" + "1" * 40, "to_address": me, "amount": 5_000_000}}}]}}
    ck, _ = provider({("POST", "gettransactionbyid"): trx})
    check((await ck.check(Expect(METHODS["trx"], "t", "0", 5_000_000, ME_TRON)))[0] is Verdict.OK, "TRX: OK")
    check((await ck.check(Expect(METHODS["trx"], "t", "0", 5_000_000, OTHER_TRON)))[0]
          is Verdict.MISMATCH, "TRX ушли на чужой адрес → MISMATCH")

    print("2. Ethereum / BSC")
    erc = METHODS["usdt_erc20"]
    receipt = {"status": "0x1", "blockNumber": hex(100), "logs": [
        {"address": erc.token.lower(), "logIndex": "0x7", "data": hex(25_000_000),
         "topics": ["0x" + TRANSFER, "0x" + "0" * 24 + "1" * 40, "0x" + "0" * 24 + ME_EVM[2:]]}]}
    world = {("POST", "drpc"): rpc({"eth_blockNumber": hex(120), "eth_getTransactionReceipt": receipt}),
             ("POST", "1rpc"): rpc({"eth_blockNumber": hex(120), "eth_getTransactionReceipt": receipt})}
    ck, calls = provider(world)
    e = Expect(erc, "0xabc", "7", 25_000_000, ME_EVM)
    check((await ck.check(e))[0] is Verdict.OK, "ERC20: тот перевод №7, наш адрес, сумма, 21 подтверждение → OK")
    check((await ck.check(Expect(erc, "0xabc", "8", 25_000_000, ME_EVM)))[0] is Verdict.MISMATCH, "перевода №8 в транзакции нет → MISMATCH")
    world[("POST", "drpc")] = rpc({"eth_blockNumber": hex(105), "eth_getTransactionReceipt": receipt})
    ck, _ = provider(world)
    v, why = await ck.check(e)
    check(v is Verdict.UNKNOWN and "6 подтверждений из 12" in why, f"у второго узла мало подтверждений → ждём ({why})")
    ck, calls = provider({("POST", "drpc"): httpx.Response(503), ("POST", "1rpc"): rpc({"eth_blockNumber": hex(120),
                                                                                       "eth_getTransactionReceipt": receipt})})
    check((await ck.check(e))[0] is Verdict.OK and any("1rpc" in u for u in calls), "первый второй-источник лежит → спросили запасной")
    ck, calls = provider(world)
    await ck.check(e, skip="https://eth.drpc.org")
    check(not any("drpc" in u for u in calls), "провайдер, от которого узнали о платеже, вторым мнением не считается")

    print("3. Bitcoin / Litecoin")
    btc = METHODS["btc"]
    tx = {"status": {"confirmed": True, "block_height": 100}, "vout": [{"scriptpubkey_address": "bc1me", "value": 150_000},
                                                                      {"scriptpubkey_address": "bc1other", "value": 99}]}
    ck, _ = provider({("GET", "/tx/"): tx, ("GET", "/blocks/tip/height"): httpx.Response(200, text="100")})
    check((await ck.check(Expect(btc, "t", "0", 150_000, "bc1me")))[0] is Verdict.OK, "BTC: наш выход 150 000 сат, 1 подтверждение → OK")
    check((await ck.check(Expect(btc, "t", "0", 150_099, "bc1me")))[0] is Verdict.MISMATCH, "сумма другая → MISMATCH")
    ltc = METHODS["ltc"]
    bb = {"confirmations": 1, "vout": [{"addresses": ["ltc1me"], "value": "500000"}]}
    ck, _ = provider({("GET", "litecoinblockexplorer"): bb})
    v, why = await ck.check(Expect(ltc, "t", "0", 500_000, "ltc1me"))
    check(v is Verdict.UNKNOWN and "1 подтверждений из 3" in why, "LTC: 1 подтверждение из 3 → ждём")
    ck, _ = provider({("GET", "litecoinblockexplorer"): httpx.Response(502),
                      ("GET", "blockcypher"): {"confirmations": 4, "outputs": [{"addresses": ["ltc1me"], "value": 500000}]}})
    check((await ck.check(Expect(ltc, "t", "0", 500_000, "ltc1me")))[0] is Verdict.OK, "Blockbook лежит → ответил BlockCypher: OK")

    print("4. TON")
    jet = METHODS["usdt_ton"]
    owner = "UQDUjx2Q41_LMHd8ebSSgwW8U7m7kJzpYfJQ7nSZ0dlnGikm"
    jw = "0:856a5546e61c37a5ab56ad8a5beef5481950d831bf4c9637464631909ef8807f"
    rx = {"account": {"address": jw}, "success": True, "aborted": False,
          "in_msg": {"decoded_op_name": "jetton_internal_transfer", "decoded_body": {"amount": "5700000"}}}
    world = {("GET", "/jettons/"): {"wallet_address": {"address": jw}}, ("GET", "/blockchain/transactions/"): rx}
    ck, _ = provider(world)
    check((await ck.check(Expect(jet, "h", "j", 5_700_000, owner)))[0] is Verdict.OK, "USDT на наш jetton-кошелёк → OK")
    ck, _ = provider({("GET", "/jettons/"): {"wallet_address": {"address": "0:" + "f" * 64}}, ("GET", "/blockchain/transactions/"): rx})
    check((await ck.check(Expect(jet, "h", "j", 5_700_000, owner)))[0] is Verdict.MISMATCH,
          "транзакция не на том кошельке, который второй источник сам вычислил для USDT → MISMATCH (поддельный жетон)")
    native = {"account": {"address": "0:d48f1d90e35fcb30777c79b4928305bc53b9bb909ce961f250ee7499d1d9671a"},
              "success": True, "aborted": False, "in_msg": {"value": 2_000_000_000, "bounced": False, "source": {"address": "0:aa"}}}
    ck, _ = provider({("GET", "/blockchain/transactions/"): native})
    check((await ck.check(Expect(METHODS["ton"], "h", "0", 2_000_000_000, owner)))[0] is Verdict.OK, "TON: 2 TON на наш кошелёк → OK")
    ck, _ = provider({("GET", "/blockchain/transactions/"): {**native, "aborted": True,
                                                              "out_msgs": [{"destination": {"address": "0:aa"}}]}})
    check((await ck.check(Expect(METHODS["ton"], "h", "0", 2_000_000_000, owner)))[0] is Verdict.MISMATCH,
          "монеты вернулись отправителю (bounce) → MISMATCH")

    print("5. Solana")
    spl = METHODS["usdt_sol"]
    stx = {"meta": {"err": None, "preTokenBalances": [{"accountIndex": 2, "mint": spl.token, "owner": "SoMe",
                                                        "uiTokenAmount": {"amount": "1000"}}],
                    "postTokenBalances": [{"accountIndex": 2, "mint": spl.token, "owner": "SoMe",
                                           "uiTokenAmount": {"amount": "3500"}}]}}
    ck, _ = provider({("POST", "publicnode"): rpc({"getTransaction": stx})})
    check((await ck.check(Expect(spl, "s", "t", 2500, "SoMe")))[0] is Verdict.OK, "USDT-SPL: +2500 нашему владельцу → OK")
    ck, _ = provider({("POST", "publicnode"): rpc({"getTransaction": {**stx, "meta": {**stx["meta"], "err": {"x": 1}}}})})
    check((await ck.check(Expect(spl, "s", "t", 2500, "SoMe")))[0] is Verdict.MISMATCH, "транзакция с ошибкой → MISMATCH")
    ck, _ = provider({("POST", "publicnode"): rpc({"getTransaction": None})})
    check((await ck.check(Expect(spl, "s", "t", 2500, "SoMe")))[0] is Verdict.UNKNOWN, "ещё не финальная у второго узла → UNKNOWN")


class FakeWatcher:
    methods = []

    async def verify(self, txid, bn):
        return True


async def flow():
    await init_db()
    from app.prices import price_feed
    price_feed.updated_at = time.time()
    verdicts: list = []

    async def fake_check(e, *, skip=None):
        return verdicts.pop(0) if verdicts else (Verdict.OK, "ok")
    X.checker.check = fake_check
    watchers = {"tron": FakeWatcher()}
    n = 0

    async def paid_order(tg):
        nonlocal n
        n += 1
        async with session_scope() as s:
            u = User(tg_id=tg, username=f"u{tg}")
            s.add(u)
            await s.flush()
            uid = u.id
        async with session_scope() as s:
            o = await O.create_order(s, await s.get(User, uid), steam_login="gooduser", amount=Decimal("1000"),
                                     currency="RUB", method_code="usdt_trc20")
        async with session_scope() as s:
            inv = (await s.execute(select(Invoice).where(Invoice.order_id == o.id))).scalar_one()
        m = METHODS["usdt_trc20"]
        await M.ingest([Incoming(method="usdt_trc20", txid=f"real{n:060d}", idx="0", to_address=m.address,
                                 units=int(inv.units), block_time=utcnow(), confirmations=1, final=True)])
        async with session_scope() as s:
            t = (await s.execute(select(Transfer).where(Transfer.txid == f"real{n:060d}"))).scalar_one()
        return uid, o.id, t.id

    async def state(tid):
        async with session_scope() as s:
            t = await s.get(Transfer, tid)
            bal = (await s.get(User, t.user_id)).balance_micro if t.user_id else None
            return t, bal

    async def admin_msgs():
        async with session_scope() as s:
            return (await s.execute(select(OutboxMessage).where(OutboxMessage.chat_id == 999)
                                    .order_by(OutboxMessage.id))).scalars().all()

    print("6. всё сошлось → зачислено")
    uid, oid, tid = await paid_order(10)
    await M.credit_ready(watchers)
    t, _ = await state(tid)
    check(t.status == "credited" and t.xcheck_ok, "второй источник подтвердил → платёж зачислен, заказ оплачен")

    print("7. второй источник видит другое → деньги стоят, решает админ")
    verdicts[:] = [(Verdict.MISMATCH, "на наш адрес по этой транзакции USDT: ничего")]
    uid, oid, tid = await paid_order(11)
    await M.credit_ready(watchers)
    t, bal = await state(tid)
    msgs = await admin_msgs()
    check(t.status == "held" and (bal or 0) == 0, "не совпало → платёж задержан, клиенту ничего не зачислено")
    card = msgs[-1]
    check("не подтвердился" in card.text and f"tx:ok:{tid}" in card.markup and f"tx:no:{tid}" in card.markup,
          "админу карточка с причиной и кнопками «Зачислить» / «Отклонить»")
    await M.credit_ready(watchers)
    t, _ = await state(tid)
    check(t.status == "held", "задержанный платёж сам по себе не зачисляется при следующих проходах")
    check(await M.release_transfer(tid, True) == "Зачисляю", "админ проверил в обозревателе и нажал «Зачислить»")
    await M.credit_ready(watchers)
    t, bal = await state(tid)
    check(t.status == "credited" and bal >= 0 and t.note == "проверено админом", "→ зачислено, без повторной перепроверки")
    check(await M.release_transfer(tid, True) == "Уже решено", "второе нажатие ничего не делает")

    print("8. админ отклонил")
    verdicts[:] = [(Verdict.MISMATCH, "сумма другая")]
    uid, oid, tid = await paid_order(12)
    await M.credit_ready(watchers)
    await M.release_transfer(tid, False)
    await M.credit_ready(watchers)
    t, bal = await state(tid)
    async with session_scope() as s:
        told = (await s.execute(select(OutboxMessage).where(OutboxMessage.user_id == uid))).scalars().all()
    check(t.status == "rejected" and (bal or 0) == 0, "отклонено → не зачислено и больше не зачислится")
    check(any("не подтвердился" in m.text for m in told), "клиенту ушло «Платёж не подтвердился… напишите в поддержку»")

    print("9. второй источник молчит → повторяем с паузой, потом зовём админа")
    verdicts[:] = [(Verdict.UNKNOWN, "второй источник недоступен: HTTP 503")] * 3
    uid, oid, tid = await paid_order(13)
    await M.credit_ready(watchers)
    t, bal = await state(tid)
    check(t.status == "matched" and t.xcheck_tries == 1 and t.xcheck_after > utcnow() and (bal or 0) == 0,
          f"не зачислено, следующая попытка через {M.XCHECK_BACKOFF[0]} с")
    left = len(verdicts)
    await M.credit_ready(watchers)
    check(len(verdicts) == left, "раньше назначенного времени второй источник не дёргаем")
    async with session_scope() as s:
        t = await s.get(Transfer, tid)
        t.xcheck_after = utcnow() - timedelta(seconds=1)
        t.xcheck_since = utcnow() - timedelta(minutes=16)
    before = len(await admin_msgs())
    await M.credit_ready(watchers)
    t, _ = await state(tid)
    msgs = await admin_msgs()
    check(len(msgs) == before + 1 and "ждёт второй источник" in msgs[-1].text and t.xcheck_alerted,
          "молчит 16 минут → одна карточка админу (с теми же кнопками)")
    async with session_scope() as s:
        (await s.get(Transfer, tid)).xcheck_after = utcnow() - timedelta(seconds=1)
    await M.credit_ready(watchers)
    check(len(await admin_msgs()) == before + 1, "повторных карточек не шлём")
    async with session_scope() as s:
        (await s.get(Transfer, tid)).xcheck_after = utcnow() - timedelta(seconds=1)
    await M.credit_ready(watchers)  # the second provider is back: OK
    t, bal = await state(tid)
    check(t.status == "credited", "второй источник ожил и подтвердил → зачислено само")

    print("10. откуда второе мнение")

    class Utxo:
        providers = [("esplora", "https://mempool.space/api"), ("esplora", "https://blockstream.info/api")]
        active = 1
    check(M._primary_base(Utxo()) == "https://blockstream.info/api",
          "BTC прочитан через blockstream (запасной) → второе мнение спросим у другого")
    await outbox.flush()


async def main():
    await unit()
    await flow()
    print(f"\nALL GOOD: {OK} checks passed")


asyncio.run(main())
