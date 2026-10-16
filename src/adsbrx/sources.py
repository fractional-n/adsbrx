"""Sample sources. Every source delivers 2 MS/s unsigned 8-bit I/Q (cu8) to a sink."""

from __future__ import annotations

import logging
import socket
import struct
import sys
import threading
import time
from collections.abc import Callable
from typing import BinaryIO

import numpy as np

from .demod import SAMPLE_RATE
from .rtl_native import RtlSdrDevice, RtlSdrError

log = logging.getLogger("adsbrx")

Sink = Callable[[int, bytes], None]  # (absolute index of the first sample, cu8 bytes)

BLOCK_SAMPLES = 131072
FORMATS = {"cu8": 2, "cs8": 2, "cs16": 4}  # bytes per complex sample


class SourceError(RuntimeError):
    pass


class Source:
    """Base class: `run()` blocks and pushes blocks into the sink until stopped or exhausted."""

    name = "source"
    live = False  # live sources cannot be paused: the pipeline drops blocks instead of waiting

    def __init__(self) -> None:
        self._stop = threading.Event()

    def run(self, sink: Sink) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()


def to_cu8(data: bytes, fmt: str) -> bytes:
    """Convert a block of `fmt` samples to cu8."""
    if fmt == "cu8":
        return data
    if fmt == "cs8":
        return (np.frombuffer(data, dtype=np.uint8) ^ 0x80).tobytes()
    if fmt == "cs16":
        values = np.frombuffer(data, dtype="<i2")
        return ((values >> 8) + 128).astype(np.uint8).tobytes()
    raise SourceError(f"unsupported sample format: {fmt}")


class _Pacer:
    """Keeps a producer in step with the wall clock at 2 MS/s."""

    def __init__(self, stop: threading.Event, speed: float = 1.0):
        self._stop, self._speed = stop, speed
        self._t0 = time.monotonic()
        self._samples = 0

    def wait(self, samples: int) -> None:
        self._samples += samples
        due = self._t0 + self._samples / (SAMPLE_RATE * self._speed)
        delay = due - time.monotonic()
        if delay > 0:
            self._stop.wait(delay)


