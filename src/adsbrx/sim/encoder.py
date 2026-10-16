"""Mode S frame builders used by the signal simulator and the tests."""

from __future__ import annotations

import math

from ..crc import crc24
from ..modes import decode_surface_speed

_CHARSET = "?ABCDEFGHIJKLMNOPQRSTUVWXYZ????? ???????????????0123456789??????"
_CHAR_INDEX = {c: i for i, c in enumerate(_CHARSET) if c != "?"}


def with_parity(body: bytes, address: int | None = None) -> bytes:
    """Append CRC-24 parity. `address` is overlaid for DF0/4/5/16/20/21."""
    parity = crc24(body)
    if address is not None:
        parity ^= address
    return body + parity.to_bytes(3, "big")


def build_df17(icao: int, me: int, ca: int = 5, df: int = 17) -> bytes:
    body = bytes([(df << 3) | ca]) + icao.to_bytes(3, "big") + me.to_bytes(7, "big")
    return with_parity(body)


def build_df11(icao: int, ca: int = 5) -> bytes:
    body = bytes([(11 << 3) | ca]) + icao.to_bytes(3, "big")
    return with_parity(body)


def build_surveillance(df: int, icao: int, field13: int, fs: int = 0) -> bytes:
    """DF4/DF5 (7 bytes) or DF20/DF21 (14 bytes, zero MB) with address/parity."""
    body = bytes([(df << 3) | (fs & 7), 0, (field13 >> 8) & 0x1F, field13 & 0xFF])
    if df >= 16:
        body += bytes(7)
    return with_parity(body, address=icao)


def squawk_to_id13(squawk: str) -> int:
    a, b, c, d = (int(ch) for ch in squawk)
    bits = [
        c & 1, a & 1, (c >> 1) & 1, (a >> 1) & 1, (c >> 2) & 1, (a >> 2) & 1, 0,
        b & 1, d & 1, (b >> 1) & 1, (d >> 1) & 1, (b >> 2) & 1, (d >> 2) & 1,
    ]
    value = 0
    for bit in bits:
        value = (value << 1) | bit
    return value


def _altitude_count(altitude_ft: int) -> int:
    return max(0, min(0x7FF, (int(altitude_ft) + 1000) // 25))


def encode_ac12(altitude_ft: int) -> int:
    """12-bit altitude field of airborne position messages (Q=1, 25 ft steps)."""
    n = _altitude_count(altitude_ft)
    return ((n >> 4) << 5) | 0x10 | (n & 0xF)


def encode_ac13(altitude_ft: int) -> int:
    """13-bit AC field of DF0/4/16/20 (Q=1, 25 ft steps, M=0)."""
    n = _altitude_count(altitude_ft)
    return ((n >> 5) << 7) | (((n >> 4) & 1) << 5) | 0x10 | (n & 0xF)


def encode_ident(callsign: str, category: int = 0, tc: int = 4) -> int:
    me = (tc << 51) | ((category & 7) << 48)
    for i, ch in enumerate(callsign.upper().ljust(8)[:8]):
        me |= _CHAR_INDEX.get(ch, 0) << (42 - 6 * i)
    return me


def encode_airborne_position(
    lat_cpr: int, lon_cpr: int, odd: bool, altitude_ft: int | None, tc: int = 11
) -> int:
    alt = 0 if altitude_ft is None else encode_ac12(altitude_ft)
    return (tc << 51) | (alt << 36) | (int(odd) << 34) | ((lat_cpr & 0x1FFFF) << 17) | (lon_cpr & 0x1FFFF)


def encode_surface_position(
    lat_cpr: int, lon_cpr: int, odd: bool, movement: int = 0, track_deg: float | None = None, tc: int = 7
) -> int:
    me = (tc << 51) | ((movement & 0x7F) << 44)
    if track_deg is not None:
        me |= (1 << 43) | ((int(round(track_deg * 128 / 360)) & 0x7F) << 36)
    return me | (int(odd) << 34) | ((lat_cpr & 0x1FFFF) << 17) | (lon_cpr & 0x1FFFF)


_MOVEMENT_TABLE: list[tuple[int, float]] = []


def encode_movement(speed_kt: float) -> int:
    """Surface movement code (1..124) closest to a ground speed."""
    if not _MOVEMENT_TABLE:
        _MOVEMENT_TABLE.extend((m, decode_surface_speed(m)) for m in range(1, 125))
    return min(_MOVEMENT_TABLE, key=lambda item: abs(item[1] - speed_kt))[0]


def encode_velocity(
    gs_kt: float,
    track_deg: float,
    vrate_fpm: int = 0,
    baro_rate: bool = False,
    geo_minus_baro_ft: int | None = None,
) -> int:
    """Airborne velocity, subtype 1 (ground speed, subsonic)."""
    vew = round(gs_kt * math.sin(math.radians(track_deg)))
    vns = round(gs_kt * math.cos(math.radians(track_deg)))
    me = (19 << 51) | (1 << 48)
    me |= (int(vew < 0) << 42) | (min(abs(vew) + 1, 1023) << 32)
    me |= (int(vns < 0) << 31) | (min(abs(vns) + 1, 1023) << 21)
    me |= int(baro_rate) << 20
    me |= int(vrate_fpm < 0) << 19
    me |= min(abs(int(vrate_fpm)) // 64 + 1, 511) << 10
    if geo_minus_baro_ft is not None:
        me |= (int(geo_minus_baro_ft < 0) << 7) | min(abs(int(geo_minus_baro_ft)) // 25 + 1, 127)
    return me


def encode_status(squawk: str, emergency: int = 0) -> int:
    return (28 << 51) | (1 << 48) | ((emergency & 7) << 45) | (squawk_to_id13(squawk) << 32)
