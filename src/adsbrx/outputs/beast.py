"""Binary Beast and AVR raw framing, as served on ports 30005 / 30002 by dump1090 / readsb."""

from __future__ import annotations

import math

from ..modes import ModeSMessage

ESCAPE = 0x1A
MLAT_TICKS_PER_SAMPLE = 6  # Beast timestamps count a 12 MHz clock; the input runs at 2 MS/s
_TIMESTAMP_MASK = (1 << 48) - 1


def signal_byte(rssi_dbfs: float) -> int:
    """Beast signal level: amplitude relative to full scale, 0-255."""
    return max(0, min(255, round(math.sqrt(10.0 ** (rssi_dbfs / 10.0)) * 255.0)))


def encode_beast(msg: ModeSMessage) -> bytes:
    """`<0x1A> <'2'|'3'> <6 byte timestamp> <signal> <frame>`, 0x1A bytes doubled."""
    kind = b"2" if len(msg.raw) == 7 else b"3"
    stamp = ((msg.sample * MLAT_TICKS_PER_SAMPLE) & _TIMESTAMP_MASK).to_bytes(6, "big")
    body = stamp + bytes((signal_byte(msg.rssi),)) + msg.raw
    return b"\x1a" + kind + body.replace(b"\x1a", b"\x1a\x1a")


def encode_raw(msg: ModeSMessage) -> bytes:
    """AVR raw format: `*<hex>;`."""
    return b"*" + msg.raw.hex().upper().encode("ascii") + b";\n"
