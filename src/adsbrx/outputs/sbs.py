"""SBS-1 (BaseStation) CSV lines, as served on port 30003 by dump1090 / readsb.

Understood by Virtual Radar Server, PlanePlotter, tar1090/readsb (`sbs_in`) and most other
ADS-B software.  Unknown values are left empty; booleans are `-1` (true) / `0` (false).
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..modes import (
    AIR_AIR,
    AIRBORNE_POS,
    ALL_CALL,
    IDENT,
    STATUS,
    SURFACE_POS,
    SURV_ALT,
    SURV_ID,
    VELOCITY,
    ModeSMessage,
)
from ..tracker import Update

MAX_IDS = 50_000


def _flag(value: bool | None) -> str:
    if value is None:
        return ""
    return "-1" if value else "0"


def _number(value: float | None) -> str:
    return "" if value is None else str(int(round(value)))


def _angle(value: float | None) -> str:
    return "" if value is None else str(int(round(value)) % 360)


def _stamp(ts: float) -> tuple[str, str]:
    moment = datetime.fromtimestamp(ts, tz=timezone.utc)
    return moment.strftime("%Y/%m/%d"), moment.strftime("%H:%M:%S.") + f"{moment.microsecond // 1000:03d}"


class SbsFormatter:
    """Turns decoded messages into `MSG,<type>,...` lines."""

    def __init__(self, session_id: int = 1):
        self.session_id = session_id
        self._ids: dict[int, int] = {}

    def _aircraft_id(self, icao: int) -> int:
        number = self._ids.get(icao)
        if number is None:
            if len(self._ids) >= MAX_IDS:
                self._ids.clear()
            number = self._ids[icao] = len(self._ids) + 1
        return number

    def format(self, msg: ModeSMessage, update: Update) -> str | None:
        """One line (with trailing newline), or None when the message has no SBS equivalent."""
        ac = update.aircraft
        if ac is None:
            return None

        callsign = altitude = speed = track = lat = lon = vrate = squawk = ""
        alert = emergency = spi = ground = ""
        kind = msg.kind

        if kind == IDENT:
            number = 1
            callsign = msg.callsign or ""
        elif kind == SURFACE_POS:
            number = 2
            speed, track = _number(msg.gs), _angle(msg.track)
            ground = "-1"
        elif kind == AIRBORNE_POS:
            number = 3
            altitude = _number(msg.altitude)
            alert, spi = _flag(msg.alert), _flag(msg.spi)
            emergency = _flag(ac.emergency_state is not None)
            ground = "0"
        elif kind == VELOCITY:
            if msg.gs is None and msg.vrate is None:
                return None
            number = 4
            speed = _number(msg.gs)
            track = _angle(msg.track) if msg.gs is not None else ""
            vrate = _number(msg.vrate)
        elif kind == SURV_ALT:
            number = 5
            altitude = _number(msg.altitude)
            alert, spi = _flag(msg.alert), _flag(msg.spi)
            emergency = _flag(ac.emergency_state is not None)
            ground = _flag(msg.on_ground)
        elif kind == SURV_ID:
            number = 6
            squawk = msg.squawk or ""
            alert, spi = _flag(msg.alert), _flag(msg.spi)
            emergency = _flag(ac.emergency_state is not None)
            ground = _flag(msg.on_ground)
        elif kind == STATUS:
            number = 6
            squawk = msg.squawk or ""
            alert, spi = "0", "0"
            emergency = _flag(ac.emergency_state is not None)
        elif kind == AIR_AIR:
            number = 7
            altitude = _number(msg.altitude)
            ground = _flag(msg.on_ground)
        elif kind == ALL_CALL:
            number = 8
            ground = _flag(msg.on_ground)
        else:
            return None

        if update.position_updated and ac.lat is not None and ac.lon is not None:
            lat, lon = f"{ac.lat:.5f}", f"{ac.lon:.5f}"

        date, clock = _stamp(msg.ts)
        ident = self._aircraft_id(msg.icao)
        return (
            f"MSG,{number},{self.session_id},{ident},{msg.icao:06X},{ident},"
            f"{date},{clock},{date},{clock},"
            f"{callsign},{altitude},{speed},{track},{lat},{lon},{vrate},{squawk},"
            f"{alert},{emergency},{spi},{ground}\n"
        )
