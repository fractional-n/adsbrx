"""Aircraft tracking: merges decoded Mode S messages into per-aircraft state.

All times are stream seconds (`ModeSMessage.ts`), so replaying a recording gives the
same result as live reception.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from . import cpr
from .geo import bearing_deg, distance_nm
from .modes import (
    AIR_AIR,
    AIRBORNE_POS,
    ALL_CALL,
    IDENT,
    OTHER,
    STATUS,
    SURFACE_POS,
    SURV_ALT,
    SURV_ID,
    VELOCITY,
    ModeSMessage,
)

PAIR_WINDOW_S = 10.0  # max age difference between the even and odd frame of a global decode
LOCAL_REF_AGE_S = 30.0  # a reliable position may seed local decoding for this long
SURFACE_REF_AGE_S = 120.0
RECEIVER_LOCAL_MAX_NM = 150.0  # airborne local decode around the receiver is unambiguous up to 180 NM
SPEED_SLACK_NM = 1.0
CONFIRM_BAD_POSITIONS = 3  # consistent "implausible" positions that override the previous one
TRAIL_MIN_STEP_NM = 0.03
TRAIL_MAX_GAP_S = 10.0
EMERGENCY_SQUAWKS = {"7500": "unlawful", "7600": "nordo", "7700": "general"}
SOURCE_RANK = {"mode_s": 0, "tisb": 1, "adsb": 2}


@dataclass(slots=True)
class Aircraft:
    icao: int
    first_seen: float
    last_seen: float
    messages: int = 0
    source: str = "mode_s"
    callsign: str | None = None
    category: str | None = None
    squawk: str | None = None
    emergency: str | None = None
    alert: bool = False
    spi: bool = False
    on_ground: bool | None = None
    alt_baro: int | None = None
    alt_geom: int | None = None
    geo_minus_baro: int | None = None
    gs: float | None = None
    track: float | None = None
    vrate: int | None = None
    vrate_source: str | None = None
    heading: float | None = None
    airspeed: int | None = None
    airspeed_type: str | None = None
    lat: float | None = None
    lon: float | None = None
    t_pos: float | None = None
    t_alt: float | None = None
    t_vel: float | None = None
    t_gmb: float | None = None
    pos_reliable: bool = False
    rssi_power: float = 0.0
    cpr_even: tuple | None = None
    cpr_odd: tuple | None = None
    pending: tuple | None = None
    trail: deque = field(default_factory=lambda: deque(maxlen=400))

    @property
    def hex(self) -> str:
        return f"{self.icao:06X}"

    @property
    def rssi(self) -> float | None:
        return 10.0 * math.log10(self.rssi_power) if self.rssi_power > 0 else None

    @property
    def emergency_state(self) -> str | None:
        if self.emergency and self.emergency != "none":
            return self.emergency
        return EMERGENCY_SQUAWKS.get(self.squawk or "")

    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None


@dataclass(slots=True)
class Update:
    aircraft: Aircraft | None
    new: bool = False
    position_updated: bool = False


class Tracker:
    def __init__(
        self,
        receiver: tuple[float, float] | None = None,
        expire_s: float = 60.0,
        field_ttl_s: float = 60.0,
        max_range_nm: float = 300.0,
        min_messages: int = 2,
        trail_len: int = 400,
    ):
        self.receiver = receiver
        self.expire_s = expire_s
        self.field_ttl_s = field_ttl_s
        self.max_range_nm = max_range_nm
        self.min_messages = min_messages
        self.trail_len = trail_len
        self.aircraft: dict[int, Aircraft] = {}
        self.now = 0.0
        self.total_messages = 0
        self.positions = 0
        self.rejected_positions = 0
        self.max_distance_nm = 0.0

    # -- public ---------------------------------------------------------------------------

    def update(self, msg: ModeSMessage) -> Update:
        if msg.df == 18 and msg.kind == OTHER:  # non-ICAO TIS-B / unsupported control field
            return Update(None)
        now = msg.ts
        if now > self.now:
            self.now = now
        ac = self.aircraft.get(msg.icao)
        new = ac is None
        if ac is None:
            ac = Aircraft(msg.icao, first_seen=now, last_seen=now, trail=deque(maxlen=self.trail_len))
            self.aircraft[msg.icao] = ac
        ac.last_seen = max(ac.last_seen, now)
        ac.messages += 1
        self.total_messages += 1
        self._update_rssi(ac, msg.rssi)

        source = "adsb" if msg.df == 17 else "tisb" if msg.df == 18 else "mode_s"
        if SOURCE_RANK[source] > SOURCE_RANK[ac.source]:
            ac.source = source

        moved = False
        kind = msg.kind
        if msg.on_ground is not None:
            ac.on_ground = msg.on_ground
        if kind == IDENT:
            if msg.callsign:
                ac.callsign = msg.callsign
            ac.category = msg.category
        elif kind == AIRBORNE_POS:
            self._set_altitude(ac, msg.altitude, now)
            ac.alert, ac.spi = msg.alert, msg.spi
            if msg.cpr is not None:
                moved = self._update_position(ac, msg, now)
        elif kind == SURFACE_POS:
            ac.alt_baro = ac.alt_geom = None
            ac.t_alt = None
            if msg.gs is not None:
                ac.gs, ac.t_vel = msg.gs, now
            if msg.track is not None:
                ac.track, ac.t_vel = msg.track, now
            if msg.cpr is not None:
                moved = self._update_position(ac, msg, now)
        elif kind == VELOCITY:
            self._update_velocity(ac, msg, now)
        elif kind == STATUS:
            ac.emergency = msg.emergency
            if msg.squawk:
                ac.squawk = msg.squawk
        elif kind in (SURV_ALT, AIR_AIR):
            self._set_altitude(ac, msg.altitude, now)
            ac.alert, ac.spi = msg.alert, msg.spi
        elif kind == SURV_ID:
            if msg.squawk:
                ac.squawk = msg.squawk
            ac.alert, ac.spi = msg.alert, msg.spi
        elif kind == ALL_CALL:
            pass
        return Update(ac, new, moved)

    def expire(self, now: float) -> list[Aircraft]:
        """Drop aircraft that went silent and invalidate stale fields."""
        if now > self.now:
            self.now = now
        removed = []
        for icao, ac in list(self.aircraft.items()):
            if now - ac.last_seen > self.expire_s:
                del self.aircraft[icao]
                removed.append(ac)
            else:
                self._age_fields(ac, now)
        return removed

    def visible(self) -> list[Aircraft]:
        """Aircraft heard often enough to be trusted (filters one-off false detections)."""
        return [a for a in self.aircraft.values() if a.messages >= self.min_messages]

    def distance_bearing(self, ac: Aircraft) -> tuple[float, float] | None:
        if self.receiver is None or not ac.has_position():
            return None
        rlat, rlon = self.receiver
        return distance_nm(rlat, rlon, ac.lat, ac.lon), bearing_deg(rlat, rlon, ac.lat, ac.lon)

    # -- field updates ----------------------------------------------------------------------

    @staticmethod
    def _update_rssi(ac: Aircraft, rssi_db: float) -> None:
        power = 10.0 ** (rssi_db / 10.0)
        ac.rssi_power = power if ac.rssi_power == 0.0 else 0.8 * ac.rssi_power + 0.2 * power

    def _set_altitude(self, ac: Aircraft, altitude: int | None, now: float) -> None:
        if altitude is None:
            return
        ac.alt_baro = altitude
        ac.t_alt = now
        self._refresh_geometric(ac, now)

    @staticmethod
    def _refresh_geometric(ac: Aircraft, now: float) -> None:
        if ac.alt_baro is not None and ac.geo_minus_baro is not None and ac.t_gmb is not None and now - ac.t_gmb <= 60:
            ac.alt_geom = ac.alt_baro + ac.geo_minus_baro
        else:
            ac.alt_geom = None

    def _update_velocity(self, ac: Aircraft, msg: ModeSMessage, now: float) -> None:
        seen = False
        if msg.gs is not None:
            ac.gs, ac.track = msg.gs, msg.track
            seen = True
        if msg.heading is not None:
            ac.heading = msg.heading
            seen = True
        if msg.airspeed is not None:
            ac.airspeed, ac.airspeed_type = msg.airspeed, msg.airspeed_type
            seen = True
        if msg.vrate is not None:
            ac.vrate, ac.vrate_source = msg.vrate, msg.vrate_source
            seen = True
        if seen:
            ac.t_vel = now
        if msg.geo_minus_baro is not None:
            ac.geo_minus_baro, ac.t_gmb = msg.geo_minus_baro, now
            self._refresh_geometric(ac, now)

    def _age_fields(self, ac: Aircraft, now: float) -> None:
        ttl = self.field_ttl_s
        if ac.t_pos is not None and now - ac.t_pos > ttl:
            ac.lat = ac.lon = ac.t_pos = None
            ac.pos_reliable = False
        if ac.t_alt is not None and now - ac.t_alt > ttl:
            ac.alt_baro = ac.alt_geom = ac.t_alt = None
        if ac.t_vel is not None and now - ac.t_vel > ttl:
            ac.gs = ac.track = ac.vrate = ac.vrate_source = None
            ac.heading = ac.airspeed = ac.airspeed_type = ac.t_vel = None
        if ac.t_gmb is not None and now - ac.t_gmb > ttl:
            ac.geo_minus_baro = ac.t_gmb = None
            ac.alt_geom = None

    # -- positions -------------------------------------------------------------------------

    def _update_position(self, ac: Aircraft, msg: ModeSMessage, now: float) -> bool:
        c = msg.cpr
        entry = (c.lat, c.lon, now, c.surface)
        if c.odd:
            ac.cpr_odd = entry
        else:
            ac.cpr_even = entry

        if c.surface:
            decoded = self._decode_surface(ac, c, now)
        else:
            decoded = self._decode_airborne(ac, c, now)
        if decoded is None:
            return False
        lat, lon, how = decoded
        if not self._plausible(ac, lat, lon, now, how):
            self.rejected_positions += 1
            return False
        self._set_position(ac, lat, lon, now, how)
        return True

    def _decode_airborne(self, ac: Aircraft, c, now: float):
        other = ac.cpr_even if c.odd else ac.cpr_odd
        if other is not None and not other[3] and abs(now - other[2]) <= PAIR_WINDOW_S:
            if c.odd:
                result = cpr.decode_global(other[0], other[1], c.lat, c.lon, odd_latest=True)
            else:
                result = cpr.decode_global(c.lat, c.lon, other[0], other[1], odd_latest=False)
            if result is not None:
                return result[0], result[1], "global"

        if ac.pos_reliable and ac.t_pos is not None and now - ac.t_pos <= LOCAL_REF_AGE_S:
            lat, lon = cpr.decode_local(ac.lat, ac.lon, c.lat, c.lon, c.odd)
            return lat, lon, "local"
        if self.receiver is not None:
            lat, lon = cpr.decode_local(self.receiver[0], self.receiver[1], c.lat, c.lon, c.odd)
            if distance_nm(self.receiver[0], self.receiver[1], lat, lon) <= RECEIVER_LOCAL_MAX_NM:
                return lat, lon, "receiver"
        return None

    def _decode_surface(self, ac: Aircraft, c, now: float):
        """Surface CPR is only unambiguous within 45 NM of the reference position."""
        if ac.pos_reliable and ac.t_pos is not None and now - ac.t_pos <= SURFACE_REF_AGE_S:
            ref, how = (ac.lat, ac.lon), "local"
        elif self.receiver is not None:
            ref, how = self.receiver, "receiver"
        else:
            return None
        lat, lon = cpr.decode_local(ref[0], ref[1], c.lat, c.lon, c.odd, surface=True)
        return lat, lon, how

    def _limit_nm(self, ac: Aircraft, dt: float) -> float:
        speed_kt = 2.0 * ac.gs + 100.0 if ac.gs is not None else 1500.0
        return SPEED_SLACK_NM + speed_kt * max(dt, 0.0) / 3600.0

    def _plausible(self, ac: Aircraft, lat: float, lon: float, now: float, how: str) -> bool:
        if self.receiver is not None and self.max_range_nm > 0:
            if distance_nm(self.receiver[0], self.receiver[1], lat, lon) > self.max_range_nm:
                return False
        if not ac.has_position() or ac.t_pos is None or not ac.pos_reliable and how == "global":
            return True
        if distance_nm(ac.lat, ac.lon, lat, lon) <= self._limit_nm(ac, now - ac.t_pos):
            ac.pending = None
            return True
        if how != "global":
            return False
        # a run of mutually consistent global decodes that disagree with the old position wins
        pend = ac.pending
        if pend is not None and distance_nm(pend[0], pend[1], lat, lon) <= self._limit_nm(ac, now - pend[2]):
            count = pend[3] + 1
        else:
            count = 1
        if count >= CONFIRM_BAD_POSITIONS:
            ac.pending = None
            return True
        ac.pending = (lat, lon, now, count)
        return False

    def _set_position(self, ac: Aircraft, lat: float, lon: float, now: float, how: str) -> None:
        ac.pending = None
        previous_reliable = ac.pos_reliable
        ac.lat, ac.lon, ac.t_pos = lat, lon, now
        ac.pos_reliable = how == "global" or (how == "local" and previous_reliable)
        self.positions += 1
        if self.receiver is not None:
            dist = distance_nm(self.receiver[0], self.receiver[1], lat, lon)
            if dist > self.max_distance_nm:
                self.max_distance_nm = dist
        trail = ac.trail
        if trail:
            last = trail[-1]
            step = abs(lat - last[1]) * 60.0 + abs(lon - last[2]) * 60.0 * math.cos(math.radians(lat))
            if step < TRAIL_MIN_STEP_NM and now - last[0] < TRAIL_MAX_GAP_S:
                return
        trail.append((now, lat, lon, ac.alt_baro))
