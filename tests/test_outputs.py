import asyncio
import io

import pytest

from adsbrx import modes
from adsbrx.outputs import console
from adsbrx.outputs.beast import ESCAPE, encode_beast, encode_raw, signal_byte
from adsbrx.outputs.netserver import BroadcastServer
from adsbrx.outputs.sbs import SbsFormatter
from adsbrx.tracker import Tracker

T0 = 1_700_000_000.0
IDENT = "8D4840D6202CC371C32CE0576098"  # KLM1023
EVEN = "8D40621D58C382D690C8AC2863A7"  # 38000 ft, even CPR
ODD = "8D40621D58C386435CC412692AD6"  # 38000 ft, odd CPR
VELOCITY = "8D485020994409940838175B284F"
RECEIVER = (52.3, 4.0)


def msg(hexstr, ts=T0, sample=0, rssi=-20.0):
    out = modes.decode(bytes.fromhex(hexstr), ts=ts, sample=sample, rssi=rssi)
    assert out is not None
    return out


def fields(line):
    assert line.endswith("\n")
    parts = line[:-1].split(",")
    assert len(parts) == 22
    return parts


def feed(tracker, formatter, *messages):
    lines = []
    for m in messages:
        update = tracker.update(m)
        lines.append(formatter.format(m, update))
    return lines


# ---------------------------------------------------------------------------------------- SBS


def test_sbs_identification():
    tracker, sbs = Tracker(receiver=RECEIVER), SbsFormatter()
    (line,) = feed(tracker, sbs, msg(IDENT, ts=T0 + 0.123))
    f = fields(line)
    assert f[0:5] == ["MSG", "1", "1", "1", "4840D6"]
    assert f[5] == "1"
    assert f[6:10] == ["2023/11/14", "22:13:20.123", "2023/11/14", "22:13:20.123"]
    assert f[10] == "KLM1023"
    assert all(v == "" for v in f[11:18])


def test_sbs_position_only_has_coordinates_once_decoded():
    tracker, sbs = Tracker(), SbsFormatter()
    first, second = feed(tracker, sbs, msg(EVEN, ts=T0), msg(ODD, ts=T0 + 1))
    f1, f2 = fields(first), fields(second)
    assert f1[1] == f2[1] == "3"
    assert f1[11] == f2[11] == "38000"
    assert f1[14] == f1[15] == ""  # no receiver position: the even/odd pair is needed
    assert float(f2[14]) == pytest.approx(52.26578, abs=1e-4)  # the odd frame is the newer one
    assert float(f2[15]) == pytest.approx(3.93891, abs=1e-4)
    assert f2[18:22] == ["0", "0", "0", "0"]


def test_sbs_velocity_and_other_types():
    tracker, sbs = Tracker(), SbsFormatter()
    (line,) = feed(tracker, sbs, msg(VELOCITY))
    f = fields(line)
    assert f[1] == "4"
    assert f[12] == "159" and f[13] == "183" and f[16] == "-832"
    assert f[11] == f[14] == f[15] == ""


def test_sbs_surveillance_messages():
    tracker, sbs = Tracker(), SbsFormatter()
    all_call = msg("5D4840D6000000")  # DF11, CA=5 (airborne); the CRC is irrelevant for formatting
    altitude = modes.ModeSMessage(raw=bytes(7), df=4, icao=0x4840D6, kind=modes.SURV_ALT, ts=T0, altitude=12000, on_ground=False)
    squawk = modes.ModeSMessage(raw=bytes(7), df=5, icao=0x4840D6, kind=modes.SURV_ID, ts=T0, squawk="7700", alert=True, on_ground=False)
    air_air = modes.ModeSMessage(raw=bytes(7), df=0, icao=0x4840D6, kind=modes.AIR_AIR, ts=T0, altitude=900, on_ground=True)
    lines = [fields(x) for x in feed(tracker, sbs, all_call, altitude, squawk, air_air)]
    assert [x[1] for x in lines] == ["8", "5", "6", "7"]
    assert lines[0][21] == "0"
    assert lines[1][11] == "12000" and lines[1][21] == "0"
    assert lines[2][17] == "7700" and lines[2][18] == "-1"
    assert lines[3][11] == "900" and lines[3][21] == "-1"


