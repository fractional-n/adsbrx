"""Received signal level and noise floor over time, for the Web UI.

All levels are in dBFS: 0 dBFS is a full-scale I/Q signal, the unit of the per-aircraft
signal strength.  Times are the stream clock of the sample source.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from collections.abc import Iterable
from typing import NamedTuple


class _Second(NamedTuple):
    t: int  # start of the second (stream clock)
    noise: float | None  # median of the blocks' noise floors, dBFS
    frames: int
    power: float  # summed linear power of the frames (1.0 = full scale)
    peak: float | None  # strongest frame, dBFS


class _Bin:
    """Accumulates one second of blocks."""

    def __init__(self, t: int):
        self.t = t
        self.noise: list[float] = []
        self.frames = 0
        self.power = 0.0
        self.peak: float | None = None

    def add(self, noise: float | None, rssi: Iterable[float]) -> None:
        if noise is not None and math.isfinite(noise):
            self.noise.append(noise)
        for value in rssi:
            if math.isfinite(value):
                self.frames += 1
                self.power += 10.0 ** (value / 10.0)
                if self.peak is None or value > self.peak:
                    self.peak = value

    def close(self) -> _Second:
        noise = statistics.median(self.noise) if self.noise else None
        return _Second(self.t, noise, self.frames, self.power, self.peak)


def _mean_db(power: float, frames: int) -> float | None:
    return 10.0 * math.log10(power / frames) if frames and power > 0.0 else None


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


class LevelMeter:
    """The noise floor and the level of the received frames, per second and over a short window.

    `update()` is fed once per block of samples with the block's noise floor and the signal
    strength of the frames decoded in it.  `reading()` summarises the last `window` seconds
    (the frame level is a power mean, so a few strong frames do not hide the typical one);
    `history` seconds of per-second values are kept for the chart.
    """

    def __init__(self, window: int = 10, history: int = 300):
        self.window = window
        self._history: deque[_Second] = deque(maxlen=history)
        self._current: _Bin | None = None
        self._now: float | None = None

    def update(self, now: float, noise: float | None, rssi: Iterable[float] = ()) -> None:
        second = math.floor(now)
        current = self._current
        if current is None:
            current = self._current = _Bin(second)
        elif second > current.t:
            self._history.append(current.close())
            current = self._current = _Bin(second)
        # a clock that steps back is counted in the current second
        current.add(noise, rssi)
        self._now = now if self._now is None else max(self._now, now)

    def reading(self) -> dict:
        """Noise floor, mean and peak signal level and their difference over the last `window` seconds."""
        current = self._current
        seconds: list[_Second] = []
        if current is not None:
            first = current.t - self.window + 1
            for second in reversed(self._history):
                if second.t < first:
                    break
                seconds.append(second)
            seconds.append(current.close())
        noises = [s.noise for s in seconds if s.noise is not None]
        peaks = [s.peak for s in seconds if s.peak is not None]
        frames = sum(s.frames for s in seconds)
        noise = statistics.median(noises) if noises else None
        signal = _mean_db(sum(s.power for s in seconds), frames)
        return {
            "window": self.window,
            "noise": _round(noise),
            "signal": _round(signal),
            "peak": _round(max(peaks, default=None)),
            "snr": _round(signal - noise if signal is not None and noise is not None else None),
            "messages": frames,
        }

    def document(self, since: float | None = None) -> dict:
        """`reading()` plus the finished seconds after `since` as [t, noise, signal, peak, frames]."""
        history = [
            [s.t, _round(s.noise), _round(_mean_db(s.power, s.frames)), _round(s.peak), s.frames]
            for s in self._history
            if since is None or s.t > since
        ]
        return {"now": None if self._now is None else round(self._now, 1), **self.reading(), "history": history}
