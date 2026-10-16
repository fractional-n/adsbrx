import asyncio

import aiohttp
import pytest

from adsbrx import cpr, modes
from adsbrx.outputs import web
from adsbrx.outputs.web import WebServer, aircraft_document, aircraft_to_dict
from adsbrx.sim.encoder import (
    build_df11,
    build_df17,
    encode_airborne_position,
    encode_ident,
    encode_status,
    encode_velocity,
)
from adsbrx.tracker import Tracker

RECEIVER = (35.5494, 139.7798)
T0 = 1_700_000_000.0
FLYING = 0x840ABC
GHOST = 0x777777  # heard only once: not shown
PARKED = 0x840D77


def pos_msg(icao, lat, lon, odd, t, alt=12000):
    la, lo = cpr.encode(lat, lon, odd)
    return modes.decode(build_df17(icao, encode_airborne_position(la, lo, odd, alt)), ts=t, rssi=-18.0)


def populated_tracker() -> Tracker:
    tracker = Tracker(receiver=RECEIVER)
    tracker.update(modes.decode(build_df17(FLYING, encode_ident("JAL515", 3)), ts=T0, rssi=-18.0))
    for step in range(3):  # a few position fixes, so that there is a trail
        lat, lon, t = 35.80 + 0.005 * step, 139.90, T0 + 5 * step
        tracker.update(pos_msg(FLYING, lat, lon, False, t))
        tracker.update(pos_msg(FLYING, lat, lon, True, t + 0.5))
    tracker.update(modes.decode(build_df17(FLYING, encode_velocity(230, 45, -1500)), ts=T0 + 11, rssi=-18.0))
    tracker.update(modes.decode(build_df11(GHOST), ts=T0 + 11, rssi=-30.0))
    on_ground = modes.decode(build_df11(PARKED, ca=4), ts=T0 + 11, rssi=-25.0)
    tracker.update(on_ground)
    tracker.update(on_ground)
    return tracker


def serve(tracker, test, **kw):
    """Runs `test(base_url, server, session)` against a live server on a free port."""

    async def main():
        server = WebServer(tracker, "127.0.0.1", 0, **kw)
        await server.start()
        try:
            async with aiohttp.ClientSession() as session:
                return await test(f"http://127.0.0.1:{server.port}", server, session)
        finally:
            await server.close()

    return asyncio.run(main())


def test_index_and_static_files_are_served():
    async def check(base, server, session):
        for path, kind in (
            ("/", "text/html"),
            ("/static/app.js", "javascript"),
            ("/static/i18n.js", "javascript"),
            ("/static/style.css", "text/css"),
            ("/static/vendor/leaflet.js", "javascript"),
            ("/static/vendor/leaflet.css", "text/css"),
            ("/static/vendor/LEAFLET-LICENSE.txt", "text/plain"),
        ):
            async with session.get(base + path) as resp:
                assert resp.status == 200, path
                assert kind in resp.headers["Content-Type"], path
                assert len(await resp.read()) > 100, path
        async with session.get(base + "/") as resp:
            html = await resp.text()
        assert "static/app.js" in html and "static/vendor/leaflet.js" in html and "static/i18n.js" in html

    serve(Tracker(), check)


def test_static_files_are_revalidated_and_not_stored_stale():
    async def check(base, server, session):
        async with session.get(base + "/static/app.js") as resp:
            assert resp.headers["Cache-Control"] == "no-cache"
            etag = resp.headers.get("ETag")
        assert etag
        async with session.get(base + "/static/app.js", headers={"If-None-Match": etag}) as resp:
            assert resp.status == 304
        async with session.get(base + "/data/aircraft.json") as resp:
            assert resp.headers["Cache-Control"] == "no-store"

    serve(Tracker(), check)


def test_static_files_cannot_escape_their_folder():
    async def check(base, server, session):
        for path in ("/static/../outputs/web.py", "/static/%2e%2e/outputs/web.py", "/static/..%5coutputs%5cweb.py", "/static/nope.js"):
            async with session.get(base + path) as resp:
                assert resp.status in (403, 404), path
                assert "WebServer" not in await resp.text(), path

    serve(Tracker(), check)


def test_aircraft_json_follows_the_dump1090_schema():
    tracker = populated_tracker()

    async def check(base, server, session):
        async with session.get(base + "/data/aircraft.json") as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("application/json")
            assert resp.headers["Cache-Control"] == "no-store"
            assert "Access-Control-Allow-Origin" not in resp.headers
            doc = await resp.json()
        assert doc["now"] == pytest.approx(T0 + 11, abs=0.1) and doc["messages"] == tracker.total_messages
        by_hex = {a["hex"]: a for a in doc["aircraft"]}
        assert set(by_hex) == {"840abc", "840d77"}  # the aircraft heard once is hidden
        jal = by_hex["840abc"]
        assert jal["type"] == "adsb_icao" and jal["flight"] == "JAL515" and jal["category"] == "A3"
        assert jal["alt_baro"] == 12000 and jal["emergency"] == "none"
        assert jal["lat"] == pytest.approx(35.81, abs=1e-3) and jal["lon"] == pytest.approx(139.90, abs=1e-3)
        assert jal["gs"] == pytest.approx(230, abs=1.5) and jal["track"] == pytest.approx(45, abs=1.5)
        assert jal["geom_rate"] == -1472  # 64 fpm steps
        assert 15 < jal["r_dst"] < 25 and 0 <= jal["r_dir"] < 360
        assert jal["messages"] == 8 and jal["seen"] == 0.0 and jal["seen_pos"] == pytest.approx(0.5, abs=0.6)
        assert jal["rssi"] == pytest.approx(-18.0, abs=0.5)
        parked = by_hex["840d77"]
        assert parked["alt_baro"] == "ground" and parked["type"] == "mode_s" and "lat" not in parked

    serve(tracker, check)


