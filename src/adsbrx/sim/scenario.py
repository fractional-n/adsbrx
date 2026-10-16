"""Synthetic air traffic for the built-in signal simulator."""

from __future__ import annotations

import copy
import heapq
import math
from collections.abc import Iterator
from dataclasses import dataclass, field

import numpy as np

from .. import cpr
from ..geo import distance_nm
from . import encoder
from .modulator import SAMPLE_RATE, Burst, IqSynthesizer

HANEDA = (35.5494, 139.7798)
ALT_CEILING_FT = 45000.0
ALT_FLOOR_FT = 1500.0

# name, mean interval [s], jitter [s]
_EMITTERS = (
    ("position", 0.5, 0.1),
    ("velocity", 0.5, 0.1),
    ("ident", 5.0, 0.5),
    ("status", 5.0, 0.5),
    ("allcall", 1.0, 0.2),
    ("altitude", 2.0, 1.0),
    ("squawk", 4.0, 2.0),
)

_AIRLINES = ("JAL", "ANA", "SKY", "APJ", "SFJ", "ADO", "JJP", "UAL", "CPA", "KAL", "CCA", "EVA", "SIA", "AAL")
# ICAO address blocks: Japan, USA, China, South Korea
_ICAO_BLOCKS = ((0x840000, 0x87FFFF, 0.7), (0xA00000, 0xAFFFFF, 0.15), (0x780000, 0x7BFFFF, 0.08), (0x718000, 0x71FFFF, 0.07))


def offset(lat: float, lon: float, bearing_deg: float, dist_nm: float) -> tuple[float, float]:
    """Point `dist_nm` away from (lat, lon) towards `bearing_deg` (flat-earth approximation)."""
    rad = math.radians(bearing_deg)
    return (
        lat + dist_nm * math.cos(rad) / 60.0,
        lon + dist_nm * math.sin(rad) / (60.0 * math.cos(math.radians(lat))),
    )


@dataclass
class SimAircraft:
    """An aircraft whose position is a closed-form function of time (straight leg or constant turn)."""

    icao: int
    callsign: str
    lat: float
    lon: float
    alt_ft: float
    gs_kt: float
    track_deg: float
    vrate_fpm: float = 0.0
    turn_dps: float = 0.0
    squawk: str = "2000"
    category: int = 3
    on_ground: bool = False
    emergency: int = 0
    geo_offset_ft: int = 150
    t0: float = 0.0
    time: float = 0.0
    _odd: bool = False
    _origin: tuple[float, float, float, float] = field(init=False, repr=False, default=(0.0, 0.0, 0.0, 0.0))

    def __post_init__(self) -> None:
        self._origin = (self.lat, self.lon, self.track_deg, self.alt_ft)
        self.time = self.t0

    def advance(self, t: float) -> None:
        """Move the aircraft to its state at stream time `t` (independent of earlier calls)."""
        lat0, lon0, trk0, alt0 = self._origin
        dt = max(t - self.t0, 0.0)
        v = self.gs_kt / 3600.0  # NM per second
        if abs(self.turn_dps) < 1e-9:
            theta = math.radians(trk0)
            north, east = v * dt * math.cos(theta), v * dt * math.sin(theta)
            trk = trk0
        else:
            w = math.radians(self.turn_dps)
            th0, th1 = math.radians(trk0), math.radians(trk0 + self.turn_dps * dt)
            north = v / w * (math.sin(th1) - math.sin(th0))
            east = v / w * (math.cos(th0) - math.cos(th1))
            trk = (trk0 + self.turn_dps * dt) % 360.0
        self.lat = lat0 + north / 60.0
        self.lon = lon0 + east / (60.0 * math.cos(math.radians(lat0)))
        self.track_deg = trk
        if self.on_ground:
            self.alt_ft = 0.0
        else:
            self.alt_ft = min(max(alt0 + self.vrate_fpm * dt / 60.0, ALT_FLOOR_FT), ALT_CEILING_FT)
        self.time = t

    def _vrate_now(self) -> int:
        level = (self.alt_ft <= ALT_FLOOR_FT and self.vrate_fpm < 0) or (
            self.alt_ft >= ALT_CEILING_FT and self.vrate_fpm > 0
        )
        return 0 if level or self.on_ground else int(self.vrate_fpm)

    def frame(self, kind: str) -> bytes:
        """The next transmission of type `kind` (one of the `_EMITTERS` names)."""
        icao = self.icao
        if kind == "position":
            self._odd = not self._odd
            if self.on_ground:
                la, lo = cpr.encode(self.lat, self.lon, self._odd, surface=True)
                me = encoder.encode_surface_position(
                    la, lo, self._odd, movement=encoder.encode_movement(self.gs_kt), track_deg=self.track_deg
                )
            else:
                la, lo = cpr.encode(self.lat, self.lon, self._odd)
                me = encoder.encode_airborne_position(la, lo, self._odd, int(self.alt_ft))
            return encoder.build_df17(icao, me)
        if kind == "velocity":
            me = encoder.encode_velocity(
                self.gs_kt, self.track_deg, self._vrate_now(), baro_rate=True, geo_minus_baro_ft=self.geo_offset_ft
            )
            return encoder.build_df17(icao, me)
        if kind == "ident":
            return encoder.build_df17(icao, encoder.encode_ident(self.callsign, self.category))
        if kind == "status":
            return encoder.build_df17(icao, encoder.encode_status(self.squawk, self.emergency))
        if kind == "allcall":
            return encoder.build_df11(icao, ca=4 if self.on_ground else 5)
        fs = 1 if self.on_ground else 0
        if kind == "altitude":
            return encoder.build_surveillance(4, icao, encoder.encode_ac13(int(self.alt_ft)), fs)
        return encoder.build_surveillance(5, icao, encoder.squawk_to_id13(self.squawk), fs)


