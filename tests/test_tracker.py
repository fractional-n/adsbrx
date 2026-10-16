import pytest

from adsbrx import cpr
from adsbrx.geo import distance_nm
from adsbrx.modes import decode
from adsbrx.sim.encoder import (
    build_df11,
    build_df17,
    build_surveillance,
    encode_ac13,
    encode_airborne_position,
    encode_ident,
    encode_status,
    encode_surface_position,
    encode_velocity,
    squawk_to_id13,
)
from adsbrx.tracker import Tracker

ICAO = 0x840ABC
HANEDA = (35.5494, 139.7798)


def pos_msg(lat, lon, odd, t, icao=ICAO, alt=12000, tc=11, rssi=-20.0):
    la, lo = cpr.encode(lat, lon, odd)
    return decode(build_df17(icao, encode_airborne_position(la, lo, odd, alt, tc)), ts=t, rssi=rssi)


def surface_msg(lat, lon, odd, t, icao=ICAO, movement=40, track=90.0):
    la, lo = cpr.encode(lat, lon, odd, surface=True)
    me = encode_surface_position(la, lo, odd, movement=movement, track_deg=track)
    return decode(build_df17(icao, me), ts=t, rssi=-20.0)


def feed_pair(tracker, lat, lon, t, **kw):
    first = tracker.update(pos_msg(lat, lon, False, t, **kw))
    second = tracker.update(pos_msg(lat, lon, True, t + 0.5, **kw))
    return first, second


def test_global_decode_from_even_odd_pair():
    tracker = Tracker()
    first, second = feed_pair(tracker, 35.80, 139.90, 0.0)
    assert first.new and not first.position_updated
    assert second.position_updated and not second.new
    ac = tracker.aircraft[ICAO]
    assert ac.lat == pytest.approx(35.80, abs=2e-4)
    assert ac.lon == pytest.approx(139.90, abs=2e-4)
    assert ac.alt_baro == 12000 and ac.on_ground is False
    assert ac.pos_reliable and len(ac.trail) == 1


def test_pair_window_is_enforced():
    tracker = Tracker()
    tracker.update(pos_msg(35.8, 139.9, False, 0.0))
    stale = tracker.update(pos_msg(35.8, 139.9, True, 11.0))
    assert not stale.position_updated and not tracker.aircraft[ICAO].has_position()
    fresh = tracker.update(pos_msg(35.8, 139.9, False, 12.0))
    assert fresh.position_updated


def test_single_frame_decodes_around_receiver_but_is_not_reliable():
    tracker = Tracker(receiver=HANEDA)
    upd = tracker.update(pos_msg(35.9, 140.2, True, 0.0))
    assert upd.position_updated
    ac = tracker.aircraft[ICAO]
    assert (ac.lat, ac.lon) == pytest.approx((35.9, 140.2), abs=2e-4)
    assert not ac.pos_reliable
    tracker.update(pos_msg(35.9, 140.2, False, 0.4))
    assert ac.pos_reliable


def test_local_decode_from_reliable_position():
    tracker = Tracker()
    feed_pair(tracker, 35.80, 139.90, 0.0)
    upd = tracker.update(pos_msg(35.83, 139.93, False, 20.0))  # odd partner is stale
    assert upd.position_updated
    ac = tracker.aircraft[ICAO]
    assert (ac.lat, ac.lon) == pytest.approx((35.83, 139.93), abs=2e-4)
    assert ac.pos_reliable


def test_without_reference_a_lone_frame_gives_no_position():
    tracker = Tracker()
    assert not tracker.update(pos_msg(35.8, 139.9, True, 0.0)).position_updated


