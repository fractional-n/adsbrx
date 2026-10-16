import io
import socket
import struct
import threading
import time

import numpy as np
import pytest
from fakes import FakeRtlLib

from adsbrx import sources
from adsbrx.sources import (
    BLOCK_SAMPLES,
    FileSource,
    RtlSdrSource,
    RtlTcpSource,
    SimSource,
    SourceError,
    StdinSource,
    to_cu8,
)


def collect(source, stop_after=None):
    """Run a source to completion and return its (index, data) blocks."""
    blocks = []

    def sink(index, data):
        blocks.append((index, data))
        if stop_after is not None and len(blocks) >= stop_after:
            source.stop()

    source.run(sink)
    return blocks


def test_to_cu8_conversions():
    assert to_cu8(b"\x01\x02", "cu8") == b"\x01\x02"
    assert to_cu8(bytes([0x00, 0x7F, 0x80, 0xFF]), "cs8") == bytes([128, 255, 0, 127])
    cs16 = np.array([0, 32767, -32768, -256], dtype="<i2").tobytes()
    assert to_cu8(cs16, "cs16") == bytes([128, 255, 0, 127])
    with pytest.raises(SourceError):
        to_cu8(b"", "float32")


class TestFileSource:
    def test_blocks_are_contiguous_and_complete(self, tmp_path):
        payload = np.random.default_rng(1).integers(0, 256, BLOCK_SAMPLES * 2 * 2 + 600, dtype=np.uint8).tobytes()
        path = tmp_path / "capture.cu8"
        path.write_bytes(payload)
        blocks = collect(FileSource(str(path)))
        assert [i for i, _ in blocks] == [0, BLOCK_SAMPLES, 2 * BLOCK_SAMPLES]
        assert b"".join(d for _, d in blocks) == payload

    def test_odd_trailing_byte_is_dropped(self, tmp_path):
        path = tmp_path / "odd.cu8"
        path.write_bytes(b"\x10\x20\x30\x40\x50")
        blocks = collect(FileSource(str(path)))
        assert blocks == [(0, b"\x10\x20\x30\x40")]

    def test_cs16_file(self, tmp_path):
        path = tmp_path / "x.cs16"
        path.write_bytes(np.array([0, 0, 32767, -32768], dtype="<i2").tobytes())
        assert collect(FileSource(str(path), fmt="cs16")) == [(0, bytes([128, 128, 255, 0]))]

    def test_missing_file(self, tmp_path):
        with pytest.raises(SourceError, match="cannot open"):
            collect(FileSource(str(tmp_path / "nope.cu8")))

    def test_unknown_format(self, tmp_path):
        with pytest.raises(SourceError, match="unsupported"):
            FileSource(str(tmp_path / "x"), fmt="cf32")

    def test_loop_keeps_counting(self, tmp_path):
        path = tmp_path / "short.cu8"
        path.write_bytes(bytes(range(200)))
        blocks = collect(FileSource(str(path), loop=True), stop_after=4)
        assert [i for i, _ in blocks] == [0, 100, 200, 300]
        assert all(d == bytes(range(200)) for _, d in blocks)

    def test_loop_on_empty_file_terminates(self, tmp_path):
        path = tmp_path / "empty.cu8"
        path.write_bytes(b"")
        assert collect(FileSource(str(path), loop=True)) == []

    def test_realtime_pacing(self, tmp_path):
        path = tmp_path / "paced.cu8"
        path.write_bytes(bytes(BLOCK_SAMPLES * 2 * 3))  # 0.197 s of signal
        start = time.monotonic()
        collect(FileSource(str(path), realtime=True))
        assert time.monotonic() - start >= 0.15

    def test_fast_mode_does_not_wait(self, tmp_path):
        path = tmp_path / "fast.cu8"
        path.write_bytes(bytes(BLOCK_SAMPLES * 2 * 3))
        start = time.monotonic()
        collect(FileSource(str(path), realtime=False))
        assert time.monotonic() - start < 0.15


class TestStdinSource:
    def test_reads_from_a_stream_and_leaves_it_open(self):
        stream = io.BytesIO(bytes(range(256)) * 4)
        blocks = collect(StdinSource(stream=stream))
        assert b"".join(d for _, d in blocks) == bytes(range(256)) * 4
        assert not stream.closed


class TestSimSource:
    def test_generates_blocks(self):
        blocks = collect(SimSource(realtime=False, seed=3), stop_after=3)
        assert [i for i, _ in blocks] == [0, BLOCK_SAMPLES, 2 * BLOCK_SAMPLES]
        assert all(len(d) == BLOCK_SAMPLES * 2 for _, d in blocks)


