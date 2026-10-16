"""The application glue, end to end: sample source -> decoder -> tracker -> every output."""

import asyncio
import errno
import io
import json
import logging
import re
import socket
import time

import aiohttp
import pytest
from fakes import FakeRtlLib, SlowDisk

from adsbrx import sources
from adsbrx.app import App, AppError, Config, RateMeter, build_demodulator, build_source
from adsbrx.crc import syndrome
from adsbrx.demod import SAMPLE_RATE, Demodulator
from adsbrx.mlse import MlseDemodulator
from adsbrx.recorder import Recorder
from adsbrx.sim.scenario import HANEDA, SimStream, default_scenario
from adsbrx.sources import (
    BLOCK_SAMPLES,
    FileSource,
    RtlSdrSource,
    RtlTcpSource,
    SimSource,
    StdinSource,
)

NO_OUTPUTS = {"web_port": None, "sbs_port": None, "beast_port": None, "raw_port": None}
ANY_PORT = {"web_port": 0, "sbs_port": 0, "beast_port": 0, "raw_port": 0}
AVR_LINE = re.compile(r"\*(?:[0-9A-F]{14}|[0-9A-F]{28});")


def sim_config(**overrides) -> Config:
    """A deterministic, as-fast-as-possible simulator run (the default scenario is the same every time)."""
    settings = {"source": "sim", "fast": True, "duration": 10.0, **NO_OUTPUTS, **overrides}
    return Config(**settings)


def run_app(app: App, timeout: float = 60.0) -> int:
    async def main():
        return await asyncio.wait_for(app.run(), timeout)

    return asyncio.run(main())


