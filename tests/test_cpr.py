import math
import random

import pytest

from adsbrx import cpr
from adsbrx.geo import distance_nm


def test_nl_reference_values():
    assert cpr.nl(0.0) == 59
    assert cpr.nl(10.0) == 59
    assert cpr.nl(10.5) == 58
    assert cpr.nl(52.25) == 36
    assert cpr.nl(-52.25) == 36
    assert cpr.nl(86.9) == 2
    assert cpr.nl(87.0) == 2
    assert cpr.nl(87.5) == 1
    assert cpr.nl(90.0) == 1


def test_nl_is_monotonic_non_increasing():
    prev = 60
    for i in range(0, 9001):
        value = cpr.nl(i / 100)
        assert 1 <= value <= prev
        prev = value


def test_global_decode_reference_pair():
    # Widely quoted example pair (The 1090 MHz Riddle / pyModeS docs): even frame is the newest
    lat, lon = cpr.decode_global(93000, 51372, 74158, 50194, odd_latest=False)
    assert lat == pytest.approx(52.25720214843750, abs=1e-9)
    assert lon == pytest.approx(3.91937255859375, abs=1e-9)


def test_global_decode_odd_latest_uses_odd_frame():
    lat, lon = cpr.decode_global(93000, 51372, 74158, 50194, odd_latest=True)
    assert lat == pytest.approx(52.26578, abs=1e-5)
    assert lon == pytest.approx(3.93891, abs=1e-5)
    even_based = cpr.decode_global(93000, 51372, 74158, 50194, odd_latest=False)
    assert distance_nm(lat, lon, *even_based) < 1.0


def random_position(rng):
    return rng.uniform(-85, 85), rng.uniform(-180, 180)


def test_global_roundtrip_random_positions():
    rng = random.Random(10)
    decoded = 0
    for _ in range(2000):
        lat, lon = random_position(rng)
        even = cpr.encode(lat, lon, odd=False)
        odd = cpr.encode(lat, lon, odd=True)
        for odd_latest in (False, True):
            result = cpr.decode_global(*even, *odd, odd_latest=odd_latest)
            if result is None:  # zone boundary crossing, correctly rejected
                continue
            decoded += 1
            assert distance_nm(lat, lon, *result) < 0.02
    assert decoded > 3900


def test_global_decode_wraps_longitude():
    for lon in (179.99, -179.99, 180.0, -180.0):
        even = cpr.encode(10.0, lon, odd=False)
        odd = cpr.encode(10.0, lon, odd=True)
        result = cpr.decode_global(*even, *odd, odd_latest=True)
        assert result is not None
        assert -180.0 <= result[1] < 180.0
        assert distance_nm(10.0, lon, *result) < 0.02


def test_local_decode_roundtrip_airborne():
    rng = random.Random(11)
    for _ in range(3000):
        lat, lon = random_position(rng)
        ref_lat = max(-89.0, min(89.0, lat + rng.uniform(-2.5, 2.5)))
        ref_lon = lon + rng.uniform(-2.5, 2.5) / max(0.05, math.cos(math.radians(lat)))
        for odd in (False, True):
            lat_cpr, lon_cpr = cpr.encode(lat, lon, odd)
            got = cpr.decode_local(ref_lat, ref_lon, lat_cpr, lon_cpr, odd)
            assert distance_nm(lat, lon, *got) < 0.02


def test_local_decode_roundtrip_surface():
    rng = random.Random(12)
    for _ in range(3000):
        lat, lon = random_position(rng)
        ref_lat = max(-89.0, min(89.0, lat + rng.uniform(-0.3, 0.3)))
        ref_lon = lon + rng.uniform(-0.3, 0.3) / max(0.05, math.cos(math.radians(lat)))
        for odd in (False, True):
            lat_cpr, lon_cpr = cpr.encode(lat, lon, odd, surface=True)
            got = cpr.decode_local(ref_lat, ref_lon, lat_cpr, lon_cpr, odd, surface=True)
            assert distance_nm(lat, lon, *got) < 0.02


def test_known_japan_position():
    lat, lon = 35.5494, 139.7798  # Haneda
    even = cpr.encode(lat, lon, False)
    odd = cpr.encode(lat, lon, True)
    got = cpr.decode_global(*even, *odd, odd_latest=True)
    assert got == pytest.approx((lat, lon), abs=1e-3)
    got = cpr.decode_local(35.0, 139.0, *odd, True)
    assert got == pytest.approx((lat, lon), abs=1e-3)