class FakeRtlTcpServer:
    """Speaks just enough of the rtl_tcp protocol."""

    def __init__(self, payload=b"", magic=b"RTL0", hold_open=False):
        self.payload, self.magic, self.hold_open = payload, magic, hold_open
        self.commands = []
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        conn, _ = self.listener.accept()
        try:
            conn.sendall(self.magic + struct.pack(">II", 5, 29))
            conn.settimeout(0.3)  # the client sends all its commands right away
            buf = b""
            while True:
                try:
                    part = conn.recv(1024)
                except OSError:
                    break
                if not part:
                    break
                buf += part
            for i in range(0, len(buf) - 4, 5):
                self.commands.append(struct.unpack(">BI", buf[i : i + 5]))
            conn.settimeout(5.0)
            for i in range(0, len(self.payload), 777):
                conn.sendall(self.payload[i : i + 777])
                time.sleep(0.001)
            while self.hold_open:
                try:
                    conn.sendall(b"\x80\x80" * 500)
                except OSError:
                    break
                time.sleep(0.01)
        except OSError:
            pass
        finally:
            conn.close()
            self.listener.close()


class TestRtlTcpSource:
    def test_configuration_and_data(self):
        payload = bytes(range(256)) * 20 + b"\x55"  # odd length
        server = FakeRtlTcpServer(payload)
        source = RtlTcpSource("127.0.0.1", server.port, gain="max", ppm=0, timeout=2.0)
        blocks = []
        with pytest.raises(SourceError, match="closed the connection"):
            source.run(lambda i, d: blocks.append((i, d)))
        server.thread.join(2)
        assert source.tuner == "R820T"
        assert server.commands == [(2, 2_000_000), (1, 1_090_000_000), (8, 0), (3, 1), (4, 496)]
        got = b"".join(d for _, d in blocks)
        assert got == payload[:-1]
        position = 0
        for index, data in blocks:
            assert index == position
            position += len(data) // 2

    def test_auto_gain_and_ppm_commands(self):
        server = FakeRtlTcpServer(b"\x01\x02" * 10)
        source = RtlTcpSource("127.0.0.1", server.port, gain="auto", ppm=12, agc=True, bias_tee=True, timeout=2.0)
        with pytest.raises(SourceError):
            source.run(lambda i, d: None)
        server.thread.join(2)
        assert (5, 12) in server.commands and (8, 1) in server.commands and (3, 0) in server.commands
        assert (0x0E, 1) in server.commands

    def test_stop_ends_cleanly(self):
        server = FakeRtlTcpServer(hold_open=True)
        source = RtlTcpSource("127.0.0.1", server.port, timeout=2.0)
        got = []

        def sink(index, data):
            got.append(index)
            if len(got) == 3:
                threading.Thread(target=source.stop).start()

        source.run(sink)  # returns without raising
        assert len(got) >= 3

    def test_connection_refused(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with pytest.raises(SourceError, match="cannot connect"):
            RtlTcpSource("127.0.0.1", port, timeout=1.0).run(lambda i, d: None)

    def test_bad_magic(self):
        server = FakeRtlTcpServer(magic=b"HTTP")
        with pytest.raises(SourceError, match="does not look like rtl_tcp"):
            RtlTcpSource("127.0.0.1", server.port, timeout=2.0).run(lambda i, d: None)


class TestRtlSdrSource:
    def test_streams_blocks_and_closes_the_device(self):
        blocks = [bytes([i]) * 2000 for i in range(4)]
        lib = FakeRtlLib(blocks=blocks)
        source = RtlSdrSource(freq_hz=1_090_000_000, gain=35, lib=lib)
        got = collect(source)
        assert [d for _, d in got] == blocks
        assert [i for i, _ in got] == [0, 1000, 2000, 3000]
        assert lib.closed
        assert "R820T" in source.description and "33.8 dB" in source.description
        assert ("freq", 1_090_000_000) in lib.calls and ("rate", 2_000_000) in lib.calls

    def test_stop_cancels_the_stream(self):
        lib = FakeRtlLib(blocks=[b"\x00" * 100] * 50)
        source = RtlSdrSource(lib=lib)
        got = collect(source, stop_after=3)
        assert 3 <= len(got) < 50
        assert lib.cancelled and lib.closed

    def test_missing_device(self):
        with pytest.raises(SourceError, match="No RTL-SDR device found"):
            RtlSdrSource(lib=FakeRtlLib(devices=0)).run(lambda i, d: None)

    def test_sink_error_propagates(self):
        lib = FakeRtlLib(blocks=[b"\x00" * 10] * 5)

        def sink(index, data):
            raise KeyError("sink broke")

        with pytest.raises(KeyError):
            RtlSdrSource(lib=lib).run(sink)
        assert lib.closed

    def test_stream_failure_is_reported(self):
        lib = FakeRtlLib(blocks=[b"\x00" * 10] * 2, read_rc=-5)
        with pytest.raises(SourceError, match="read_async stopped with code -5"):
            RtlSdrSource(lib=lib).run(lambda i, d: None)
        assert lib.closed

    def test_error_code_after_a_requested_stop_is_not_a_failure(self):
        # librtlsdr can return a non-zero code once a cancel has been honoured
        lib = FakeRtlLib(blocks=[b"\x00" * 10] * 20, read_rc=-5)
        got = collect(RtlSdrSource(lib=lib), stop_after=2)
        assert 2 <= len(got) < 20
        assert lib.cancelled and lib.closed


def test_sources_module_exports_formats():
    assert set(sources.FORMATS) == {"cu8", "cs8", "cs16"}