async def until(predicate, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


def parse_beast(data: bytes) -> list[tuple[int, int, bytes]]:
    """(timestamp, signal, frame) of every Beast message; fails on any framing error."""
    frames, i = [], 0
    while i < len(data):
        assert data[i] == 0x1A, f"no frame start at byte {i}"
        length = {0x32: 7, 0x33: 14}[data[i + 1]]  # '2' short, '3' long
        body, i = bytearray(), i + 2
        while len(body) < 7 + length:
            if data[i] == 0x1A:
                i += 1
                assert data[i] == 0x1A, "unescaped 0x1A inside a frame"
            body.append(data[i])
            i += 1
        frames.append((int.from_bytes(body[:6], "big"), body[6], bytes(body[7:])))
    return frames


class ClosedPipe(io.StringIO):
    """Standard output whose reader has gone away (`adsbrx --raw | head`)."""

    def write(self, text):
        raise BrokenPipeError(32, "Broken pipe")


# -------------------------------------------------------------------------- RateMeter


def test_rate_meter_measures_over_a_sliding_window():
    meter = RateMeter(window_s=5.0)
    assert meter.update(0, now=0.0) == 0.0
    assert meter.update(100, now=1.0) == pytest.approx(100.0)
    assert meter.update(300, now=2.0) == pytest.approx(150.0)
    assert meter.update(300, now=10.0) == 0.0  # old samples dropped out of the window
    assert meter.update(400, now=11.0) == pytest.approx(100.0)
    assert meter.rate == pytest.approx(100.0)


def test_rate_meter_without_time_passing_is_zero():
    meter = RateMeter()
    assert meter.update(5, now=3.0) == 0.0
    assert meter.update(9, now=3.0) == 0.0


# -------------------------------------------------------------------------- build_source


def test_build_source_for_each_input_kind(tmp_path):
    dongle = build_source(Config(source="rtlsdr", device_index=2, gain=40.0, ppm=-3, agc=True, bias_tee=True, dll_dir="D"))
    assert isinstance(dongle, RtlSdrSource)
    assert (dongle.device_index, dongle.gain, dongle.ppm, dongle.agc, dongle.bias_tee, dongle.dll_dir) == (
        2, 40.0, -3, True, True, "D",
    )

    tcp = build_source(Config(source="rtl_tcp", host="192.0.2.7", port=4321))
    assert isinstance(tcp, RtlTcpSource)
    assert (tcp.host, tcp.port) == ("192.0.2.7", 4321)

    recording = tmp_path / "x.cs16"
    recording.write_bytes(b"")
    replay = build_source(Config(source="file", path=str(recording), fmt="cs16", loop=True, speed=2.0))
    assert isinstance(replay, FileSource)
    assert (replay.fmt, replay.loop, replay.realtime, replay.speed) == ("cs16", True, True, 2.0)
    assert build_source(Config(source="file", path=str(recording), fast=True)).realtime is False

    pipe = build_source(Config(source="stdin", fmt="cs8"))
    assert isinstance(pipe, StdinSource) and pipe.fmt == "cs8"


def test_build_source_simulator_uses_the_receiver_position():
    default = build_source(Config(source="sim"))
    assert isinstance(default, SimSource)
    assert default.scenario.receiver == HANEDA
    assert default.realtime is True

    moved = build_source(Config(source="sim", lat=51.5, lon=-0.12, fast=True, speed=3.0))
    assert moved.scenario.receiver == (51.5, -0.12)
    assert (moved.realtime, moved.speed) == (False, 3.0)


def test_build_source_rejects_bad_requests():
    with pytest.raises(AppError, match="--file is required"):
        build_source(Config(source="file"))
    with pytest.raises(AppError, match="unknown source: radio"):
        build_source(Config(source="radio"))
    with pytest.raises(sources.SourceError, match="cannot open"):
        build_source(Config(source="file", path="does-not-exist.cu8")).run(lambda *_: None)


# ------------------------------------------------------------------------ build_demodulator


def test_build_demodulator_defaults_to_the_sequence_detector():
    demod = build_demodulator(Config())
    assert isinstance(demod, MlseDemodulator)
    assert (demod.fix_bits, demod.all_df, demod.soft_window, demod.soft_flips) == (1, False, 12, 3)


def test_build_demodulator_builds_the_classic_one_on_request():
    demod = build_demodulator(Config(demod="classic", fix_bits=2, all_df=True))
    assert type(demod) is Demodulator
    assert (demod.fix_bits, demod.all_df) == (2, True)


@pytest.mark.parametrize("fix_bits, search", [(0, (12, 3)), (1, (12, 3)), (2, (14, 4))])
def test_build_demodulator_for_the_sequence_detector(fix_bits, search):
    demod = build_demodulator(Config(demod="mlse", fix_bits=fix_bits, all_df=True))
    assert isinstance(demod, MlseDemodulator)
    assert (demod.fix_bits, demod.all_df) == (fix_bits, True)
    assert (demod.soft_window, demod.soft_flips) == search  # --aggressive searches deeper


def test_build_demodulator_rejects_an_unknown_one():
    with pytest.raises(AppError, match="unknown demodulator: fancy"):
        build_demodulator(Config(demod="fancy"))


# ---------------------------------------------------------------- App: information and status


def test_app_takes_the_receiver_position_from_the_best_available_place():
    assert App(sim_config(), out=io.StringIO()).tracker.receiver == HANEDA
    assert App(sim_config(lat=10.0, lon=20.0), out=io.StringIO()).tracker.receiver == (10.0, 20.0)
    dongle = App(Config(source="rtlsdr"), source=RtlSdrSource(lib=FakeRtlLib()), out=io.StringIO())
    assert dongle.tracker.receiver is None


def test_app_applies_the_decoder_settings():
    app = App(sim_config(fix_bits=2, all_df=True, expire_s=15.0, max_range_nm=120.0), out=io.StringIO())
    assert (app.demod.fix_bits, app.demod.all_df) == (2, True)
    assert (app.tracker.expire_s, app.tracker.max_range_nm) == (15.0, 120.0)


def test_app_uses_the_chosen_demodulator():
    assert isinstance(App(sim_config(), out=io.StringIO()).demod, MlseDemodulator)  # the default
    assert type(App(sim_config(demod="classic"), out=io.StringIO()).demod) is Demodulator
    app = App(sim_config(demod="mlse", fix_bits=2), out=io.StringIO())
    assert isinstance(app.demod, MlseDemodulator) and app.demod.soft_flips == 4


def test_labels_describe_the_input():
    assert App(sim_config(), out=io.StringIO()).input_label == "built-in signal simulator"
    assert App(Config(source="rtl_tcp", host="h", port=7), out=io.StringIO()).input_label == "rtl_tcp at h:7"
    assert App(Config(source="rtlsdr", device_index=3), out=io.StringIO()).input_label == "RTL-SDR device #3"
    assert App(Config(source="file", path="a.cu8"), out=io.StringIO()).input_label == "file a.cu8"
    assert App(sim_config(), out=io.StringIO()).source_label == "sim"
    dongle = App(Config(source="rtlsdr"), source=RtlSdrSource(lib=FakeRtlLib()), out=io.StringIO())
    dongle.source.description = "Generic dongle (R820T), gain 49.6 dB"
    assert dongle.source_label == "rtlsdr: Generic dongle (R820T), gain 49.6 dB"


def test_stats_document_is_json_and_complete_before_the_first_message():
    document = App(sim_config(), out=io.StringIO()).stats_document()
    json.dumps(document)
    assert set(document) == {
        "version", "source", "uptime", "stream_time", "messages", "message_rate", "aircraft", "positions",
        "rejected_positions", "max_range_nm", "dropped_blocks", "demod", "levels", "outputs",
    }
    assert document["messages"] == 0 and document["aircraft"] == 0 and document["outputs"] == {}
    assert document["levels"] == {"window": 10, "noise": None, "signal": None, "peak": None, "snr": None, "messages": 0}


def test_status_text_lists_clients_and_dropped_blocks():
    app = App(sim_config(**ANY_PORT), out=io.StringIO())
    assert app._extra_status() == ""

    async def check():
        await app.start_outputs()
        try:
            assert app._extra_status() == "clients raw 0 sbs 0 beast 0 web 0"
            app.dropped_blocks = 3
            assert app._extra_status() == "dropped 3 blocks | clients raw 0 sbs 0 beast 0 web 0"
            assert set(app.stats_document()["outputs"]) == {"raw", "sbs", "beast", "web"}
            ports = {key: server.port for key, server in app.servers.items()}
            await app.start_outputs()  # starting twice changes nothing
            assert {key: server.port for key, server in app.servers.items()} == ports
        finally:
            await app.close_outputs()
        assert app.servers == {} and app.web is None

    asyncio.run(check())


def test_status_text_shows_the_size_of_the_recording(tmp_path):
    app = App(sim_config(), out=io.StringIO())
    app.recorder = Recorder(str(tmp_path / "rec.cu8"))  # not started: only its counters matter here
    app.recorder.bytes_written = 12_345_678
    assert app._extra_status() == "recording 12 MB"
    app.recorder.dropped_blocks = 2
    app.dropped_blocks = 3
    assert app._extra_status() == "dropped 3 blocks | recording 12 MB (2 blocks lost)"


# ------------------------------------------------------------------ App: the full data path


def test_simulated_traffic_reaches_every_output():
    out = io.StringIO()
    app = App(sim_config(hold=True, **ANY_PORT), out=out)  # hold: keep serving once the input has ended

    async def scenario():
        await app.start_outputs()
        ports = {key: server.port for key, server in app.servers.items()}
        streams = {key: await asyncio.open_connection("127.0.0.1", port) for key, port in ports.items()}
        await until(lambda: all(server.clients == 1 for server in app.servers.values()))

        task = asyncio.create_task(app.run())
        await until(lambda: app.pipeline is not None and app.pipeline.done.done())
        async with aiohttp.ClientSession() as session:
            base = f"http://127.0.0.1:{app.web.port}"
            aircraft = await (await session.get(base + "/data/aircraft.json")).json()
            stats = await (await session.get(base + "/data/stats.json")).json()
            levels = await (await session.get(base + "/data/levels.json")).json()
            last = levels["history"][-1][0]
            newer = await (await session.get(base + f"/data/levels.json?since={last}")).json()
        assert not task.done()  # --hold keeps the program alive

        task.cancel()  # what Ctrl-C does
        with pytest.raises(asyncio.CancelledError):
            await task
        received = {key: await reader.read() for key, (reader, _) in streams.items()}  # the servers closed: EOF
        for _, writer in streams.values():
            writer.close()
        return ports, aircraft, stats, levels, newer, received

    ports, aircraft, stats, levels, newer, received = asyncio.run(scenario())

    total = app.tracker.total_messages
    assert total > 300

    lines = received["raw"].decode("ascii").split()
    assert len(lines) == total
    assert all(AVR_LINE.fullmatch(line) for line in lines)
    frames = [bytes.fromhex(line[1:-1]) for line in lines]
    assert all(syndrome(frame) == 0 for frame in frames if frame[0] >> 3 in (11, 17, 18))

    beast = parse_beast(received["beast"])
    assert [frame for _, _, frame in beast] == frames
    stamps = [stamp for stamp, _, _ in beast]
    assert stamps == sorted(stamps) and stamps[-1] > stamps[0]

    sbs = [line.split(",") for line in received["sbs"].decode("ascii").splitlines()]
    assert len(sbs) == total
    assert all(len(fields) == 22 and fields[0] == "MSG" for fields in sbs)
    assert {"1", "3", "4", "8"} <= {fields[1] for fields in sbs}  # ident, position, velocity, all-call
    visible = {entry["hex"].upper() for entry in aircraft["aircraft"]}
    assert len(visible) >= 10 and visible <= {fields[4] for fields in sbs}
    assert sum("lat" in entry for entry in aircraft["aircraft"]) >= 10

    assert stats["messages"] == total
    assert stats["aircraft"] == len(visible)
    assert stats["max_range_nm"] > 50
    assert stats["demod"]["df17"] > 300
    assert stats["dropped_blocks"] == 0
    for key in ("raw", "sbs", "beast"):
        assert stats["outputs"][key] == {"port": ports[key], "clients": 1}

    assert -36 < levels["noise"] < -29  # the simulator's noise floor is about -32 dBFS
    assert 0 < levels["messages"] <= total
    assert levels["noise"] < levels["signal"] <= levels["peak"] < 0
    assert levels["snr"] == pytest.approx(levels["signal"] - levels["noise"], abs=0.11)
    assert {key: levels[key] for key in stats["levels"]} == stats["levels"]
    rows = levels["history"]
    assert len(rows) >= 8 and all(len(row) == 5 for row in rows)
    assert [row[0] for row in rows] == sorted({row[0] for row in rows})
    assert all(-36 < row[1] < -29 for row in rows)
    assert sum(row[4] for row in rows) > 0.8 * total
    assert newer["history"] == [] and newer["noise"] == levels["noise"]

    assert app.servers == {} and app.web is None  # cancelling shut everything down
    summary = out.getvalue().splitlines()[-1]  # ... and printed the closing summary
    assert re.search(r"aircraft 1\d \(1\d with position\)", summary), summary


def test_duration_ends_the_run_by_itself():
    app = App(sim_config(duration=3.0), out=io.StringIO())
    assert run_app(app) == 0
    streamed = app.demod_stats["samples"] / SAMPLE_RATE
    assert 3.0 <= streamed < 8.0
    assert app.tracker.total_messages > 50


def test_summary_and_statistics_are_printed_at_the_end():
    out = io.StringIO()
    app = App(sim_config(duration=3.0, stats=True), out=out)
    assert run_app(app) == 0
    text = out.getvalue()
    assert "with position" in text and "Valid frames" in text


@pytest.mark.parametrize("fix_bits", [0, 1, 2])
def test_the_sequence_detector_runs_the_whole_pipeline(fix_bits):
    out = io.StringIO()
    app = App(sim_config(demod="mlse", fix_bits=fix_bits, duration=3.0, stats=True), out=out)
    assert run_app(app) == 0
    stats = app.demod_stats
    assert stats["valid_crc"] > 50 and app.tracker.total_messages > 50
    assert stats["soft_fixed"] <= stats["fixed"]
    if fix_bits == 0:
        assert stats["fixed"] == 0  # --no-fix repairs nothing, soft decisions included
    text = out.getvalue()
    assert "soft-decision-corrected" in text and "phase-corrected" not in text
    json.dumps(app.stats_document())


def test_raw_mode_prints_every_message_and_nothing_else():
    out = io.StringIO()
    app = App(sim_config(duration=3.0, raw_stdout=True, stats=True), out=out)
    assert run_app(app) == 0
    lines = out.getvalue().splitlines()
    assert len(lines) == app.tracker.total_messages > 50
    assert all(AVR_LINE.fullmatch(line) for line in lines)


def test_raw_mode_stops_when_the_reader_goes_away():
    app = App(sim_config(duration=None, raw_stdout=True), out=ClosedPipe())  # endless input
    assert run_app(app, timeout=30) == 0


def test_a_closed_stdout_does_not_break_the_run():
    app = App(sim_config(duration=2.0), out=ClosedPipe())
    assert run_app(app) == 0
    assert app.tracker.total_messages > 20


def test_a_dongle_feeds_the_decoder():
    stream = SimStream(default_scenario(), noise_sigma=2.0, seed=1)
    lib = FakeRtlLib(blocks=[stream.read(BLOCK_SAMPLES) for _ in range(40)])  # about 2.6 s of traffic
    source = RtlSdrSource(lib=lib, gain=40.0, ppm=2)
    app = App(Config(source="rtlsdr", **NO_OUTPUTS), source=source, out=io.StringIO())

    assert run_app(app) == 0

    assert ("freq", 1_090_000_000) in lib.calls and ("rate", 2_000_000) in lib.calls
    assert ("gain", 402) in lib.calls and ("ppm", 2) in lib.calls  # 40.2 dB is the nearest supported gain
    assert lib.closed
    assert app.dropped_blocks == 0
    assert app.tracker.total_messages > 50
    assert app.tracker.positions > 0  # no receiver position given: pairs of messages still give positions
    assert app.source_label.startswith("rtlsdr: Generic RTL2832U OEM (R820T)")


def test_replaying_a_recording_from_a_pipe():
    stream = SimStream(default_scenario(), noise_sigma=2.0, seed=1)
    recording = b"".join(stream.read(BLOCK_SAMPLES) for _ in range(30))
    app = App(sim_config(source="stdin", duration=None), source=StdinSource(io.BytesIO(recording)), out=io.StringIO())
    assert run_app(app) == 0
    assert app.tracker.total_messages > 30


# ------------------------------------------------------------------------------ --record


def sim_recording(blocks: int) -> bytes:
    stream = SimStream(default_scenario(), noise_sigma=2.0, seed=1)
    return b"".join(stream.read(BLOCK_SAMPLES) for _ in range(blocks))


def test_the_samples_are_saved_while_receiving_and_replay_to_the_same_result(tmp_path, caplog):
    recording = sim_recording(30)
    path = tmp_path / "rec.cu8"
    app = App(
        sim_config(source="stdin", duration=None, record=str(path)),
        source=StdinSource(io.BytesIO(recording)),
        out=io.StringIO(),
    )
    with caplog.at_level(logging.INFO, logger="adsbrx"):
        assert run_app(app) == 0

    assert path.read_bytes() == recording  # byte for byte what the decoder was given
    assert app.recorder.bytes_written == len(recording) and app.recorder.error is None
    assert f"recording the raw I/Q samples to {path}" in caplog.text
    assert re.search(r"recorded 8 MB \(2 s of samples\) to .*rec\.cu8", caplog.text)

    replay = App(sim_config(source="file", path=str(path), duration=None), out=io.StringIO())
    assert run_app(replay) == 0
    assert replay.tracker.total_messages == app.tracker.total_messages > 30


def test_a_dongle_is_recorded_in_full(tmp_path):
    stream = SimStream(default_scenario(), noise_sigma=2.0, seed=1)
    data = [stream.read(BLOCK_SAMPLES) for _ in range(40)]
    path = tmp_path / "rec.cu8"
    source = RtlSdrSource(lib=FakeRtlLib(blocks=data))
    app = App(Config(source="rtlsdr", record=str(path), **NO_OUTPUTS), source=source, out=io.StringIO())
    assert run_app(app) == 0
    assert path.read_bytes() == b"".join(data) and app.recorder.dropped_blocks == 0


def test_an_existing_file_is_not_overwritten_and_nothing_stays_open(tmp_path):
    path = tmp_path / "rec.cu8"
    path.write_bytes(b"precious")
    app = App(sim_config(record=str(path), **ANY_PORT), out=io.StringIO())
    with pytest.raises(AppError, match="already exists"):
        run_app(app)
    assert path.read_bytes() == b"precious"
    assert app.servers == {} and app.web is None and app.recorder is None


def test_a_failed_start_leaves_no_empty_recording_behind(tmp_path, caplog):
    path = tmp_path / "rec.cu8"
    app = App(
        Config(source="rtlsdr", record=str(path), **NO_OUTPUTS),
        source=RtlSdrSource(lib=FakeRtlLib(devices=0)),
        out=io.StringIO(),
    )
    with caplog.at_level(logging.INFO, logger="adsbrx"):
        assert run_app(app) == 1
    assert "No RTL-SDR device found" in caplog.text
    assert not path.exists()


def test_a_full_disk_ends_the_run_with_an_error_and_keeps_what_was_saved(monkeypatch, tmp_path, caplog):
    recording = sim_recording(30)
    disk = SlowDisk(fail_after=3)
    monkeypatch.setattr("adsbrx.app.Recorder", lambda path: Recorder(path, opener=lambda _: disk))
    path = tmp_path / "rec.cu8"
    app = App(
        sim_config(source="stdin", duration=None, record=str(path)),
        source=StdinSource(io.BytesIO(recording)),
        out=io.StringIO(),
    )
    with caplog.at_level(logging.INFO, logger="adsbrx"):
        assert run_app(app) == 1
    assert f"cannot write to {path}: No space left on device" in caplog.text
    assert disk.saved == recording[: 3 * 2 * BLOCK_SAMPLES]


def test_a_failure_that_only_shows_when_the_file_is_closed_is_still_reported(monkeypatch, tmp_path, caplog):
    class Unflushable(SlowDisk):
        def close(self):
            super().close()
            raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr("adsbrx.app.Recorder", lambda path: Recorder(path, opener=lambda _: Unflushable()))
    path = tmp_path / "rec.cu8"
    app = App(
        sim_config(source="stdin", duration=None, record=str(path)),
        source=StdinSource(io.BytesIO(sim_recording(10))),
        out=io.StringIO(),
    )
    with caplog.at_level(logging.INFO, logger="adsbrx"):
        assert run_app(app) == 1
    assert f"cannot write to {path}: Input/output error" in caplog.text


# --------------------------------------------------------------------------- failures


def test_a_missing_dongle_ends_the_run_with_an_error(caplog):
    out = io.StringIO()
    app = App(Config(source="rtlsdr", **NO_OUTPUTS), source=RtlSdrSource(lib=FakeRtlLib(devices=0)), out=out)
    with caplog.at_level(logging.INFO, logger="adsbrx"):
        assert run_app(app) == 1
    assert "No RTL-SDR device found" in caplog.text
    assert len(out.getvalue().splitlines()) <= 1  # at most the start-up line: no closing summary of nothing


def test_an_error_after_some_traffic_still_prints_the_summary(caplog):
    class Unplugged(sources.Source):
        name = "unplugged"

        def run(self, sink):
            stream = SimStream(default_scenario(), noise_sigma=2.0, seed=1)
            for number in range(20):
                sink(number * BLOCK_SAMPLES, stream.read(BLOCK_SAMPLES))
            raise sources.SourceError("the dongle was unplugged")

    out = io.StringIO()
    app = App(Config(source="rtlsdr", **NO_OUTPUTS), source=Unplugged(), out=out)
    with caplog.at_level(logging.INFO, logger="adsbrx"):
        assert run_app(app) == 1
    assert "the dongle was unplugged" in caplog.text
    assert app.tracker.total_messages > 10
    assert "with position" in out.getvalue().splitlines()[-1]


def test_an_unreachable_rtl_tcp_server_ends_the_run_with_an_error(monkeypatch, caplog):
    def refuse(*args, **kwargs):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(sources.socket, "create_connection", refuse)
    app = App(Config(source="rtl_tcp", host="192.0.2.1", port=1234, **NO_OUTPUTS), out=io.StringIO())
    with caplog.at_level(logging.INFO, logger="adsbrx"):
        assert run_app(app) == 1
    assert "cannot connect to rtl_tcp at 192.0.2.1:1234" in caplog.text


@pytest.mark.parametrize("busy, label", [("sbs_port", "SBS"), ("beast_port", "Beast"), ("web_port", "HTTP")])
def test_a_port_in_use_is_reported_and_nothing_stays_open(busy, label):
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        app = App(sim_config(**{**ANY_PORT, busy: port}), out=io.StringIO())
        with pytest.raises(AppError, match=f"cannot listen for {label} on 127.0.0.1:{port}"):
            run_app(app)
    assert app.servers == {} and app.web is None


# ---------------------------------------------------------------------------- warnings


def test_a_silent_dongle_is_reported_once(caplog):
    app = App(Config(source="rtlsdr", **NO_OUTPUTS), source=RtlSdrSource(lib=FakeRtlLib()), out=io.StringIO())
    app._started = 100.0
    with caplog.at_level(logging.WARNING, logger="adsbrx"):
        app._tick(110.0)
        assert not caplog.records  # too early to say
        app._tick(125.0)
        app._tick(140.0)
    assert [record.getMessage() for record in caplog.records] == [
        "no ADS-B messages received yet: check the antenna, the gain, and that no other program is using the dongle"
    ]


def test_no_silence_warning_when_messages_arrive_or_the_input_is_not_live(caplog):
    dongle = App(Config(source="rtlsdr", **NO_OUTPUTS), source=RtlSdrSource(lib=FakeRtlLib()), out=io.StringIO())
    dongle._started = 0.0
    dongle.tracker.total_messages = 1
    replay = App(sim_config(), out=io.StringIO())
    replay._started = 0.0
    with caplog.at_level(logging.WARNING, logger="adsbrx"):
        dongle._tick(500.0)
        replay._tick(500.0)
    assert not caplog.records


def test_dropped_blocks_are_reported_without_flooding(caplog):
    app = App(sim_config(demod="classic"), out=io.StringIO())
    with caplog.at_level(logging.WARNING, logger="adsbrx"):
        app.dropped_blocks = 5
        app._tick(1.0)  # first drops: warn
        app.dropped_blocks = 9
        app._tick(2.0)  # more drops, but we just warned
        app._tick(3.0)  # nothing new
        app.dropped_blocks = 12
        app._tick(30.0)  # long enough ago: warn again
        app._tick(31.0)
    assert [record.getMessage() for record in caplog.records] == [
        "the CPU cannot keep up: 5 sample blocks dropped so far",
        "the CPU cannot keep up: 12 sample blocks dropped so far",
    ]


def test_the_sequence_detector_suggests_the_light_demodulator_when_the_cpu_is_too_slow(caplog):
    app = App(sim_config(), out=io.StringIO())
    with caplog.at_level(logging.WARNING, logger="adsbrx"):
        app.dropped_blocks = 5
        app._tick(1.0)
    assert [record.getMessage() for record in caplog.records] == [
        "the CPU cannot keep up: 5 sample blocks dropped so far (--demod classic needs less CPU)"
    ]


def test_ticks_feed_the_rate_meter():
    app = App(sim_config(), out=io.StringIO())
    app._tick(0.0)
    app.tracker.total_messages = 50
    app._tick(2.0)
    assert app.rate.rate == pytest.approx(25.0)
    assert app.stats_document()["message_rate"] == 25.0
