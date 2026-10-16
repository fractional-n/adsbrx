"""Mode S CRC-24 helpers (scalar and vectorised) and bit-error correction tables."""

from __future__ import annotations

import numpy as np

# Generator polynomial x^24 + x^23 + ... + x^3 + 1 without the leading term.
_POLY = 0xFFF409


def _make_table() -> list[int]:
    table = []
    for byte in range(256):
        crc = byte << 16
        for _ in range(8):
            crc = ((crc << 1) ^ _POLY) if crc & 0x800000 else (crc << 1)
            crc &= 0xFFFFFF
        table.append(crc)
    return table


_TABLE_LIST = _make_table()
TABLE = np.array(_TABLE_LIST, dtype=np.uint32)


def crc24(data: bytes) -> int:
    """CRC-24 parity of `data` (the value the 24 parity bits would carry)."""
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFFFF) ^ _TABLE_LIST[((crc >> 16) ^ b) & 0xFF]
    return crc


def syndrome(frame: bytes) -> int:
    """CRC remainder of a whole frame. 0 means a valid DF11/17/18 frame.

    For address/parity frames (DF0/4/5/16/20/21) the remainder is the ICAO address.
    """
    return crc24(frame[:-3]) ^ int.from_bytes(frame[-3:], "big")


def syndromes(frames: np.ndarray) -> np.ndarray:
    """Vectorised `syndrome` for an (N, nbytes) uint8 array."""
    n = frames.shape[1]
    crc = np.zeros(frames.shape[0], dtype=np.uint32)
    for k in range(n - 3):
        crc = ((crc << 8) & 0xFFFFFF) ^ TABLE[(crc >> 16) ^ frames[:, k]]
    parity = (
        (frames[:, n - 3].astype(np.uint32) << 16)
        | (frames[:, n - 2].astype(np.uint32) << 8)
        | frames[:, n - 1].astype(np.uint32)
    )
    return crc ^ parity


def _unit_syndromes(n_bits: int) -> list[int]:
    out = []
    for bit in range(n_bits):
        buf = bytearray(n_bits // 8)
        buf[bit >> 3] = 0x80 >> (bit & 7)
        out.append(syndrome(bytes(buf)))
    return out


class ErrorTable:
    """Syndrome -> bit positions lookup for 1 (and optionally 2) bit errors."""

    def __init__(self, n_bits: int, max_bits: int = 1):
        units = _unit_syndromes(n_bits)
        table: dict[int, tuple[int, ...]] = {s: (i,) for i, s in enumerate(units)}
        if max_bits >= 2:
            pairs: dict[int, tuple[int, ...]] = {}
            ambiguous: set[int] = set()
            for i in range(n_bits):
                for j in range(i + 1, n_bits):
                    s = units[i] ^ units[j]
                    if s in table:
                        continue
                    if s in pairs or s in ambiguous:
                        pairs.pop(s, None)
                        ambiguous.add(s)
                        continue
                    pairs[s] = (i, j)
            table.update(pairs)
        self.n_bits = n_bits
        self.table = table
        keys = np.array(sorted(table), dtype=np.uint32)
        self.keys = keys
        self.positions = [table[int(k)] for k in keys]

    def lookup(self, syn: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Return (hit mask, index into `positions`) for an array of syndromes."""
        idx = np.searchsorted(self.keys, syn)
        idx = np.minimum(idx, len(self.keys) - 1)
        hit = (self.keys[idx] == syn) & (syn != 0)
        return hit, idx


_tables: dict[tuple[int, int], ErrorTable] = {}


def error_table(n_bits: int, max_bits: int = 1) -> ErrorTable:
    key = (n_bits, max_bits)
    if key not in _tables:
        _tables[key] = ErrorTable(n_bits, max_bits)
    return _tables[key]


def fix_frame(frame: bytes, max_bits: int = 1) -> tuple[bytes, int] | None:
    """Try to repair a DF11/17/18 frame. Returns (fixed frame, bit errors) or None."""
    syn = syndrome(frame)
    if syn == 0:
        return frame, 0
    if max_bits <= 0:
        return None
    pos = error_table(len(frame) * 8, max_bits).table.get(syn)
    if pos is None:
        return None
    fixed = bytearray(frame)
    for bit in pos:
        fixed[bit >> 3] ^= 0x80 >> (bit & 7)
    return bytes(fixed), len(pos)
