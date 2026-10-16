"""Console output: ANSI aircraft table, periodic one-line summaries and a statistics report."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
from collections.abc import Callable
from typing import TextIO

from ..demod import SAMPLE_RATE
from ..tracker import Aircraft, Tracker

_COLUMNS = (
    ("ICAO", 6, "<"),
    ("Flight", 8, "<"),
    ("Cat", 3, "<"),
    ("Alt(ft)", 7, ">"),
    ("GS(kt)", 6, ">"),
    ("Trk", 3, ">"),
    ("VRate", 6, ">"),
    ("Lat", 9, ">"),
    ("Lon", 10, ">"),
    ("Dist(NM)", 8, ">"),
    ("Brg", 3, ">"),
    ("Sqwk", 4, ">"),
    ("Msgs", 6, ">"),
    ("Seen", 5, ">"),
    ("RSSI", 6, ">"),
)
HEADER = "  " + " ".join(f"{name:{align}{width}}" for name, width, align in _COLUMNS)

CLEAR_SCREEN = "\x1b[2J"
HOME = "\x1b[H"
CLEAR_LINE = "\x1b[K"
CLEAR_BELOW = "\x1b[J"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"


def enable_ansi(stream: TextIO) -> bool:
    """True when `stream` is a terminal that understands ANSI escapes (enables them on Windows)."""
    try:
        if not stream.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11 if stream is sys.stdout else -12)
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def _sort_key(item: tuple[Aircraft, tuple[float, float] | None]):
    ac, located = item
    return (located is None, located[0] if located else 0.0, -ac.messages, ac.icao)


def aircraft_rows(tracker: Tracker) -> list[str]:
    """Table body, nearest aircraft first (aircraft without a position last)."""
    now = tracker.now
    items = [(ac, tracker.distance_bearing(ac)) for ac in tracker.visible()]
    items.sort(key=_sort_key)
    rows = []
    for ac, located in items:
        if ac.on_ground:
            altitude = "GND"
        else:
            altitude = "" if ac.alt_baro is None else str(ac.alt_baro)
        cells = (
            ac.hex,
            ac.callsign or "",
            ac.category or "",
            altitude,
            "" if ac.gs is None else f"{ac.gs:.0f}",
            "" if ac.track is None else f"{ac.track:.0f}",
            "" if ac.vrate is None else f"{ac.vrate:+d}",
            f"{ac.lat:.5f}" if ac.has_position() else "",
            f"{ac.lon:.5f}" if ac.has_position() else "",
            f"{located[0]:.1f}" if located else "",
            f"{located[1]:.0f}" if located else "",
            ac.squawk or "",
            str(ac.messages),
            f"{max(0.0, now - ac.last_seen):.0f}",
            "" if ac.rssi is None else f"{ac.rssi:.1f}",
        )
        marker = "!" if ac.emergency_state else " "
        rows.append(marker + " " + " ".join(f"{cell:{align}{width}}"[:width] for cell, (_, width, align) in zip(cells, _COLUMNS)))
    return rows


def render_frame(tracker: Tracker, status: str, width: int = 120, height: int = 30) -> str:
    """Full-screen table that fits `width` x `height` (without a trailing newline)."""
    body = aircraft_rows(tracker)
    room = max(0, height - 4)
    lines = [status[:width], "", HEADER[:width]]
    lines += [row[:width] for row in body[:room]]
    if len(body) > room:
        lines.append(f"... {len(body) - room} more aircraft (enlarge the window to see them)"[:width])
    return "\n".join(lines)


def format_summary(tracker: Tracker, rate: float, extra: str = "") -> str:
    visible = tracker.visible()
    located = sum(1 for ac in visible if ac.has_position())
    parts = [
        time.strftime("%H:%M:%S"),
        f"aircraft {len(visible)} ({located} with position)",
        f"{rate:.0f} msg/s, total {tracker.total_messages}",
    ]
    if tracker.receiver is not None:
        parts.append(f"max range {tracker.max_distance_nm:.1f} NM")
    if extra:
        parts.append(extra)
    return " | ".join(parts)


def format_stats(demod_stats: dict, tracker: Tracker, dropped_blocks: int = 0, demod: str = "classic") -> str:
    samples = demod_stats.get("samples", 0)
    visible = tracker.visible()
    located = sum(1 for ac in visible if ac.has_position())
    by_df = [demod_stats.get(key, 0) for key in ("df17", "df18", "df11", "other_df")]
    rows = [
        ("Samples processed", f"{samples:,} ({samples / SAMPLE_RATE:.1f} s)"),
        ("Preambles detected", f"{demod_stats.get('preambles', 0):,}"),
        (
            "Valid frames",
            f"{sum(by_df):,} (DF17 {by_df[0]:,}, DF18 {by_df[1]:,}, DF11 {by_df[2]:,}, other {by_df[3]:,})",
        ),
        ("  of which error-corrected", f"{demod_stats.get('fixed', 0):,}"),
    ]
    if demod == "mlse":
        rows += [
            ("  of which soft-decision-corrected", f"{demod_stats.get('soft_fixed', 0):,}"),
            ("  of which found by a runner-up timing", f"{demod_stats.get('retimed', 0):,}"),
        ]
    else:
        rows.append(("  of which phase-corrected", f"{demod_stats.get('phase_corrected', 0):,}"))
    rows += [
        ("Sample gaps / dropped blocks", f"{demod_stats.get('gaps', 0)} / {dropped_blocks}"),
        ("Messages tracked", f"{tracker.total_messages:,}"),
        ("Aircraft now", f"{len(visible)} ({located} with position)"),
        ("Positions decoded", f"{tracker.positions:,} (rejected {tracker.rejected_positions})"),
    ]
    if tracker.receiver is not None:
        rows.append(("Maximum range", f"{tracker.max_distance_nm:.1f} NM"))
    width = max(len(name) for name, _ in rows)
    return "\n".join(f"{name:<{width}} : {value}" for name, value in rows)


class Console:
    """Periodically shows the aircraft table (ANSI terminals) or one-line summaries."""

    def __init__(
        self,
        tracker: Tracker,
        status: Callable[[], str],
        *,
        rate: Callable[[], float],
        table: bool,
        out: TextIO | None = None,
        interval: float = 1.0,
        summary_interval: float = 10.0,
    ):
        self.tracker = tracker
        self.status = status
        self.rate = rate  # messages per second, measured by whoever owns the clock
        self.out = out or sys.stdout
        self.table = table and enable_ansi(self.out)
        self.interval = interval
        self.summary_interval = summary_interval
        self._last_summary = float("-inf")

    def draw(self) -> None:
        if self.table:
            line = format_summary(self.tracker, self.rate(), self.status())
            size = shutil.get_terminal_size((120, 30))
            frame = render_frame(self.tracker, line, size.columns - 1, size.lines)
            text = HOME + frame.replace("\n", CLEAR_LINE + "\n") + CLEAR_LINE + "\n" + CLEAR_BELOW
            self.out.write(text)
            self.out.flush()
            return
        now = time.monotonic()
        if now - self._last_summary >= self.summary_interval:
            self._last_summary = now
            self.out.write(format_summary(self.tracker, self.rate(), self.status()) + "\n")
            self.out.flush()

    async def run(self) -> None:
        if self.table:
            self.out.write(HIDE_CURSOR + CLEAR_SCREEN)
        try:
            while True:
                self.draw()
                await asyncio.sleep(self.interval)
        finally:
            if self.table:
                self.out.write(SHOW_CURSOR + "\n")
                self.out.flush()