def test_sbs_emergency_flag_follows_the_squawk():
    tracker, sbs = Tracker(), SbsFormatter()
    squawk = modes.ModeSMessage(raw=bytes(7), df=5, icao=0x123456, kind=modes.SURV_ID, ts=T0, squawk="7700")
    (line,) = feed(tracker, sbs, squawk)
    assert fields(line)[19] == "-1"
    calm = modes.ModeSMessage(raw=bytes(7), df=5, icao=0x654321, kind=modes.SURV_ID, ts=T0, squawk="1200")
    (line,) = feed(tracker, sbs, calm)
    assert fields(line)[19] == "0"


def test_sbs_surface_position_and_aircraft_ids():
    tracker, sbs = Tracker(), SbsFormatter()
    surface = modes.ModeSMessage(raw=bytes(14), df=17, icao=0xABCDEF, kind=modes.SURFACE_POS, ts=T0, gs=12.4, track=91.0, on_ground=True)
    other = modes.ModeSMessage(raw=bytes(14), df=17, icao=0x111111, kind=modes.IDENT, ts=T0, callsign="TEST1")
    a, b, c = (fields(x) for x in feed(tracker, sbs, surface, other, surface))
    assert a[1] == "2" and a[12] == "12" and a[13] == "91" and a[21] == "-1"
    assert (a[3], b[3], c[3]) == ("1", "2", "1") and a[5] == a[3]


def test_sbs_ignores_messages_without_an_equivalent():
    tracker, sbs = Tracker(), SbsFormatter()
    nothing = modes.ModeSMessage(raw=bytes(14), df=17, icao=1, kind=modes.OTHER, ts=T0)
    no_speed = modes.ModeSMessage(raw=bytes(14), df=17, icao=1, kind=modes.VELOCITY, ts=T0, heading=10.0)
    assert feed(tracker, sbs, nothing, no_speed) == [None, None]
    assert sbs.format(nothing, tracker.update(modes.ModeSMessage(raw=bytes(14), df=18, icao=2, kind=modes.OTHER, ts=T0))) is None


def test_sbs_status_message_carries_squawk():
    tracker, sbs = Tracker(), SbsFormatter()
    status = modes.ModeSMessage(raw=bytes(14), df=17, icao=5, kind=modes.STATUS, ts=T0, squawk="7500", emergency="unlawful")
    (line,) = feed(tracker, sbs, status)
    f = fields(line)
    assert f[1] == "6" and f[17] == "7500" and f[19] == "-1"


# -------------------------------------------------------------------------- Beast / AVR raw


def parse_beast(stream: bytes):
    """Minimal Beast parser (mirrors what readsb does): [(type, timestamp, signal, frame)]."""
    out, i = [], 0
    sizes = {0x31: 2, 0x32: 7, 0x33: 14}
    while i < len(stream):
        assert stream[i] == ESCAPE
        kind = stream[i + 1]
        i += 2
        body = bytearray()
        need = 7 + sizes[kind]
        while len(body) < need:
            byte = stream[i]
            i += 1
            if byte == ESCAPE:
                assert stream[i] == ESCAPE
                i += 1
            body.append(byte)
        out.append((kind, int.from_bytes(body[:6], "big"), body[6], bytes(body[7:])))
    return out


def test_beast_long_frame_layout():
    m = msg(IDENT, sample=1_000_000, rssi=-20.0)
    data = encode_beast(m)
    assert data[:2] == b"\x1a3"
    ((kind, stamp, signal, frame),) = parse_beast(data)
    assert kind == 0x33 and frame == m.raw
    assert stamp == 6_000_000
    assert signal == 26  # 10 ** (-20 / 20) * 255


