"""Compact Position Reporting (CPR) for 1090ES airborne and surface positions."""

from __future__ import annotations

import math

NZ = 15
_SCALE = 131072.0  # 2**17


def nl(lat: float) -> int:
    """Number of longitude zones at a latitude."""
    lat = abs(lat)
    if lat < 1e-9:
        return 59
    if lat == 87.0:
        return 2
    if lat > 87.0:
        return 1
    a = 1.0 - math.cos(math.pi / (2 * NZ))
    b = math.cos(math.radians(lat)) ** 2
    return int(math.floor(2 * math.pi / math.acos(1.0 - a / b)))


def _wrap_lon(lon: float) -> float:
    lon = (lon + 180.0) % 360.0 - 180.0
    return lon


def decode_global(
    lat_even: int, lon_even: int, lat_odd: int, lon_odd: int, odd_latest: bool
) -> tuple[float, float] | None:
    """Airborne global decode from an even/odd pair. None when the pair straddles zones."""
    lat_e = lat_even / _SCALE
    lon_e = lon_even / _SCALE
    lat_o = lat_odd / _SCALE
    lon_o = lon_odd / _SCALE

    j = math.floor(59 * lat_e - 60 * lat_o + 0.5)
    rlat_e = (360.0 / 60) * ((j % 60) + lat_e)
    rlat_o = (360.0 / 59) * ((j % 59) + lat_o)
    if rlat_e >= 270:
        rlat_e -= 360
    if rlat_o >= 270:
        rlat_o -= 360
    if not (-90.0 <= rlat_e <= 90.0 and -90.0 <= rlat_o <= 90.0):
        return None
    if nl(rlat_e) != nl(rlat_o):
        return None

    if odd_latest:
        lat = rlat_o
        zones = nl(lat)
        ni = max(zones - 1, 1)
        m = math.floor(lon_e * (zones - 1) - lon_o * zones + 0.5)
        lon = (360.0 / ni) * ((m % ni) + lon_o)
    else:
        lat = rlat_e
        zones = nl(lat)
        ni = max(zones, 1)
        m = math.floor(lon_e * (zones - 1) - lon_o * zones + 0.5)
        lon = (360.0 / ni) * ((m % ni) + lon_e)
    return lat, _wrap_lon(lon)


def decode_local(
    ref_lat: float,
    ref_lon: float,
    lat_cpr: int,
    lon_cpr: int,
    odd: bool,
    surface: bool = False,
) -> tuple[float, float]:
    """Decode a single CPR frame relative to a reference position.

    Valid when the true position is within 180 NM (airborne) / 45 NM (surface)
    of the reference.
    """
    lat_c = lat_cpr / _SCALE
    lon_c = lon_cpr / _SCALE
    span = 90.0 if surface else 360.0

    d_lat = span / (59 if odd else 60)
    j = math.floor(ref_lat / d_lat) + math.floor(0.5 + (ref_lat % d_lat) / d_lat - lat_c)
    lat = d_lat * (j + lat_c)

    ni = max(nl(lat) - (1 if odd else 0), 1)
    d_lon = span / ni
    m = math.floor(ref_lon / d_lon) + math.floor(0.5 + (ref_lon % d_lon) / d_lon - lon_c)
    lon = d_lon * (m + lon_c)
    return lat, _wrap_lon(lon)


def encode(lat: float, lon: float, odd: bool, surface: bool = False) -> tuple[int, int]:
    """Encode a position into 17-bit CPR latitude/longitude (used by the simulator)."""
    span = 90.0 if surface else 360.0
    d_lat = span / (59 if odd else 60)
    yz = math.floor(_SCALE * ((lat % d_lat) / d_lat) + 0.5)
    rlat = d_lat * (yz / _SCALE + math.floor(lat / d_lat))

    ni = max(nl(rlat) - (1 if odd else 0), 1)
    d_lon = span / ni
    xz = math.floor(_SCALE * ((lon % d_lon) / d_lon) + 0.5)
    return int(yz) & 0x1FFFF, int(xz) & 0x1FFFF