def test_implausible_jump_needs_three_consistent_decodes():
    tracker = Tracker()
    feed_pair(tracker, 35.80, 139.90, 0.0)
    ac = tracker.aircraft[ICAO]
    t = 30.0
    results = []
    for i in range(8):
        results.append(tracker.update(pos_msg(40.80, 139.90, bool(i % 2), t + 0.5 * i)).position_updated)
    # frame 0 is local-decoded from the old position and rejected, 1-2 are rejected global
    # jumps, and 3 is the third consistent one that overrides the old position
    assert results[:3] == [False, False, False]
    assert results[3] is True
    assert ac.lat == pytest.approx(40.80, abs=2e-4)
    assert tracker.rejected_positions == 3


def test_isolated_bad_position_is_ignored():
    tracker = Tracker()
    feed_pair(tracker, 35.80, 139.90, 0.0)
    ac = tracker.aircraft[ICAO]
    tracker.update(pos_msg(35.80, 139.90, False, 29.0))  # refresh the even frame
    assert not tracker.update(pos_msg(38.0, 141.0, True, 29.2)).position_updated  # garbage pair
    assert ac.lat == pytest.approx(35.80, abs=2e-4)
    assert tracker.update(pos_msg(35.80, 139.90, False, 29.4)).position_updated


def test_max_range_rejects_far_positions():
    tracker = Tracker(receiver=HANEDA, max_range_nm=100)
    _, second = feed_pair(tracker, 38.5, 139.8, 0.0)  # ~175 NM north
    assert not second.position_updated and not tracker.aircraft[ICAO].has_position()
    _, ok = feed_pair(tracker, 36.0, 139.8, 5.0)
    assert ok.position_updated
    assert tracker.max_distance_nm == pytest.approx(distance_nm(*HANEDA, 36.0, 139.8), abs=0.1)


def test_surface_positions_need_a_reference():
    assert not Tracker().update(surface_msg(35.5494, 139.7798, False, 0.0)).position_updated
    tracker = Tracker(receiver=HANEDA)
    upd = tracker.update(surface_msg(35.5600, 139.7700, False, 0.0))
    assert upd.position_updated
    ac = tracker.aircraft[ICAO]
    assert (ac.lat, ac.lon) == pytest.approx((35.56, 139.77), abs=2e-4)
    assert ac.on_ground is True and ac.alt_baro is None
    assert ac.gs == pytest.approx(15.0, abs=1.0) and ac.track == pytest.approx(90.0, abs=3.0)


def test_surface_uses_last_reliable_airborne_position():
    tracker = Tracker()
    feed_pair(tracker, 35.5600, 139.7700, 0.0)
    upd = tracker.update(surface_msg(35.5610, 139.7710, True, 40.0))
    assert upd.position_updated
    ac = tracker.aircraft[ICAO]
    assert (ac.lat, ac.lon) == pytest.approx((35.561, 139.771), abs=2e-4)


def test_identity_velocity_status_and_altitudes():
    tracker = Tracker()
    tracker.update(decode(build_df17(ICAO, encode_ident("JAL123", 3)), ts=0.0, rssi=-20.0))
    tracker.update(decode(build_df17(ICAO, encode_airborne_position(0, 0, False, 30000)), ts=1.0, rssi=-20.0))
    tracker.update(
        decode(
            build_df17(ICAO, encode_velocity(450, 90, vrate_fpm=-1280, baro_rate=True, geo_minus_baro_ft=-200)),
            ts=2.0,
            rssi=-20.0,
        )
    )
    ac = tracker.aircraft[ICAO]
    assert ac.callsign == "JAL123" and ac.category == "A3"
    assert ac.alt_baro == 30000 and ac.alt_geom == 29800
    assert ac.gs == pytest.approx(450, abs=2) and ac.track == pytest.approx(90, abs=1)
    assert ac.vrate == -1280 and ac.vrate_source == "baro"

    tracker.update(decode(build_df17(ICAO, encode_status("7700", 1)), ts=3.0, rssi=-20.0))
    assert ac.squawk == "7700" and ac.emergency_state == "general"

    tracker.update(decode(build_surveillance(5, ICAO, squawk_to_id13("1200")), ts=4.0, rssi=-20.0))
    assert ac.squawk == "1200"
    ac.emergency = "none"
    assert ac.emergency_state is None
    tracker.update(decode(build_surveillance(4, ICAO, encode_ac13(29900)), ts=5.0, rssi=-20.0))
    assert ac.alt_baro == 29900 and ac.alt_geom == 29700
    assert ac.messages == 6 and ac.source == "adsb"