def test_beast_short_frame_and_escaping():
    short = modes.ModeSMessage(raw=bytes.fromhex("5D4840D6001A1A"), df=11, icao=0x4840D6, sample=0x1A1A1A // 6, rssi=0.0)
    data = encode_beast(short)
    assert data[:2] == b"\x1a2"
    ((kind, stamp, signal, frame),) = parse_beast(data)
    assert kind == 0x32 and frame == short.raw and stamp == 0x1A1A1A // 6 * 6 and signal == 255
    assert data.count(b"\x1a\x1a") >= 2  # the escape byte is doubled everywhere in the body


def test_beast_stream_of_several_frames():
    frames = [msg(IDENT, sample=10), msg(EVEN, sample=300), msg(VELOCITY, sample=7000)]
    parsed = parse_beast(b"".join(encode_beast(m) for m in frames))
    assert [p[3] for p in parsed] == [m.raw for m in frames]
    assert [p[1] for p in parsed] == [60, 1800, 42000]


def test_beast_timestamp_wraps_at_48_bits():
    m = modes.ModeSMessage(raw=bytes(14), df=17, icao=1, sample=(1 << 48) // 6 + 5)
    ((_, stamp, _, _),) = parse_beast(encode_beast(m))
    assert 0 <= stamp < 1 << 48 and stamp == ((1 << 48) // 6 + 5) * 6 % (1 << 48)


def test_signal_byte_limits():
    assert signal_byte(0.0) == 255 and signal_byte(10.0) == 255
    assert signal_byte(-99.0) == 0
    assert signal_byte(-6.0) == 128


def test_avr_raw():
    assert encode_raw(msg(IDENT)) == b"*8D4840D6202CC371C32CE0576098;\n"


# --------------------------------------------------------------------------------- servers


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


async def connect(server):
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    for _ in range(100):  # wait until the server registered the client
        if server.clients:
            break
        await asyncio.sleep(0.01)
    return reader, writer


def test_broadcast_reaches_every_client():
    async def main():
        server = BroadcastServer("test", "127.0.0.1", 0)
        await server.start()
        assert server.port != 0 and server.clients == 0
        server.broadcast(b"nobody listens\n")  # no clients: harmless
        r1, w1 = await connect(server)
        r2, w2 = await connect(server)
        for _ in range(100):
            if server.clients == 2:
                break
            await asyncio.sleep(0.01)
        assert server.clients == 2
        server.broadcast(b"hello\n")
        assert await asyncio.wait_for(r1.readline(), 5) == b"hello\n"
        assert await asyncio.wait_for(r2.readline(), 5) == b"hello\n"
        w1.close()
        await w1.wait_closed()
        for _ in range(200):
            if server.clients == 1:
                break
            await asyncio.sleep(0.01)
        assert server.clients == 1
        server.broadcast(b"again\n")
        assert await asyncio.wait_for(r2.readline(), 5) == b"again\n"
        await server.close()
        assert await asyncio.wait_for(r2.read(), 5) == b""  # closed by the server
        w2.close()

    run(main())


def test_clients_may_send_data_which_is_ignored():
    async def main():
        server = BroadcastServer("test", "127.0.0.1", 0)
        await server.start()
        reader, writer = await connect(server)
        writer.write(b"\x1a1C\x1a1d" * 100)
        await writer.drain()
        server.broadcast(b"still fine\n")
        assert await asyncio.wait_for(reader.readline(), 5) == b"still fine\n"
        writer.close()
        await server.close()

    run(main())


def test_slow_client_is_disconnected_but_others_continue():
    async def main():
        server = BroadcastServer("test", "127.0.0.1", 0, max_buffer=200_000)
        await server.start()
        slow_reader, slow_writer = await connect(server)
        fast_reader, fast_writer = await connect(server)
        for _ in range(100):
            if server.clients == 2:
                break
            await asyncio.sleep(0.01)
        chunk = b"x" * 60_000 + b"\n"
        received = 0

        async def drain():
            nonlocal received
            while True:
                data = await fast_reader.read(1 << 20)
                if not data:
                    return
                received += len(data)

        task = asyncio.create_task(drain())
        for _ in range(400):  # the slow client never reads
            server.broadcast(chunk)
            await asyncio.sleep(0.002)
            if server.dropped_clients:
                break
        assert server.dropped_clients == 1 and server.clients == 1
        server.broadcast(b"end\n")
        await asyncio.sleep(0.2)
        assert received > 0
        await server.close()
        await asyncio.wait_for(task, 5)
        slow_writer.close()
        fast_writer.close()

    run(main())


def test_port_in_use_gives_a_helpful_error():
    async def main():
        first = BroadcastServer("first", "127.0.0.1", 0)
        await first.start()
        second = BroadcastServer("SBS", "127.0.0.1", first.port)
        with pytest.raises(OSError, match=r"cannot listen for SBS on 127\.0\.0\.1"):
            await second.start()
        await first.close()

    run(main())


# ----------------------------------------------------------------------------------- console


def make_tracker():
    tracker = Tracker(receiver=RECEIVER)
    for m in (msg(IDENT, ts=T0), msg(IDENT, ts=T0 + 1), msg(EVEN, ts=T0 + 2), msg(ODD, ts=T0 + 3), msg(VELOCITY, ts=T0 + 4)):
        tracker.update(m)
    return tracker


def test_aircraft_rows_content_and_order():
    tracker = make_tracker()
    ghost = modes.ModeSMessage(raw=bytes(7), df=11, icao=0x777777, kind=modes.ALL_CALL, ts=T0 + 5)
    tracker.update(ghost)
    tracker.update(ghost)
    rows = console.aircraft_rows(tracker)
    assert len(rows) == 3
    near = [r for r in rows if "40621D" in r][0]
    assert "38000" in near and "52.2" in near and "3.9" in near
    assert rows[0].startswith("  40621D")  # nearest (it has a position) first
    assert rows[-1].startswith("  777777")  # no position: last
    assert all(len(r) == len(console.HEADER) for r in rows)
    klm = [r for r in rows if "4840D6" in r][0]
    assert "KLM1023" in klm


def test_emergency_rows_are_marked():
    tracker = Tracker()
    squawk = modes.ModeSMessage(raw=bytes(7), df=5, icao=0x123456, kind=modes.SURV_ID, ts=T0, squawk="7700")
    tracker.update(squawk)
    tracker.update(squawk)
    (row,) = console.aircraft_rows(tracker)
    assert row.startswith("! 123456") and "7700" in row


def test_render_frame_fits_the_window():
    tracker = Tracker(receiver=RECEIVER)
    for i in range(50):
        m = modes.ModeSMessage(raw=bytes(7), df=11, icao=0x100000 + i, kind=modes.ALL_CALL, ts=T0)
        tracker.update(m)
        tracker.update(m)
    frame = console.render_frame(tracker, "status", width=80, height=20)
    lines = frame.split("\n")
    assert len(lines) <= 20 and all(len(x) <= 80 for x in lines)
    assert lines[0] == "status" and "more aircraft" in lines[-1]
    full = console.render_frame(tracker, "status", width=200, height=100)
    assert len(full.split("\n")) == 3 + 50


def test_summary_and_stats_text():
    tracker = make_tracker()
    line = console.format_summary(tracker, 123.4, "extra")
    assert "aircraft 2 (1 with position)" in line and "123 msg/s" in line and "extra" in line and "max range" in line
    text = console.format_stats({"samples": 4_000_000, "valid_crc": 10, "df17": 8, "df11": 2, "gaps": 1}, tracker, dropped_blocks=3)
    assert "4,000,000 (2.0 s)" in text and "DF17 8" in text and "1 / 3" in text and "Maximum range" in text


def test_stats_text_shows_what_the_chosen_demodulator_can_do():
    tracker = make_tracker()
    stats = {"samples": 4_000_000, "df17": 8, "df11": 2, "fixed": 3, "phase_corrected": 1, "soft_fixed": 2, "retimed": 5}
    classic = console.format_stats(stats, tracker)
    assert "phase-corrected" in classic and "soft-decision" not in classic and "runner-up" not in classic
    mlse = console.format_stats(stats, tracker, demod="mlse")
    assert "phase-corrected" not in mlse
    soft = [line for line in mlse.splitlines() if "soft-decision-corrected" in line]
    retimed = [line for line in mlse.splitlines() if "runner-up timing" in line]
    assert soft and soft[0].endswith(": 2") and retimed and retimed[0].endswith(": 5")
    assert len({line.index(":") for line in mlse.splitlines()}) == 1  # the columns stay aligned


class FakeTty(io.StringIO):
    def isatty(self):
        return True


def test_console_prints_summaries_when_not_a_terminal():
    tracker = make_tracker()
    out = io.StringIO()
    screen = console.Console(tracker, lambda: "dropped 0", rate=lambda: 42.0, table=True, out=out, summary_interval=0.0)
    assert screen.table is False
    screen.draw()
    screen.draw()
    lines = out.getvalue().splitlines()
    assert len(lines) == 2 and "dropped 0" in lines[0] and "42 msg/s" in lines[0]


def test_console_table_uses_ansi_on_terminals(monkeypatch):
    monkeypatch.setattr(console, "enable_ansi", lambda stream: True)
    tracker = make_tracker()
    out = FakeTty()
    screen = console.Console(tracker, lambda: "", rate=lambda: 0.0, table=True, out=out, interval=0.05)
    assert screen.table is True

    async def main():
        task = asyncio.create_task(screen.run())
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    text = out.getvalue()
    assert text.startswith(console.HIDE_CURSOR + console.CLEAR_SCREEN) and console.HOME in text
    assert "KLM1023" in text and text.rstrip("\n").endswith(console.SHOW_CURSOR)
