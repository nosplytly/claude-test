from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import httpx

from ..methods import Method

log = logging.getLogger("sh.chains")


@dataclass
class Incoming:
    """One incoming transfer of `method`'s asset to our address."""

    method: str
    txid: str
    idx: str  # distinguishes several transfers inside one tx (log index, etc.)
    to_address: str
    units: int
    from_address: str | None = None
    comment: str | None = None
    block_number: int | None = None
    block_time: datetime | None = None
    confirmations: int = 0
    final: bool = False

    @property
    def uid(self) -> str:
        return f"{self.method}:{self.txid}:{self.idx}"


class Watcher:
    """Polls one chain for incoming transfers to our addresses of the given methods."""

    chain: str = ""

    def __init__(self, methods: list[Method]):
        self.methods = methods
        self.http = httpx.AsyncClient(timeout=20, headers={"User-Agent": "Mozilla/5.0 SupplierHub"})

    async def poll(self) -> list[Incoming]:
        raise NotImplementedError

    async def head(self) -> int | None:
        """Current block height for chains where confirmations are counted by blocks."""
        return None

    async def verify(self, inc_txid: str, block_number: int | None) -> bool:
        """Re-check a tx right before crediting (reorg guard). Default: trust the watcher."""
        return True
