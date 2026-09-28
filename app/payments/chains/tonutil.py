"""Tiny TON helpers: address formats and single-cell BOC (enough for get_wallet_address)."""
from __future__ import annotations

import base64
import binascii


def _crc16(data: bytes) -> bytes:
    return binascii.crc_hqx(data, 0).to_bytes(2, "big")


def parse_address(addr: str) -> tuple[int, bytes]:
    addr = addr.strip()
    if ":" in addr:
        wc, h = addr.split(":", 1)
        hb = bytes.fromhex(h)
        if len(hb) != 32:
            raise ValueError("bad raw TON address")
        return int(wc), hb
    raw = base64.urlsafe_b64decode(addr.replace("+", "-").replace("/", "_") + "==")
    if len(raw) != 36 or _crc16(raw[:34]) != raw[34:]:
        raise ValueError("bad user-friendly TON address")
    wc = raw[1] if raw[1] < 128 else raw[1] - 256
    return wc, raw[2:34]


def to_raw(addr: str) -> str:
    wc, h = parse_address(addr)
    return f"{wc}:{h.hex().upper()}"


def to_friendly(addr: str, bounceable: bool = False) -> str:
    wc, h = parse_address(addr)
    tag = 0x11 if bounceable else 0x51
    body = bytes([tag, wc & 0xFF]) + h
    return base64.urlsafe_b64encode(body + _crc16(body)).decode()


def address_to_boc(addr: str) -> str:
    """Serialize MsgAddressInt as a one-cell BOC (base64) — a tvm.Slice argument for get methods."""
    wc, h = parse_address(addr)
    bits = "100" + format(wc & 0xFF, "08b") + "".join(format(b, "08b") for b in h)  # 267 bits
    nbits = len(bits)
    padded = bits + "1"
    padded += "0" * (-len(padded) % 8)
    data = int(padded, 2).to_bytes(len(padded) // 8, "big")
    d1, d2 = 0, nbits // 8 + (nbits + 7) // 8
    cell = bytes([d1, d2]) + data
    boc = bytes.fromhex("b5ee9c72") + bytes([0x01, 0x01, 1, 1, 0, len(cell), 0]) + cell
    return base64.b64encode(boc).decode()


def address_from_boc(b64: str) -> str:
    """Parse the root cell of a BOC that holds a MsgAddressInt; returns raw 'wc:HEX'."""
    b = base64.b64decode(b64)
    if b[:4] != bytes.fromhex("b5ee9c72"):
        raise ValueError("not a BOC")
    flags = b[4]
    has_idx, size = bool(flags & 0x80), flags & 0x07
    off_bytes = b[5]
    p = 6
    cells = int.from_bytes(b[p:p + size], "big"); p += size
    roots = int.from_bytes(b[p:p + size], "big"); p += size
    p += size  # absent
    p += off_bytes  # tot_cells_size
    root_idx = int.from_bytes(b[p:p + size], "big"); p += size * roots
    if has_idx:
        p += cells * off_bytes
    # walk to the root cell
    for i in range(cells):
        d1, d2 = b[p], b[p + 1]
        dlen = (d2 + 1) // 2
        refs = d1 & 7
        if i == root_idx:
            data = b[p + 2:p + 2 + dlen]
            bits = "".join(format(x, "08b") for x in data)
            if d2 % 2:  # strip completion tag
                bits = bits[: bits.rstrip("0").__len__() - 1]
            if not bits.startswith("100"):
                raise ValueError("not addr_std")
            wc = int(bits[3:11], 2)
            wc = wc - 256 if wc > 127 else wc
            h = int(bits[11:267], 2).to_bytes(32, "big")
            return f"{wc}:{h.hex().upper()}"
        p += 2 + dlen + refs * size
    raise ValueError("root cell not found")
