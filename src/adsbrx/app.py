"""Application glue: sample source -> demodulator -> tracker -> network and console outputs."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections import deque
from dataclasses import dataclass
from typing import TextIO

from . import __version__
from .demod import SAMPLE_RATE, Demodulator
from .levels import LevelMeter
from .mlse import MlseDemodulator
from .outputs.beast import encode_beast, encode_raw
from .outputs.console import Console, format_stats, format_summary
from .outputs.netserver import BroadcastServer
from .outputs.sbs import SbsFormatter
from .outputs.web import WebServer
from .pipeline import Batch, Pipeline
from .recorder import Recorder, RecorderError
from .sources import (
    FileSource,
    RtlSdrSource,
    RtlTcpSource,
    SimSource,
    Source,
    StdinSource,
)
from .tracker import Tracker

log = logging.getLogger("adsbrx")

SOURCES = ("rtlsdr", "rtl_tcp", "file", "stdin", "sim")
DEMODULATORS = ("classic", "mlse")
SILENCE_WARNING_S = 20.0  # live input without a single message for this long: probably a setup problem
DROP_WARNING_INTERVAL_S = 10.0


class AppError(RuntimeError):
    """A problem the user can fix (port in use, missing file, ...)."""


@dataclass
class Config:
    # sample source
    source: str = "rtlsdr"
    device_index: int = 0
    gain: str | float = "max"  # "max", "auto" or a value in dB
    ppm: int = 0
    agc: bool = False
    bias_tee: bool = False
    freq_hz: int = 1_090_000_000
    host: str = "127.0.0.1"  # rtl_tcp server
    port: int = 1234
    path: str | None = None
    fmt: str = "cu8"
    loop: bool = False
    fast: bool = False  # replay files / the simulator as fast as possible
    speed: float = 1.0  # replay speed factor
    dll_dir: str | None = None
    seed: int = 1
    record: str | None = None  # also save the raw I/Q stream (cu8) to this new file
    # receiver and decoder
    lat: float | None = None
    lon: float | None = None
    demod: str = "mlse"
    fix_bits: int = 1
    all_df: bool = False
    expire_s: float = 60.0
    max_range_nm: float = 300.0
    # outputs: None disables, 0 lets the OS pick a free port
    bind: str = "127.0.0.1"
    web_port: int | None = 8080
    sbs_port: int | None = 30003
    beast_port: int | None = 30005
    raw_port: int | None = 30002
    # console
    interactive: bool = False
    raw_stdout: bool = False
    stats: bool = False
    # run control
    hold: bool = False  # keep serving after the input ends
    duration: float | None = None  # stop after this many seconds of stream time


def build_source(cfg: Config) -> Source:
    kind = cfg.source
    realtime = not cfg.fast
    if kind == "rtlsdr":
        return RtlSdrSource(cfg.device_index, cfg.freq_hz, cfg.gain, cfg.ppm, cfg.agc, cfg.bias_tee, cfg.dll_dir)
    if kind == "rtl_tcp":
        return RtlTcpSource(cfg.host, cfg.port, cfg.freq_hz, cfg.gain, cfg.ppm, cfg.agc, cfg.bias_tee)
    if kind == "file":
        if not cfg.path:
            raise AppError("--file is required with --source file")
        return FileSource(cfg.path, fmt=cfg.fmt, realtime=realtime, loop=cfg.loop, speed=cfg.speed)
    if kind == "stdin":
        return StdinSource(fmt=cfg.fmt)
    if kind == "sim":
        from .sim.scenario import HANEDA, default_scenario

        receiver = (cfg.lat, cfg.lon) if cfg.lat is not None and cfg.lon is not None else HANEDA
        scenario = default_scenario(receiver=receiver, seed=cfg.seed)
        return SimSource(scenario, seed=cfg.seed, realtime=realtime, speed=cfg.speed)
    raise AppError(f"unknown source: {kind} (choose from {', '.join(SOURCES)})")


def build_demodulator(cfg: Config) -> Demodulator:
    if cfg.demod == "classic":
        return Demodulator(fix_bits=cfg.fix_bits, all_df=cfg.all_df)
    if cfg.demod == "mlse":
        # --aggressive (2-bit repair) also lets the soft-decision repair search deeper
        window, flips = (14, 4) if cfg.fix_bits > 1 else (12, 3)
        return MlseDemodulator(fix_bits=cfg.fix_bits, all_df=cfg.all_df, soft_window=window, soft_flips=flips)
    raise AppError(f"unknown demodulator: {cfg.demod} (choose from {', '.join(DEMODULATORS)})")


class RateMeter:
    """Messages per second over a short sliding window."""

    def __init__(self, window_s: float = 5.0):
        self.window_s = window_s
        self.rate = 0.0
        self._samples: deque[tuple[float, int]] = deque()

    def update(self, total: int, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        self._samples.append((now, total))
        while len(self._samples) > 2 and now - self._samples[0][0] > self.window_s:
            self._samples.popleft()
        first_time, first_total = self._samples[0]
        self.rate = (total - first_total) / (now - first_time) if now > first_time else 0.0
        return self.rate


class App:
    def __init__(self, config: Config, source: Source | None = None, out: TextIO | None = None):
        self.config = config
        self.source = source if source is not None else build_source(config)
        self.out = out if out is not None else sys.stdout
        receiver = None
        if config.lat is not None and config.lon is not None:
            receiver = (config.lat, config.lon)
        elif hasattr(self.source, "scenario"):
            receiver = self.source.scenario.receiver
        self.tracker = Tracker(receiver=receiver, expire_s=config.expire_s, max_range_nm=config.max_range_nm)
        self.demod = build_demodulator(config)
        self.levels = LevelMeter()
        self.rate = RateMeter()
        self.sbs = SbsFormatter()
        self.servers: dict[str, BroadcastServer] = {}
        self.web: WebServer | None = None
        self.pipeline: Pipeline | None = None
        self.recorder: Recorder | None = None
        self.console: Console | None = None
        self.demod_stats: dict = {}
        self.dropped_blocks = 0
        self._deadline: float | None = None
        self._last_expire = 0.0
        self._started = time.monotonic()
        self._outputs_started = False
        self._raw_stdout = config.raw_stdout
        self._warned_silent = False
        self._last_dropped = 0
        self._last_drop_warning = float("-inf")

    # -- information for the user -----------------------------------------------------------

    @property
    def source_label(self) -> str:
        description = getattr(self.source, "description", "")
        return f"{self.source.name}: {description}" if description else self.source.name

    @property
    def input_label(self) -> str:
        cfg = self.config
        return {
            "rtlsdr": f"RTL-SDR device #{cfg.device_index}",
            "rtl_tcp": f"rtl_tcp at {cfg.host}:{cfg.port}",
            "file": f"file {cfg.path}",
            "stdin": "standard input",
            "sim": "built-in signal simulator",
        }.get(self.source.name, self.source.name)

    def _extra_status(self) -> str:
        parts = []
        if self.dropped_blocks:
            parts.append(f"dropped {self.dropped_blocks} blocks")
        recorder = self.recorder
        if recorder is not None:
            text = f"recording {recorder.bytes_written / 1e6:.0f} MB"
            if recorder.dropped_blocks:
                text += f" ({recorder.dropped_blocks} blocks lost)"
            parts.append(text)
        clients = [f"{name} {server.clients}" for name, server in self.servers.items()]
        if self.web is not None:
            clients.append(f"web {self.web.viewers}")
        if clients:
            parts.append("clients " + " ".join(clients))
        return " | ".join(parts)

    def stats_document(self) -> dict:
        tracker = self.tracker
        outputs = {name: {"port": server.port, "clients": server.clients} for name, server in self.servers.items()}
        if self.web is not None:
            outputs["web"] = {"port": self.web.port, "clients": self.web.viewers}
        return {
            "version": __version__,
            "source": self.source_label,
            "uptime": round(time.monotonic() - self._started, 1),
            "stream_time": round(tracker.now, 1),
            "messages": tracker.total_messages,
            "message_rate": round(self.rate.rate, 1),
            "aircraft": len(tracker.visible()),
            "positions": tracker.positions,
            "rejected_positions": tracker.rejected_positions,
            "max_range_nm": round(tracker.max_distance_nm, 1),
            "dropped_blocks": self.dropped_blocks,
            "demod": self.demod_stats,
            "levels": self.levels.reading(),
            "outputs": outputs,
        }

    # -- lifecycle --------------------------------------------------------------------------

    async def start_outputs(self) -> None:
        if self._outputs_started:
            return
        cfg = self.config
        try:
            for key, label, port in (
                ("raw", "raw", cfg.raw_port),
                ("sbs", "SBS", cfg.sbs_port),
                ("beast", "Beast", cfg.beast_port),
            ):
                if port is not None:
                    server = BroadcastServer(label, cfg.bind, port)
                    await server.start()
                    self.servers[key] = server
            if cfg.web_port is not None:
                self.web = WebServer(
                    self.tracker, cfg.bind, cfg.web_port, stats=self.stats_document, levels=self.levels.document
                )
                await self.web.start()
        except OSError as exc:
            await self.close_outputs()
            raise AppError(str(exc)) from exc
        self._outputs_started = True

    async def close_outputs(self) -> None:
        for server in self.servers.values():
            await server.close()
        self.servers.clear()
        if self.web is not None:
            await self.web.close()
            self.web = None
        self._outputs_started = False

    async def run(self) -> int:
        """Runs until the input ends (or Ctrl-C); returns the process exit code."""
        cfg = self.config
        loop = asyncio.get_running_loop()
        await self.start_outputs()
        if cfg.record is not None:
            recorder = Recorder(cfg.record)
            try:
                recorder.start()
            except RecorderError as exc:
                await self.close_outputs()
                raise AppError(str(exc)) from exc
            self.recorder = recorder
        self._started = time.monotonic()

        show_console = not cfg.raw_stdout
        self.console = Console(
            self.tracker, self._extra_status, rate=lambda: self.rate.rate, table=cfg.interactive, out=self.out
        )
        self.pipeline = Pipeline(self.source, self.demod, self._on_batch, loop, recorder=self.recorder)
        if cfg.duration is not None:
            self._deadline = self.pipeline.t0 + cfg.duration
        self.pipeline.start()
        log.info("input: %s", self.input_label)
        if self.recorder is not None:
            log.info("recording the raw I/Q samples to %s (4 MB per second)", os.path.abspath(self.recorder.path))

        tasks = [asyncio.create_task(self._ticker())]
        if show_console:
            tasks.append(asyncio.create_task(self.console.run()))
        error = None
        try:
            error = await self.pipeline.done
            if error is None and cfg.hold:
                log.info("input finished; still serving (press Ctrl-C to quit)")
                await asyncio.Event().wait()
        finally:
            self.pipeline.stop()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await loop.run_in_executor(None, self.pipeline.join, 3.0)
            if self.recorder is not None:
                await loop.run_in_executor(None, self.recorder.close)
                if error is None:
                    error = self.recorder.error
                self._report_recording(self.recorder)
            await self.close_outputs()
            self._finish(failed_early=error is not None and self.tracker.total_messages == 0)
        if error is not None:
            log.error("%s", error)
            return 1
        return 0

    @staticmethod
    def _report_recording(recorder: Recorder) -> None:
        if recorder.bytes_written:
            log.info(
                "recorded %.0f MB (%.0f s of samples) to %s",
                recorder.bytes_written / 1e6,
                recorder.bytes_written / (2 * SAMPLE_RATE),
                os.path.abspath(recorder.path),
            )
        if recorder.dropped_blocks:
            log.warning("the disk was too slow: %d blocks are missing from the recording", recorder.dropped_blocks)

    def _finish(self, failed_early: bool = False) -> None:
        if self.config.raw_stdout or failed_early:
            return
        try:
            self.out.write(format_summary(self.tracker, self.rate.rate, self._extra_status()) + "\n")
            if self.config.stats:
                self.out.write(format_stats(self.demod_stats, self.tracker, self.dropped_blocks, self.config.demod) + "\n")
            self.out.flush()
        except (OSError, ValueError):  # stdout is gone
            pass

    # -- data path --------------------------------------------------------------------------

    def _write_raw_stdout(self, text: str) -> None:
        try:
            self.out.write(text)
            self.out.flush()
        except (OSError, ValueError):  # whoever reads our output has gone away
            self._raw_stdout = False
            log.info("standard output was closed; stopping")
            if self.pipeline is not None:
                self.pipeline.stop()

    def _on_batch(self, batch: Batch) -> None:
        raw, beast, sbs = self.servers.get("raw"), self.servers.get("beast"), self.servers.get("sbs")
        send_raw = raw is not None and raw.clients > 0
        send_beast = beast is not None and beast.clients > 0
        send_sbs = sbs is not None and sbs.clients > 0
        lines = []
        for msg in batch.messages:
            update = self.tracker.update(msg)
            if send_raw:
                raw.broadcast(encode_raw(msg))
            if send_beast:
                beast.broadcast(encode_beast(msg))
            if send_sbs:
                line = self.sbs.format(msg, update)
                if line is not None:
                    sbs.broadcast(line.encode("ascii"))
            if self._raw_stdout:
                lines.append(f"*{msg.hex};\n")
        if lines:
            self._write_raw_stdout("".join(lines))

        self.demod_stats = batch.stats
        self.dropped_blocks = batch.dropped_blocks
        self.levels.update(batch.now, batch.noise, [msg.rssi for msg in batch.messages])
        if batch.now - self._last_expire >= 1.0:
            self._last_expire = batch.now
            self.tracker.expire(batch.now)
        if self._deadline is not None and batch.now >= self._deadline and self.pipeline is not None:
            self.pipeline.stop()

    def _tick(self, now: float) -> None:
        """Once a second: refresh the message rate and warn about the two common live problems."""
        self.rate.update(self.tracker.total_messages, now)
        if self.source.live and not self._warned_silent and now - self._started > SILENCE_WARNING_S and self.tracker.total_messages == 0:
            self._warned_silent = True
            log.warning(
                "no ADS-B messages received yet: check the antenna, the gain, and that no other program is using the dongle"
            )
        if self.dropped_blocks > self._last_dropped and now - self._last_drop_warning > DROP_WARNING_INTERVAL_S:
            self._last_drop_warning = now
            hint = " (--demod classic needs less CPU)" if self.config.demod == "mlse" else ""
            log.warning("the CPU cannot keep up: %d sample blocks dropped so far%s", self.dropped_blocks, hint)
        self._last_dropped = self.dropped_blocks

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            self._tick(time.monotonic())