def test_mode_s_only_aircraft_and_all_call():
    tracker = Tracker()
    upd = tracker.update(decode(build_surveillance(4, 0x123456, encode_ac13(5000)), ts=0.0, rssi=-30.0))
    assert upd.new and upd.aircraft.source == "mode_s"
    tracker.update(decode(build_df11(0x123456, ca=4), ts=1.0, rssi=-30.0))
    assert tracker.aircraft[0x123456].on_ground is True


def test_non_icao_tisb_is_ignored():
    raw = build_df17(0x400001, encode_ident("TISB", 3), ca=1, df=18)  # CF=1: non-ICAO address
    assert decode(raw, ts=0.0).kind == "other"
    assert tracker_update(raw).aircraft is None


def tracker_update(raw):
    return Tracker().update(decode(raw, ts=0.0))


def test_visible_requires_two_messages():
    tracker = Tracker()
    tracker.update(decode(build_df17(ICAO, encode_ident("JAL1", 3)), ts=0.0, rssi=-20.0))
    assert tracker.visible() == []
    tracker.update(decode(build_df17(ICAO, encode_ident("JAL1", 3)), ts=1.0, rssi=-20.0))
    assert [a.icao for a in tracker.visible()] == [ICAO]


def test_expiry_and_field_aging():
    tracker = Tracker(expire_s=60, field_ttl_s=30)
    feed_pair(tracker, 35.8, 139.9, 0.0)
    other = 0x840DEF
    tracker.update(decode(build_df17(other, encode_ident("ANA9", 3)), ts=50.0, rssi=-20.0))
    assert tracker.expire(20.0) == []
    ac = tracker.aircraft[ICAO]
    assert ac.has_position() and ac.alt_baro == 12000
    removed = tracker.expire(40.0)
    assert removed == []
    assert not ac.has_position() and ac.alt_baro is None and not ac.pos_reliable
    removed = tracker.expire(61.0)
    assert [a.icao for a in removed] == [ICAO] and set(tracker.aircraft) == {other}


def test_trail_only_grows_when_the_aircraft_moves():
    tracker = Tracker()
    feed_pair(tracker, 35.8, 139.9, 0.0)
    ac = tracker.aircraft[ICAO]
    tracker.update(pos_msg(35.8, 139.9, False, 1.0))
    assert len(ac.trail) == 1
    tracker.update(pos_msg(35.81, 139.9, True, 2.0))
    tracker.update(pos_msg(35.82, 139.9, False, 3.0))
    assert len(ac.trail) == 3
    assert ac.trail[-1][1] == pytest.approx(35.82, abs=2e-4) and ac.trail[-1][3] == 12000


def test_rssi_is_smoothed_in_the_power_domain():
    tracker = Tracker()
    tracker.update(pos_msg(35.8, 139.9, False, 0.0, rssi=-20.0))
    assert tracker.aircraft[ICAO].rssi == pytest.approx(-20.0)
    tracker.update(pos_msg(35.8, 139.9, True, 0.5, rssi=-10.0))
    assert -20.0 < tracker.aircraft[ICAO].rssi < -10.0


def test_distance_bearing():
    tracker = Tracker(receiver=HANEDA)
    feed_pair(tracker, 36.5494, 139.7798, 0.0)
    dist, brg = tracker.distance_bearing(tracker.aircraft[ICAO])
    assert dist == pytest.approx(60.0, abs=0.5) and brg == pytest.approx(0.0, abs=0.5)
    assert Tracker().distance_bearing(tracker.aircraft[ICAO]) is None
