import random

import pytest

from adsbrx import modes
from adsbrx.sim import encoder as enc

pms = pytest.importorskip("pyModeS")


def test_ident_known_vector():
    msg = modes.decode(bytes.fromhex("8D4840D6202CC371C32CE0576098"))
    assert msg.kind == modes.IDENT
    assert msg.icao_hex == "4840D6"
    assert msg.callsign == "KLM1023"
    assert msg.category == "A0"
    assert msg.tc == 4


def test_airborne_position_known_vectors():
    even = modes.decode(bytes.fromhex("8D40621D58C382D690C8AC2863A7"))
    odd = modes.decode(bytes.fromhex("8D40621D58C386435CC412692AD6"))
    assert even.kind == modes.AIRBORNE_POS
    assert even.altitude == 38000
    assert (even.cpr.odd, even.cpr.lat, even.cpr.lon) == (False, 93000, 51372)
    assert (odd.cpr.odd, odd.cpr.lat, odd.cpr.lon) == (True, 74158, 50194)
    assert even.on_ground is False


def test_velocity_known_vector():
    msg = modes.decode(bytes.fromhex("8D485020994409940838175B284F"))
    assert msg.kind == modes.VELOCITY
    assert round(msg.gs) == 159
    assert msg.track == pytest.approx(182.88, abs=0.01)
    assert msg.vrate == -832
    assert msg.vrate_source == "geom"
    assert msg.geo_minus_baro == 550


def test_squawk_roundtrip_all_codes():
    for a in range(8):
        for b in range(8):
            for c in range(8):
                for d in range(8):
                    code = f"{a}{b}{c}{d}"
                    assert modes.decode_squawk(enc.squawk_to_id13(code)) == code


def test_altitude_roundtrip_q_bit():
    for alt in range(-1000, 50176, 25):
        assert modes.decode_ac12(enc.encode_ac12(alt)) == alt
        assert modes.decode_ac13(enc.encode_ac13(alt)) == alt
    assert modes.decode_ac12(0) is None
    assert modes.decode_ac13(0) is None


def gillham_bits(ac13):
    """The 11 information bits of a Gillham coded AC field (M, Q and D1 excluded)."""
    return ac13 & ~(0x0040 | 0x0010)


def test_gillham_code_is_a_unit_distance_code_over_the_whole_range():
    decoded = {}
    for code in range(1 << 13):
        if code & 0x0040 or code & 0x0010:
            continue
        alt = modes.decode_ac13(code)
        if alt is not None:
            assert alt not in decoded, "two code words for one altitude"
            decoded[alt] = code
    altitudes = sorted(decoded)
    assert altitudes[0] == -1200
    assert altitudes == list(range(-1200, altitudes[-1] + 1, 100))
    assert altitudes[-1] >= 62000
    for lower, upper in zip(altitudes, altitudes[1:]):
        diff = decoded[lower] ^ decoded[upper]
        assert bin(diff).count("1") == 1, (lower, upper)
    assert decoded[-1200] == 0x0100  # only C4 set


def test_decode_rejects_malformed():
    assert modes.decode(b"\x00" * 5) is None
    assert modes.decode(bytes.fromhex("8D4840D6202CC371C32CE057")) is None
    assert modes.decode(bytes([0x1F]) + bytes(6)) is None


def test_to_dict_keeps_zero_values():
    frame = enc.build_df17(0x123456, enc.encode_velocity(0, 0, 0))
    info = modes.decode(frame).to_dict()
    assert info["icao"] == "123456"
    assert info["kind"] == modes.VELOCITY
    assert info["gs"] == 0.0
    assert info["track"] == 0.0


def test_df11_and_surveillance_messages():
    msg = modes.decode(enc.build_df11(0xABCDEF, ca=4))
    assert (msg.df, msg.icao_hex, msg.kind, msg.on_ground) == (11, "ABCDEF", modes.ALL_CALL, True)

    msg = modes.decode(enc.build_surveillance(4, 0x4840D6, enc.encode_ac13(35000), fs=1))
    assert (msg.icao_hex, msg.altitude, msg.on_ground) == ("4840D6", 35000, True)

    msg = modes.decode(enc.build_surveillance(21, 0x4840D6, enc.squawk_to_id13("7700"), fs=2))
    assert (msg.icao_hex, msg.squawk, msg.alert, msg.on_ground) == ("4840D6", "7700", True, False)