def test_emergency_squawk_is_reported():
    tracker = Tracker()
    for t in (0.0, 1.0):
        tracker.update(modes.decode(build_df17(FLYING, encode_status("7700", 1)), ts=T0 + t, rssi=-20.0))
    (entry,) = aircraft_document(tracker)["aircraft"]
    assert entry["emergency"] == "general" and entry["squawk"] == "7700"


def test_aircraft_to_dict_omits_unknown_fields():
    tracker = Tracker()
    tracker.update(modes.decode(build_df11(0x123456), ts=T0))
    ac = tracker.aircraft[0x123456]
    entry = aircraft_to_dict(ac, T0 + 3, tracker)
    assert entry == {"hex": "123456", "type": "mode_s", "emergency": "none", "messages": 1, "seen": 3.0, "rssi": entry["rssi"]}


def test_receiver_stats_and_trails_documents():
    tracker = populated_tracker()

    async def check(base, server, session):
        async with session.get(base + "/data/receiver.json") as resp:
            receiver = await resp.json()
        assert receiver["lat"] == RECEIVER[0] and receiver["lon"] == RECEIVER[1] and "adsbrx" in receiver["version"]

        async with session.get(base + "/data/stats.json") as resp:
            assert await resp.json() == {"messages": 7, "message_rate": 1.5}

        async with session.get(base + "/data/trails.json") as resp:
            trails = (await resp.json())["trails"]
        assert set(trails) == {"840abc"}  # only aircraft with at least two trail points
        points = trails["840abc"]
        assert len(points) >= 2 and all(len(p) == 4 for p in points)
        assert points[0][0] < points[-1][0] and points[0][3] == 12000

    serve(tracker, check, stats=lambda: {"messages": 7, "message_rate": 1.5})


def test_receiver_document_without_a_position():
    async def check(base, server, session):
        async with session.get(base + "/data/receiver.json") as resp:
            receiver = await resp.json()
        assert "lat" not in receiver and "lon" not in receiver
        async with session.get(base + "/data/stats.json") as resp:
            assert await resp.json() == {}
        async with session.get(base + "/data/levels.json") as resp:
            assert await resp.json() == {}

    serve(Tracker(), check)


def test_levels_document_is_served_with_what_the_viewer_already_has():
    asked = []

    def levels(since):
        asked.append(since)
        return {"noise": -21.5, "history": []}

    async def check(base, server, session):
        async with session.get(base + "/data/levels.json") as resp:
            assert resp.status == 200
            assert resp.headers["Cache-Control"] == "no-store"
            assert await resp.json() == {"noise": -21.5, "history": []}
        for query in ("?since=1700000012", "?since=-3.5", "?since=1.7e9"):
            async with session.get(base + "/data/levels.json" + query) as resp:
                assert resp.status == 200, query
        assert asked == [None, 1700000012.0, -3.5, 1.7e9]

    serve(Tracker(), check, levels=levels)


@pytest.mark.parametrize("since", ["abc", "", "nan", "inf", "-inf", "1e999"])
def test_levels_since_must_be_a_number(since):
    asked = []

    async def check(base, server, session):
        async with session.get(base + "/data/levels.json", params={"since": since}) as resp:
            assert resp.status == 400
            assert "since" in await resp.text()
        assert asked == []

    serve(Tracker(), check, levels=lambda s: asked.append(s) or {})


def test_viewers_are_counted_and_forgotten(monkeypatch):
    monkeypatch.setattr(web, "VIEWER_WINDOW_S", 0.3)

    async def check(base, server, session):
        assert server.viewers == 0
        async with session.get(base + "/data/aircraft.json"):
            pass
        async with session.get(base + "/data/aircraft.json"):
            pass
        assert server.viewers == 1  # the same peer
        await asyncio.sleep(0.5)
        assert server.viewers == 0

    serve(Tracker(), check)


def test_port_in_use_is_reported_clearly():
    async def main():
        first = WebServer(Tracker(), "127.0.0.1", 0)
        await first.start()
        try:
            second = WebServer(Tracker(), "127.0.0.1", first.port)
            with pytest.raises(OSError, match=r"cannot listen for HTTP on 127\.0\.0\.1:\d+.*another program"):
                await second.start()
            await second.close()
        finally:
            await first.close()

    asyncio.run(main())
