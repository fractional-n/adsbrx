import copy
import math

import numpy as np
import pytest

from adsbrx import modes
from adsbrx.demod import Demodulator
from adsbrx.geo import distance_nm
from adsbrx.modes import decode_surface_speed
from adsbrx.sim import encoder
from adsbrx.sim.modulator import SAMPLE_RATE
from adsbrx.sim.scenario import HANEDA, Scenario, SimAircraft, SimStream, default_scenario, offset
from adsbrx.tracker import Tracker


def make_aircraft(**kw):
    base = dict(icao=0x840001, callsign="TEST1", lat=35.0, lon=140.0, alt_ft=10000, gs_kt=360, track_deg=0)
    base.update(kw)
    return SimAircraft(**base)


class TestEncodeMovement:
    def test_roundtrip_for_every_code(self):
        for movement in range(1, 125):
            assert encoder.encode_movement(decode_surface_speed(movement)) == movement

    @pytest.mark.parametrize("speed", [0.0, 3.3, 12.0, 18.0, 45.0, 80.0, 150.0])
    def test_closest_speed(self, speed):
        decoded = decode_surface_speed(encoder.encode_movement(speed))
        assert abs(decoded - speed) <= 2.5


class TestAircraftMotion:
    def test_straight_north(self):
        ac = make_aircraft(vrate_fpm=600)
        ac.advance(600)  # 360 kt for 10 minutes = 60 NM = one degree of latitude
        assert ac.lat == pytest.approx(36.0, abs=1e-9)
        assert ac.lon == pytest.approx(140.0, abs=1e-9)
        assert ac.alt_ft == pytest.approx(16000)
        assert ac.track_deg == 0

    def test_straight_east_scales_with_latitude(self):
        ac = make_aircraft(lat=60.0, track_deg=90)
        ac.advance(600)
        assert ac.lat == pytest.approx(60.0, abs=1e-9)
        assert (ac.lon - 140.0) * 60 * math.cos(math.radians(60)) == pytest.approx(60.0, rel=1e-9)

    def test_advance_is_independent_of_call_history(self):
        stepped, direct = make_aircraft(turn_dps=3.0, track_deg=270), make_aircraft(turn_dps=3.0, track_deg=270)
        for t in (0.3, 1.7, 4.0, 11.5, 30.0):
            stepped.advance(t)
        direct.advance(30.0)
        assert (stepped.lat, stepped.lon, stepped.track_deg) == (direct.lat, direct.lon, direct.track_deg)

    def test_turn_follows_a_circle(self):
        ac = make_aircraft(gs_kt=180, track_deg=0, turn_dps=3.0)
        radius_nm = (180 / 3600) / math.radians(3.0)
        ac.advance(60)  # half a circle (180 degrees)
        assert ac.track_deg == pytest.approx(180)
        assert distance_nm(35.0, 140.0, ac.lat, ac.lon) == pytest.approx(2 * radius_nm, rel=0.01)
        ac.advance(120)  # full circle
        assert distance_nm(35.0, 140.0, ac.lat, ac.lon) < 0.01
        assert ac.track_deg == pytest.approx(0, abs=1e-6) or ac.track_deg == pytest.approx(360, abs=1e-6)

    def test_altitude_levels_off(self):
        ac = make_aircraft(alt_ft=2000, vrate_fpm=-1000)
        ac.advance(300)
        assert ac.alt_ft == 1500
        assert ac._vrate_now() == 0
        ac.advance(10)  # going back in time restores the earlier state
        assert ac.alt_ft == pytest.approx(2000 - 10000 / 60)

    def test_ground_aircraft_stay_on_the_ground(self):
        ac = make_aircraft(on_ground=True, alt_ft=0, gs_kt=12, vrate_fpm=500)
        ac.advance(100)
        assert ac.alt_ft == 0 and ac._vrate_now() == 0