class Scenario:
    """A set of aircraft emitting Mode S messages as seen by a receiver.

    With `respawn`, airborne aircraft that fly further than `respawn_nm` from the
    receiver are replaced by new traffic entering at the edge of the area, so a
    simulation can run indefinitely. `truth()` is only valid without respawn.
    """

    def __init__(
        self,
        aircraft: list[SimAircraft],
        receiver: tuple[float, float] = HANEDA,
        seed: int = 1,
        fading_db: float = 1.0,
        amplitude_at_1nm: float = 1500.0,
        amplitude_cap: float = 120.0,
        respawn: bool = False,
        respawn_nm: float = 170.0,
        spawn_nm: float = 150.0,
    ):
        self.receiver = receiver
        self.rng = np.random.default_rng(seed)
        self.fading_db = fading_db
        self.amplitude_at_1nm = amplitude_at_1nm
        self.amplitude_cap = amplitude_cap
        self.respawn = respawn
        self.respawn_nm = respawn_nm
        self.spawn_nm = spawn_nm
        self.aircraft = aircraft
        self._initial = [copy.copy(a) for a in aircraft]

    def truth(self, icao: int, t: float) -> SimAircraft:
        """Ground truth state of one of the initial aircraft at stream time `t`."""
        for ac in self._initial:
            if ac.icao == icao:
                state = copy.copy(ac)
                state.advance(t)
                return state
        raise KeyError(icao)

    def amplitude(self, ac: SimAircraft) -> float:
        """Peak signal amplitude in cu8 counts: inverse with range, plus fading."""
        rng_nm = max(distance_nm(self.receiver[0], self.receiver[1], ac.lat, ac.lon), 0.5)
        fade = 10.0 ** (self.rng.normal(0.0, self.fading_db) / 20.0)
        return min(self.amplitude_cap, self.amplitude_at_1nm / rng_nm * fade)

    def _random_aircraft(self, t: float) -> SimAircraft:
        rng = self.rng
        bearing = float(rng.uniform(0.0, 360.0))
        track = (bearing + 180.0 + float(rng.uniform(-35.0, 35.0))) % 360.0
        lat, lon = offset(self.receiver[0], self.receiver[1], bearing, self.spawn_nm)
        profile = int(rng.integers(0, 3))
        if profile == 0:  # cruise
            alt, gs, vrate = float(rng.integers(28, 41)) * 1000, float(rng.integers(420, 500)), 0.0
        elif profile == 1:  # climb
            alt, gs, vrate = float(rng.integers(4, 16)) * 1000, float(rng.integers(250, 330)), float(rng.integers(15, 26)) * 100
        else:  # descent
            alt, gs, vrate = float(rng.integers(15, 31)) * 1000, float(rng.integers(300, 400)), -float(rng.integers(15, 21)) * 100
        used = {a.icao for a in self.aircraft}
        while True:
            lo, hi, _ = _ICAO_BLOCKS[int(rng.choice(len(_ICAO_BLOCKS), p=[b[2] for b in _ICAO_BLOCKS]))]
            icao = int(rng.integers(lo, hi + 1))
            if icao not in used:
                break
        callsign = f"{_AIRLINES[int(rng.integers(0, len(_AIRLINES)))]}{int(rng.integers(1, 9999))}"
        squawk = "".join(str(int(d)) for d in rng.integers(0, 8, size=4))
        return SimAircraft(
            icao, callsign, lat, lon, alt, gs, track, vrate,
            squawk=squawk, category=int(rng.choice([3, 3, 5])), t0=t,
        )

    def events(self) -> Iterator[tuple[float, bytes, float]]:
        """Endless (time, frame, peak amplitude) tuples in time order."""
        heap: list[tuple[float, int, int, int]] = []
        counter = 0
        for ai in range(len(self.aircraft)):
            for ei, (_, interval, _) in enumerate(_EMITTERS):
                heap.append((float(self.rng.uniform(0.0, interval)), counter, ai, ei))
                counter += 1
        heapq.heapify(heap)
        while True:
            t, _, ai, ei = heapq.heappop(heap)
            ac = self.aircraft[ai]
            ac.advance(t)
            if (
                self.respawn
                and not ac.on_ground
                and distance_nm(self.receiver[0], self.receiver[1], ac.lat, ac.lon) > self.respawn_nm
            ):
                ac = self.aircraft[ai] = self._random_aircraft(t)
                ac.advance(t)
            name, interval, jitter = _EMITTERS[ei]
            yield t, ac.frame(name), self.amplitude(ac)
            counter += 1
            nxt = t + interval + float(self.rng.uniform(-jitter, jitter))
            heapq.heappush(heap, (max(nxt, t + 0.05), counter, ai, ei))


