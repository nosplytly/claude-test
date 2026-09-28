"""Payment methods = (coin, network) pairs we accept, each paid to one of our own public addresses."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..config import settings


@dataclass(frozen=True)
class Method:
    code: str
    coin: str
    network: str  # short label shown on the badge
    network_title: str  # human readable
    chain: str  # watcher key
    decimals: int  # on-chain decimals
    step: Decimal  # unique-amount granularity
    confirmations: int  # blocks required (block chains); 1 for chains where the watcher reports finality
    stable: bool
    token: str | None = None  # contract / mint / jetton master
    uses_comment: bool = False
    explorer_tx: str = ""
    sort: int = 0
    min_usd: Decimal = Decimal(0)  # smallest invoice the network can carry (BTC: dust limit); extra goes to balance

    @property
    def address(self) -> str:
        return settings.wallet(self.code)

    @property
    def enabled(self) -> bool:
        return bool(self.address)

    @property
    def title(self) -> str:
        return f"{self.coin} · {self.network}" if self.coin != self.network else self.coin

    def tx_url(self, txid: str) -> str:
        return self.explorer_tx.format(txid=txid) if self.explorer_tx and txid else ""


_D = Decimal

METHODS: dict[str, Method] = {m.code: m for m in [
    Method("usdt_trc20", "USDT", "TRC20", "TRON", "tron", 6, _D("0.0001"), 1, True,
           token="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", explorer_tx="https://tronscan.org/#/transaction/{txid}", sort=1),
    Method("usdt_ton", "USDT", "TON", "TON", "ton", 6, _D("0.0001"), 1, True,
           token="EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs", uses_comment=True,
           explorer_tx="https://tonviewer.com/transaction/{txid}", sort=2),
    Method("usdt_bep20", "USDT", "BEP20", "BNB Smart Chain", "bsc", 18, _D("0.0001"), 15, True,
           token="0x55d398326f99059fF775485246999027B3197955", explorer_tx="https://bscscan.com/tx/{txid}", sort=3),
    Method("usdt_sol", "USDT", "SOL", "Solana", "sol", 6, _D("0.0001"), 1, True,
           token="Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", explorer_tx="https://solscan.io/tx/{txid}", sort=4),
    Method("usdt_erc20", "USDT", "ERC20", "Ethereum", "eth", 6, _D("0.0001"), 12, True,
           token="0xdAC17F958D2ee523a2206206994597C13D831ec7", explorer_tx="https://etherscan.io/tx/{txid}", sort=5),
    Method("btc", "BTC", "BTC", "Bitcoin", "btc", 8, _D("0.00000001"), 1, False,
           explorer_tx="https://mempool.space/tx/{txid}", sort=10, min_usd=_D("1")),  # < 546 sat is unsendable dust
    Method("eth", "ETH", "ETH", "Ethereum", "eth", 18, _D("0.000001"), 12, False,
           explorer_tx="https://etherscan.io/tx/{txid}", sort=11),
    Method("ton", "TON", "TON", "TON", "ton", 9, _D("0.0001"), 1, False, uses_comment=True,
           explorer_tx="https://tonviewer.com/transaction/{txid}", sort=12),
    Method("ltc", "LTC", "LTC", "Litecoin", "ltc", 8, _D("0.000001"), 3, False,
           explorer_tx="https://litecoinspace.org/tx/{txid}", sort=13),
    Method("trx", "TRX", "TRX", "TRON", "tron", 6, _D("0.001"), 1, False,
           explorer_tx="https://tronscan.org/#/transaction/{txid}", sort=14),
    Method("sol", "SOL", "SOL", "Solana", "sol", 9, _D("0.000001"), 1, False,
           explorer_tx="https://solscan.io/tx/{txid}", sort=15),
]}


def get_method(code: str) -> Method | None:
    return METHODS.get(code)


def enabled_methods() -> list[Method]:
    return sorted((m for m in METHODS.values() if m.enabled), key=lambda m: m.sort)
