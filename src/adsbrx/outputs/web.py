"""HTTP server: dump1090-compatible JSON (`/data/aircraft.json`) and the built-in map UI."""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable
from pathlib import Path

from aiohttp import web

from .. import __version__
from ..tracker import Aircraft, Tracker

log = logging.getLogger("adsbrx")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
VIEWER_WINDOW_S = 10.0
_TYPES = {"adsb": "adsb_icao", "tisb": "tisb_icao", "mode_s": "mode_s"}


def aircraft_to_dict(ac: Aircraft, now: float, tracker: Tracker) -> dict:
    """One aircraft in the dump1090-fa / readsb `aircraft.json` schema."""
    out: dict = {"hex": ac.hex.lower(), "type": _TYPES.get(ac.source, "mode_s")}
    if ac.callsign:
        out["flight"] = ac.callsign
    if ac.on_ground:
        out["alt_baro"] = "ground"
    elif ac.alt_baro is not None:
        out["alt_baro"] = ac.alt_baro
    if ac.alt_geom is not None:
        out["alt_geom"] = ac.alt_geom
    if ac.gs is not None:
        out["gs"] = round(ac.gs, 1)
    if ac.track is not None:
        out["track"] = round(ac.track, 1)
    if ac.airspeed is not None:
        out["tas" if ac.airspeed_type == "TAS" else "ias"] = ac.airspeed
    if ac.heading is not None:
        out["mag_heading"] = round(ac.heading, 1)
    if ac.vrate is not None:
        out["geom_rate" if ac.vrate_source == "geom" else "baro_rate"] = ac.vrate
    if ac.squawk:
        out["squawk"] = ac.squawk
    out["emergency"] = ac.emergency_state or "none"
    if ac.category:
        out["category"] = ac.category
    if ac.alert:
        out["alert"] = True
    if ac.spi:
        out["spi"] = True
    if ac.has_position():
        out["lat"] = round(ac.lat, 6)
        out["lon"] = round(ac.lon, 6)
        out["seen_pos"] = round(max(0.0, now - ac.t_pos), 1) if ac.t_pos is not None else 0.0
        located = tracker.distance_bearing(ac)
        if located is not None:
            out["r_dst"] = round(located[0], 1)
            out["r_dir"] = round(located[1], 1)
    out["messages"] = ac.messages
    out["seen"] = round(max(0.0, now - ac.last_seen), 1)
    rssi = ac.rssi
    if rssi is not None:
        out["rssi"] = round(rssi, 1)
    return out


def aircraft_document(tracker: Tracker) -> dict:
    now = tracker.now
    return {
        "now": round(now, 1),
        "messages": tracker.total_messages,
        "aircraft": [aircraft_to_dict(ac, now, tracker) for ac in tracker.visible()],
    }


def receiver_document(tracker: Tracker) -> dict:
    out: dict = {"version": f"adsbrx {__version__}", "refresh": 1000, "history": 0}
    if tracker.receiver is not None:
        out["lat"], out["lon"] = tracker.receiver
    return out


def trails_document(tracker: Tracker) -> dict:
    trails = {}
    for ac in tracker.visible():
        if len(ac.trail) >= 2:
            trails[ac.hex.lower()] = [[round(t, 1), round(lat, 5), round(lon, 5), alt] for t, lat, lon, alt in ac.trail]
    return {"now": round(tracker.now, 1), "trails": trails}


def _json(data: dict) -> web.Response:
    return web.Response(
        text=json.dumps(data, separators=(",", ":"), allow_nan=False),
        content_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


async def _revalidate_static(request: web.Request, response: web.StreamResponse) -> None:
    # Without this a browser may keep using an old app.js/style.css after an upgrade.
    if request.path.startswith("/static/") and "Cache-Control" not in response.headers:
        response.headers["Cache-Control"] = "no-cache"


class WebServer:
    def __init__(
        self,
        tracker: Tracker,
        host: str,
        port: int,
        stats: Callable[[], dict] | None = None,
        levels: Callable[[float | None], dict] | None = None,
    ):
        self.tracker = tracker
        self.host = host
        self.port = port
        self._stats = stats or (lambda: {})
        self._levels = levels or (lambda since: {})
        self._viewers: dict[str | None, float] = {}
        self._runner: web.AppRunner | None = None

    @property
    def viewers(self) -> int:
        """Distinct clients that polled the aircraft list recently."""
        cutoff = time.monotonic() - VIEWER_WINDOW_S
        self._viewers = {peer: seen for peer, seen in self._viewers.items() if seen >= cutoff}
        return len(self._viewers)

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/data/aircraft.json", self._aircraft)
        app.router.add_get("/data/receiver.json", self._receiver)
        app.router.add_get("/data/stats.json", self._stats_json)
        app.router.add_get("/data/levels.json", self._levels_json)
        app.router.add_get("/data/trails.json", self._trails)
        app.router.add_static("/static/", STATIC_DIR, show_index=False, follow_symlinks=False)
        app.on_response_prepare.append(_revalidate_static)
        return app

    async def start(self) -> None:
        self._runner = web.AppRunner(self.build_app(), access_log=None)
        await self._runner.setup()
        try:
            await web.TCPSite(self._runner, self.host, self.port).start()
        except OSError as exc:
            await self._runner.cleanup()
            self._runner = None
            raise OSError(
                f"cannot listen for HTTP on {self.host}:{self.port}: {exc.strerror or exc} "
                f"(is another program using this port?)"
            ) from exc
        self.port = self._runner.addresses[0][1]
        log.info("web interface listening on http://%s:%d/", self.host, self.port)

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _index(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    async def _aircraft(self, request: web.Request) -> web.Response:
        self._viewers[request.remote] = time.monotonic()
        return _json(aircraft_document(self.tracker))

    async def _receiver(self, request: web.Request) -> web.Response:
        return _json(receiver_document(self.tracker))

    async def _stats_json(self, request: web.Request) -> web.Response:
        return _json(self._stats())

    async def _levels_json(self, request: web.Request) -> web.Response:
        since = None
        if "since" in request.query:
            try:
                since = float(request.query["since"])
            except ValueError:
                since = math.nan
            if not math.isfinite(since):
                raise web.HTTPBadRequest(text="since must be a number")
        return _json(self._levels(since))

    async def _trails(self, request: web.Request) -> web.Response:
        return _json(trails_document(self.tracker))