class TestFrames:
    def test_every_kind_decodes_back(self):
        ac = make_aircraft(
            callsign="JAL123", alt_ft=33000, gs_kt=450, track_deg=90, vrate_fpm=-640, squawk="4271", geo_offset_ft=175
        )
        pos = modes.decode(ac.frame("position"))
        assert pos.kind == modes.AIRBORNE_POS and pos.altitude == 33000 and pos.cpr is not None
        vel = modes.decode(ac.frame("velocity"))
        assert vel.kind == modes.VELOCITY
        assert vel.gs == pytest.approx(450, abs=1.5) and vel.track == pytest.approx(90, abs=1)
        assert vel.vrate == -640 and vel.vrate_source == "baro" and vel.geo_minus_baro == 175
        ident = modes.decode(ac.frame("ident"))
        assert ident.callsign == "JAL123" and ident.category == "A3"
        status = modes.decode(ac.frame("status"))
        assert status.kind == modes.STATUS and status.squawk == "4271" and status.emergency is None
        assert modes.decode(ac.frame("allcall")).kind == modes.ALL_CALL
        alt = modes.decode(ac.frame("altitude"))
        assert alt.kind == modes.SURV_ALT and alt.altitude == 33000 and alt.icao == ac.icao
        sq = modes.decode(ac.frame("squawk"))
        assert sq.kind == modes.SURV_ID and sq.squawk == "4271" and sq.icao == ac.icao

    def test_position_alternates_even_and_odd(self):
        ac = make_aircraft()
        flags = [modes.decode(ac.frame("position")).cpr.odd for _ in range(4)]
        assert flags[0] != flags[1] and flags[0] == flags[2] and flags[1] == flags[3]

    def test_ground_aircraft_emit_surface_frames(self):
        ac = make_aircraft(on_ground=True, alt_ft=0, gs_kt=15, track_deg=270, lat=35.55, lon=139.78)
        msg = modes.decode(ac.frame("position"))
        assert msg.kind == modes.SURFACE_POS and msg.cpr.surface
        assert msg.gs == pytest.approx(15, abs=1) and msg.track == pytest.approx(270, abs=3)
        assert modes.decode(ac.frame("allcall")).on_ground is True
        assert modes.decode(ac.frame("altitude")).on_ground is True


class TestScenario:
    def test_events_are_ordered_and_valid(self):
        events = default_scenario().events()
        last = -1.0
        for _ in range(600):
            t, frame, amplitude = next(events)
            assert t >= last
            last = t
            msg = modes.decode(frame)
            assert msg is not None
            assert 0 < amplitude <= 120

    def test_same_seed_same_traffic(self):
        a, b = default_scenario(seed=5).events(), default_scenario(seed=5).events()
        assert [next(a) for _ in range(200)] == [next(b) for _ in range(200)]
        c = default_scenario(seed=6).events()
        assert [next(c) for _ in range(20)] != [next(default_scenario(seed=5).events()) for _ in range(20)]

    def test_amplitude_falls_with_range(self):
        near = make_aircraft(lat=HANEDA[0], lon=offset(*HANEDA, 90, 20)[1])
        far = make_aircraft(lat=HANEDA[0], lon=offset(*HANEDA, 90, 200)[1])
        scenario = Scenario([near, far], fading_db=0.0)
        assert scenario.amplitude(near) / scenario.amplitude(far) == pytest.approx(10.0, rel=0.01)
        very_near = make_aircraft(lat=HANEDA[0], lon=HANEDA[1])
        assert scenario.amplitude(very_near) == scenario.amplitude_cap

    def test_truth_matches_live_aircraft(self):
        scenario = default_scenario(respawn=False)
        events = scenario.events()
        t = 0.0
        while t < 40.0:
            t, _, _ = next(events)
        for live in scenario.aircraft:
            truth = scenario.truth(live.icao, live.time)
            assert (live.lat, live.lon, live.alt_ft) == (truth.lat, truth.lon, truth.alt_ft)

    def test_truth_unknown_icao(self):
        with pytest.raises(KeyError):
            default_scenario().truth(0x123456, 0.0)

    def test_respawn_replaces_aircraft_that_left(self):
        gone = make_aircraft(icao=0x840099, lat=HANEDA[0], lon=HANEDA[1], gs_kt=500, track_deg=90)
        scenario = Scenario([gone], respawn=True, respawn_nm=50.0, spawn_nm=40.0, seed=3)
        events = scenario.events()
        seen = set()
        t = 0.0
        while t < 5400.0:  # arrivals cross the 50 NM area in 10-20 minutes
            t, frame, _ = next(events)
            seen.add(modes.decode(frame).icao)
        assert 0x840099 in seen and len(seen) >= 4
        replacement = scenario.aircraft[0]
        assert replacement.icao != 0x840099
        assert distance_nm(*HANEDA, replacement.lat, replacement.lon) <= 51.0
        assert replacement.t0 > 0 and replacement.callsign[:3].isalpha()

    def test_no_respawn_keeps_aircraft(self):
        gone = make_aircraft(icao=0x840099, lat=HANEDA[0], lon=HANEDA[1], gs_kt=500, track_deg=90)
        scenario = Scenario([gone], respawn=False)
        events = scenario.events()
        seen = set()
        t = 0.0
        while t < 600.0:
            t, frame, _ = next(events)
            seen.add(modes.decode(frame).icao)
        assert seen == {0x840099}