def test_surface_position_fields():
    me = enc.encode_surface_position(1234, 5678, True, movement=40, track_deg=90.0)
    msg = modes.decode(enc.build_df17(0xABC123, me))
    assert msg.kind == modes.SURFACE_POS
    assert msg.on_ground is True
    assert msg.gs == pytest.approx(16.0)
    assert msg.track == pytest.approx(90.0)
    assert (msg.cpr.odd, msg.cpr.lat, msg.cpr.lon, msg.cpr.surface) == (True, 1234, 5678, True)


@pytest.mark.parametrize(
    "movement,speed",
    [(0, None), (1, 0.0), (2, 0.125), (8, 0.875), (9, 1.0), (13, 2.0), (39, 15.0), (94, 70.0),
     (109, 100.0), (123, 170.0), (124, 175.0), (125, None)],
)
def test_surface_speed_table(movement, speed):
    assert modes.decode_surface_speed(movement) == speed


def test_status_message_emergency():
    msg = modes.decode(enc.build_df17(0x111111, enc.encode_status("7700", emergency=1)))
    assert msg.kind == modes.STATUS
    assert msg.squawk == "7700"
    assert msg.emergency == "general"


# --- cross-checks against pyModeS ---------------------------------------------------------

def _oracle(frame):
    return pms.decode(frame.hex().upper())


def test_random_ident_matches_pymodes():
    rng = random.Random(21)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    for _ in range(200):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(3, 8)))
        tc = rng.randint(1, 4)
        frame = enc.build_df17(rng.getrandbits(24), enc.encode_ident(text, rng.randint(0, 7), tc))
        mine, ref = modes.decode(frame), _oracle(frame)
        assert mine.callsign == ref["callsign"] == text
        assert mine.tc == ref["typecode"] == tc


def test_random_airborne_position_matches_pymodes():
    rng = random.Random(22)
    for _ in range(300):
        alt = rng.randrange(-1000, 50000, 25)
        me = enc.encode_airborne_position(
            rng.getrandbits(17), rng.getrandbits(17), bool(rng.getrandbits(1)), alt, tc=rng.randint(9, 18)
        )
        frame = enc.build_df17(rng.getrandbits(24), me)
        mine, ref = modes.decode(frame), _oracle(frame)
        assert mine.altitude == ref["altitude"] == alt
        assert mine.cpr.lat == ref["cpr_lat"]
        assert mine.cpr.lon == ref["cpr_lon"]
        assert int(mine.cpr.odd) == ref["cpr_format"]


def test_random_velocity_matches_pymodes():
    rng = random.Random(23)
    for _ in range(300):
        gs = rng.uniform(0, 600)
        trk = rng.uniform(0, 360)
        vr = rng.randrange(-4000, 4000, 64)
        gap = rng.randrange(-1000, 1000, 25)
        me = enc.encode_velocity(gs, trk, vr, baro_rate=rng.random() < 0.5, geo_minus_baro_ft=gap)
        frame = enc.build_df17(rng.getrandbits(24), me)
        mine, ref = modes.decode(frame), _oracle(frame)
        assert mine.gs == pytest.approx(ref["groundspeed"], abs=1.0)
        assert mine.track == pytest.approx(ref["track"], abs=0.02)
        assert mine.vrate == ref["vertical_rate"]
        assert mine.geo_minus_baro == ref["geo_minus_baro"]
        assert mine.vrate_source == ("baro" if ref["vr_source"].lower().startswith("b") else "geom")


def test_random_surveillance_matches_pymodes():
    rng = random.Random(24)
    for _ in range(300):
        icao = rng.getrandbits(24)
        alt = rng.randrange(-1000, 50000, 25)
        for df in (4, 20):
            frame = enc.build_surveillance(df, icao, enc.encode_ac13(alt), fs=rng.randint(0, 5))
            assert modes.decode(frame).altitude == _oracle(frame)["altitude"] == alt
        code = f"{rng.randint(0, 7)}{rng.randint(0, 7)}{rng.randint(0, 7)}{rng.randint(0, 7)}"
        for df in (5, 21):
            frame = enc.build_surveillance(df, icao, enc.squawk_to_id13(code), fs=rng.randint(0, 5))
            assert modes.decode(frame).squawk == _oracle(frame)["squawk"] == code
            assert modes.decode(frame).icao == icao


def test_random_status_matches_pymodes():
    rng = random.Random(25)
    for _ in range(100):
        code = f"{rng.randint(0, 7)}{rng.randint(0, 7)}{rng.randint(0, 7)}{rng.randint(0, 7)}"
        frame = enc.build_df17(rng.getrandbits(24), enc.encode_status(code, rng.randint(0, 6)))
        mine, ref = modes.decode(frame), _oracle(frame)
        assert mine.squawk == ref["squawk"] == code
        assert (mine.emergency is not None) == (ref["emergency_state"] != 0)