class _StreamSource(Source):
    """Shared reader for files and stdin."""

    def __init__(self, fmt: str = "cu8", realtime: bool = False, loop: bool = False, speed: float = 1.0):
        super().__init__()
        if fmt not in FORMATS:
            raise SourceError(f"unsupported sample format: {fmt} (choose from {', '.join(FORMATS)})")
        self.fmt, self.realtime, self.loop, self.speed = fmt, realtime, loop, speed

    def _open(self) -> BinaryIO:
        raise NotImplementedError

    def _close(self, stream: BinaryIO) -> None:
        stream.close()

    def run(self, sink: Sink) -> None:
        width = FORMATS[self.fmt]
        stream = self._open()
        pacer = _Pacer(self._stop, self.speed) if self.realtime else None
        index = 0
        carry = b""
        try:
            while not self._stop.is_set():
                chunk = stream.read(BLOCK_SAMPLES * width)
                if not chunk:
                    if self.loop and index > 0 and stream.seekable():
                        stream.seek(0)
                        carry = b""
                        continue
                    break
                chunk = carry + chunk
                usable = len(chunk) // width * width
                chunk, carry = chunk[:usable], chunk[usable:]
                if not chunk:
                    continue
                data = to_cu8(chunk, self.fmt)
                sink(index, data)
                index += len(data) // 2
                if pacer:
                    pacer.wait(len(data) // 2)
        finally:
            self._close(stream)


class FileSource(_StreamSource):
    name = "file"

    def __init__(self, path: str, **kw):
        super().__init__(**kw)
        self.path = path

    def _open(self) -> BinaryIO:
        try:
            return open(self.path, "rb")
        except OSError as exc:
            raise SourceError(f"cannot open {self.path}: {exc.strerror or exc}") from exc


class StdinSource(_StreamSource):
    """Reads raw samples from a pipe, e.g. `rtl_sdr -f 1090000000 -s 2000000 - | adsbrx --source stdin`."""

    name = "stdin"

    def __init__(self, stream: BinaryIO | None = None, **kw):
        super().__init__(**kw)
        self._stream = stream

    def _open(self) -> BinaryIO:
        return self._stream if self._stream is not None else sys.stdin.buffer

    def _close(self, stream: BinaryIO) -> None:
        pass  # leave the process' stdin open


class SimSource(Source):
    """Generates traffic from the built-in simulator."""

    name = "sim"

    def __init__(self, scenario=None, noise_sigma: float = 2.0, seed: int = 1, realtime: bool = True, speed: float = 1.0):
        super().__init__()
        from .sim.scenario import SimStream, default_scenario

        self.scenario = scenario if scenario is not None else default_scenario(seed=seed)
        self._stream = SimStream(self.scenario, noise_sigma=noise_sigma, seed=seed)
        self.realtime, self.speed = realtime, speed

    def run(self, sink: Sink) -> None:
        pacer = _Pacer(self._stop, self.speed) if self.realtime else None
        index = 0
        while not self._stop.is_set():
            data = self._stream.read(BLOCK_SAMPLES)
            sink(index, data)
            index += BLOCK_SAMPLES
            if pacer:
                pacer.wait(BLOCK_SAMPLES)


class RtlTcpSource(Source):
    """Network client for `rtl_tcp` (https://osmocom.org/projects/rtl-sdr)."""

    name = "rtl_tcp"
    live = True

    _SET_FREQ, _SET_RATE, _SET_GAIN_MODE, _SET_GAIN, _SET_PPM, _SET_AGC = 0x01, 0x02, 0x03, 0x04, 0x05, 0x08
    _SET_BIAS_TEE = 0x0E

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 1234,
        freq_hz: int = 1_090_000_000,
        gain: str | float = "max",
        ppm: int = 0,
        agc: bool = False,
        bias_tee: bool = False,
        timeout: float = 5.0,
    ):
        super().__init__()
        self.host, self.port, self.freq_hz, self.gain = host, port, freq_hz, gain
        self.ppm, self.agc, self.bias_tee, self.timeout = ppm, agc, bias_tee, timeout
        self._sock: socket.socket | None = None
        self.tuner = "unknown"

    def _command(self, cmd: int, value: int) -> None:
        assert self._sock is not None
        self._sock.sendall(struct.pack(">BI", cmd, value & 0xFFFFFFFF))

    def _recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        buf = b""
        while len(buf) < n:
            part = self._sock.recv(n - len(buf))
            if not part:
                raise SourceError("rtl_tcp closed the connection")
            buf += part
        return buf

    def run(self, sink: Sink) -> None:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise SourceError(f"cannot connect to rtl_tcp at {self.host}:{self.port}: {exc}") from exc
        self._sock = sock
        try:
            sock.settimeout(self.timeout)
            header = self._recv_exact(12)
            if header[:4] != b"RTL0":
                raise SourceError("the server does not look like rtl_tcp (bad magic)")
            tuner_type = struct.unpack(">I", header[4:8])[0]
            self.tuner = {1: "E4000", 2: "FC0012", 3: "FC0013", 4: "FC2580", 5: "R820T", 6: "R828D"}.get(
                tuner_type, "unknown"
            )
            log.info("connected to rtl_tcp at %s:%d (%s tuner)", self.host, self.port, self.tuner)
            self._command(self._SET_RATE, SAMPLE_RATE)
            self._command(self._SET_FREQ, self.freq_hz)
            if self.ppm:
                self._command(self._SET_PPM, self.ppm)
            self._command(self._SET_AGC, 1 if self.agc else 0)
            if str(self.gain).lower() == "auto":
                self._command(self._SET_GAIN_MODE, 0)
            else:
                tenth = 496 if str(self.gain).lower() == "max" else int(round(float(self.gain) * 10))
                self._command(self._SET_GAIN_MODE, 1)
                self._command(self._SET_GAIN, tenth)
            if self.bias_tee:
                self._command(self._SET_BIAS_TEE, 1)

            index = 0
            pending = b""
            while not self._stop.is_set():
                try:
                    part = sock.recv(262144)
                except socket.timeout as exc:
                    raise SourceError(f"no data from rtl_tcp for {self.timeout:.0f} s") from exc
                except OSError:
                    if self._stop.is_set():
                        break
                    raise
                if not part:
                    if self._stop.is_set():
                        break
                    raise SourceError("rtl_tcp closed the connection")
                part = pending + part
                usable = len(part) & ~1
                part, pending = part[:usable], part[usable:]
                if part:
                    sink(index, part)
                    index += len(part) // 2
        finally:
            self._sock = None
            sock.close()

    def stop(self) -> None:
        super().stop()
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class RtlSdrSource(Source):
    """A dongle on this machine, through librtlsdr."""

    name = "rtlsdr"
    live = True

    def __init__(
        self,
        device_index: int = 0,
        freq_hz: int = 1_090_000_000,
        gain: str | float = "max",
        ppm: int = 0,
        agc: bool = False,
        bias_tee: bool = False,
        dll_dir: str | None = None,
        lib=None,
    ):
        super().__init__()
        self.device_index, self.freq_hz, self.gain, self.ppm = device_index, freq_hz, gain, ppm
        self.agc, self.bias_tee, self.dll_dir, self._lib = agc, bias_tee, dll_dir, lib
        self._device: RtlSdrDevice | None = None
        self.description = ""

    def run(self, sink: Sink) -> None:
        try:
            device = RtlSdrDevice(self.device_index, lib=self._lib, dll_dir=self.dll_dir)
        except RtlSdrError as exc:
            raise SourceError(str(exc)) from exc
        try:
            applied = device.configure(self.freq_hz, SAMPLE_RATE, self.gain, self.ppm, self.agc, self.bias_tee)
            gain_text = "auto" if applied is None else f"{applied:.1f} dB"
            self.description = f"{device.name} ({device.tuner}), gain {gain_text}"
            log.info("tuned to %.3f MHz: %s", self.freq_hz / 1e6, self.description)
            self._device = device
            index = 0

            def on_data(data: bytes) -> None:
                nonlocal index
                if self._stop.is_set():
                    device.cancel()
                    return
                sink(index, data)
                index += len(data) // 2

            if not self._stop.is_set():
                device.read_async(on_data)
        except RtlSdrError as exc:
            # librtlsdr may report a non-zero code after a requested cancel (rtl_sdr itself ignores it then)
            if not self._stop.is_set():
                raise SourceError(str(exc)) from exc
        finally:
            self._device = None
            device.close()

    def stop(self) -> None:
        super().stop()
        device = self._device
        if device is not None:
            device.cancel()