class TestStream:
    def test_block_size_does_not_change_the_signal(self):
        total = 600_000
        whole = SimStream(default_scenario(respawn=False), noise_sigma=0.0, seed=1).read(total)
        chunked_stream = SimStream(default_scenario(respawn=False), noise_sigma=0.0, seed=1)
        chunks = [1, 4999, 100_000, 7, 3, 94_990, 400_000]
        assert sum(chunks) == total
        chunked = b"".join(chunked_stream.read(n) for n in chunks)
        assert chunked == whole
        assert chunked_stream.position == total

    def test_block_format(self):
        data = SimStream(default_scenario(), seed=2).read(1000)
        assert len(data) == 2000
        arr = np.frombuffer(data, dtype=np.uint8)
        assert abs(arr.mean() - 127.5) < 5


def run_chain(scenario, seconds, *, seed=3, block=131072, **demod_args):
    stream = SimStream(scenario, seed=seed)
    demod = Demodulator(**demod_args)
    tracker = Tracker(receiver=scenario.receiver)
    messages = []
    total = int(seconds * SAMPLE_RATE)
    pos = 0
    while pos < total:
        n = min(block, total - pos)
        for det in demod.process(stream.read(n), start=pos):
            msg = modes.decode(
                det.frame, ts=det.sample / SAMPLE_RATE, sample=det.sample, rssi=det.rssi, corrected=det.corrected
            )
            if msg is not None:
                messages.append(msg)
                tracker.update(msg)
        pos += n
    return tracker, messages, demod


@pytest.fixture(scope="module")
def chain():
    scenario = default_scenario(respawn=False)
    tracker, messages, demod = run_chain(scenario, 40.0)
    return scenario, tracker, messages, demod


class TestEndToEnd:
    def test_traffic_is_tracked(self, chain):
        scenario, tracker, _, demod = chain
        tracked = {ac.icao for ac in tracker.visible()}
        assert len(tracked) >= 11
        for icao in (0x840A01, 0x840B22, 0x840E88, 0x840F31, 0x840D77):
            assert icao in tracked
        assert demod.stats.gaps == 0 and demod.stats.df17 > 500

    def test_positions_match_ground_truth(self, chain):
        scenario, tracker, _, _ = chain
        checked = 0
        for ac in tracker.visible():
            if ac.lat is None:
                continue
            truth = scenario.truth(ac.icao, ac.t_pos)
            assert distance_nm(ac.lat, ac.lon, truth.lat, truth.lon) < 0.02, ac.hex
            checked += 1
        assert checked >= 10
        assert tracker.rejected_positions == 0

    def test_other_fields_match_ground_truth(self, chain):
        # Whatever the tracker knows must be right; nearby aircraft must be complete.
        scenario, tracker, _, _ = chain
        complete = 0
        for ac in tracker.visible():
            sim = next(a for a in scenario.aircraft if a.icao == ac.icao)
            near = distance_nm(*scenario.receiver, sim.lat, sim.lon) < 50
            fields = [ac.callsign, ac.squawk, ac.category, ac.gs, ac.geo_minus_baro]
            if not sim.on_ground:
                fields += [ac.alt_baro, ac.track, ac.vrate]
            if near:
                assert None not in fields, ac.hex
                complete += 1
            if ac.callsign is not None:
                assert ac.callsign == sim.callsign
            if ac.squawk is not None:
                assert ac.squawk == sim.squawk
            if ac.category is not None:
                assert ac.category == f"A{sim.category}"
            if sim.on_ground:
                assert ac.on_ground is True and ac.alt_baro is None
                continue
            assert ac.on_ground is False
            if ac.alt_baro is not None:
                assert abs(ac.alt_baro - scenario.truth(ac.icao, ac.t_alt).alt_ft) <= 25
            if ac.gs is not None:
                assert ac.gs == pytest.approx(sim.gs_kt, abs=1.5)
            if ac.geo_minus_baro is not None:
                assert ac.geo_minus_baro == pytest.approx(sim.geo_offset_ft, abs=25)
            if ac.icao != 0x840F31:  # the turning aircraft changes track continuously
                if ac.track is not None:
                    assert ac.track == pytest.approx(sim.track_deg, abs=1.5)
                if ac.vrate is not None:
                    assert ac.vrate == pytest.approx(sim.vrate_fpm, abs=64)
        assert complete >= 7

    def test_receiver_statistics(self, chain):
        _, tracker, _, _ = chain
        assert 100 < tracker.max_distance_nm < 160
        assert tracker.total_messages > 1000

    def test_mode_s_replies_with_all_df(self):
        scenario = default_scenario(respawn=False)
        tracker, messages, demod = run_chain(scenario, 20.0, all_df=True)
        alts = [m for m in messages if m.kind == modes.SURV_ALT]
        squawks = [m for m in messages if m.kind == modes.SURV_ID]
        assert len(alts) > 10 and len(squawks) > 10
        by_icao = {a.icao: a for a in scenario.aircraft}
        good = sum(1 for m in squawks if m.icao in by_icao and m.squawk == by_icao[m.icao].squawk)
        assert good >= 0.95 * len(squawks)
        assert demod.stats.other_df > 0
