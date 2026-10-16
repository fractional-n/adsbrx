import random

import numpy as np
import pytest

from adsbrx import crc
from adsbrx.sim import encoder as enc

KNOWN_FRAMES = [
    "8D4840D6202CC371C32CE0576098",
    "8D40621D58C382D690C8AC2863A7",
    "8D40621D58C386435CC412692AD6",
    "8D485020994409940838175B284F",
]


def random_frame(rng, nbytes=14):
    body = bytes(rng.getrandbits(8) for _ in range(nbytes - 3))
    return body + crc.crc24(body).to_bytes(3, "big")


@pytest.mark.parametrize("hexstr", KNOWN_FRAMES)
def test_known_frames_have_zero_syndrome(hexstr):
    assert crc.syndrome(bytes.fromhex(hexstr)) == 0


def test_short_frame_roundtrip():
    frame = enc.build_df11(0x4840D6)
    assert len(frame) == 7
    assert crc.syndrome(frame) == 0


def test_address_parity_returns_icao():
    frame = enc.build_surveillance(4, 0xABCDEF, enc.encode_ac13(30000))
    assert crc.syndrome(frame) == 0xABCDEF


def test_vectorised_syndromes_match_scalar():
    rng = random.Random(1)
    for nbytes in (7, 14):
        frames = [bytes(rng.getrandbits(8) for _ in range(nbytes)) for _ in range(200)]
        arr = np.frombuffer(b"".join(frames), dtype=np.uint8).reshape(-1, nbytes)
        vec = crc.syndromes(arr)
        assert [int(v) for v in vec] == [crc.syndrome(f) for f in frames]


@pytest.mark.parametrize("nbytes", [7, 14])
def test_every_single_bit_error_is_corrected(nbytes):
    rng = random.Random(2)
    good = random_frame(rng, nbytes)
    for bit in range(nbytes * 8):
        bad = bytearray(good)
        bad[bit >> 3] ^= 0x80 >> (bit & 7)
        fixed = crc.fix_frame(bytes(bad), max_bits=1)
        assert fixed == (good, 1)


def test_two_bit_errors_are_corrected_exactly():
    rng = random.Random(3)
    good = random_frame(rng, 14)
    total = 0
    for i in range(0, 112, 3):
        for j in range(i + 1, 112, 5):
            bad = bytearray(good)
            for bit in (i, j):
                bad[bit >> 3] ^= 0x80 >> (bit & 7)
            total += 1
            assert crc.fix_frame(bytes(bad), max_bits=2) == (good, 2)
            assert crc.fix_frame(bytes(bad), max_bits=1) is None
    assert total > 400


def test_unfixable_frame_returns_none():
    rng = random.Random(4)
    good = bytearray(random_frame(rng, 14))
    for bit in (3, 40, 77, 100, 111):
        good[bit >> 3] ^= 0x80 >> (bit & 7)
    assert crc.fix_frame(bytes(good), max_bits=1) is None


def test_error_table_lookup_vectorised():
    table = crc.error_table(112, 1)
    rng = random.Random(5)
    good = random_frame(rng, 14)
    syns = []
    for bit in (0, 17, 55, 111):
        bad = bytearray(good)
        bad[bit >> 3] ^= 0x80 >> (bit & 7)
        syns.append(crc.syndrome(bytes(bad)))
    syns.append(0)
    syns.append(0x123456)
    hit, idx = table.lookup(np.array(syns, dtype=np.uint32))
    assert hit.tolist() == [True, True, True, True, False, False]
    assert [table.positions[i] for i in idx[:4]] == [(0,), (17,), (55,), (111,)]