class SimStream:
    """Renders a scenario as a continuous 2 MS/s cu8 sample stream.

    Burst phases are drawn from their own generator, so the transmissions do not
    depend on how the stream is cut into blocks; only the noise does.
    """

    def __init__(self, scenario: Scenario, noise_sigma: float = 2.0, seed: int = 0):
        self.scenario = scenario
        self.synth = IqSynthesizer(noise_sigma, seed=seed)
        self._burst_rng = np.random.default_rng(seed + 7919)
        self._events = scenario.events()
        self._pending = self._next_burst()
        self._active: list[Burst] = []
        self.position = 0  # next sample to be produced

    def _next_burst(self) -> Burst:
        t, frame, amplitude = next(self._events)
        phase = int(self._burst_rng.integers(0, self.synth.channel.oversample))
        burst = self.synth.make_burst(int(round(t * SAMPLE_RATE)), frame, amplitude, phase)
        burst.carrier = float(self._burst_rng.uniform(0.0, 2.0 * math.pi))
        return burst

    def read(self, count: int) -> bytes:
        """The next `count` samples as interleaved unsigned 8-bit I/Q."""
        start, end = self.position, self.position + count
        while self._pending.first < end:
            self._active.append(self._pending)
            self._pending = self._next_burst()
        data = self.synth.synthesize(start, count, self._active)
        self._active = [b for b in self._active if b.end > end]
        self.position = end
        return data


def default_scenario(receiver: tuple[float, float] = HANEDA, seed: int = 1, respawn: bool = True) -> Scenario:
    """A dozen aircraft around Tokyo Haneda: a taxiing jet, arrivals, departures and distant airliners."""
    # icao, callsign, bearing from receiver, range NM, alt ft, gs kt, track, vrate fpm, extras
    rows = [
        (0x840A01, "JAL515", 225, 12, 8000, 230, 45, -1500, {}),
        (0x840B22, "ANA232", 90, 30, 12000, 280, 90, 2200, {"squawk": "4271"}),
        (0x841123, "SKY721", 330, 45, 31000, 440, 200, 0, {"squawk": "5421"}),
        (0x86A3D1, "APJ1105", 270, 70, 36000, 470, 90, 0, {"squawk": "3321"}),
        (0xA1B2C3, "UAL838", 140, 95, 38000, 520, 290, 0, {"squawk": "6210"}),
        (0x780123, "CCA927", 235, 110, 35000, 480, 60, 0, {"squawk": "2145"}),
        (0x71BA12, "KAL703", 310, 60, 28000, 430, 135, -800, {"squawk": "3604", "category": 5}),
        (0x840F31, "JJP022", 185, 18, 5000, 190, 270, 0, {"turn_dps": 3.0, "squawk": "1473"}),
        (0x860A05, "ADO57", 40, 25, 9500, 300, 20, 1800, {"squawk": "2530", "category": 2}),
        (0x84C456, "JTA611", 220, 140, 37000, 450, 30, 0, {"squawk": "4630"}),
        (0x840D77, "SFJ31", 95, 0.8, 0, 15, 270, 0, {"on_ground": True, "turn_dps": 5.0, "squawk": "0000"}),
        (0x840E88, "ANA854", 280, 20, 3000, 150, 90, -700, {"squawk": "3175"}),
    ]
    aircraft = []
    for icao, call, bearing, dist, alt, gs, track, vrate, extra in rows:
        lat, lon = offset(receiver[0], receiver[1], bearing, dist)
        aircraft.append(SimAircraft(icao, call, lat, lon, alt, gs, track, vrate, **extra))
    return Scenario(aircraft, receiver=receiver, seed=seed, respawn=respawn)
