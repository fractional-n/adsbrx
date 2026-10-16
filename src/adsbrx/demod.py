"""Vectorised Mode S demodulator for 2 MS/s unsigned 8-bit IQ.

The algorithm follows the classic dump1090 2 MS/s design (preamble relations on
magnitude samples, PPM slicing, phase-corrected retry), but works on whole blocks
with numpy instead of looping sample by sample.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from .crc import error_table, syndromes

SAMPLE_RATE = 2_000_000
PREAMBLE_SAMPLES = 16
LONG_BITS = 112
SHORT_BITS = 56
FULL_SAMPLES = PREAMBLE_SAMPLES + 2 * LONG_BITS
NEED = FULL_SAMPLES + 2  # samples that must follow a candidate before it can be judged
FULL_SCALE = 127.5  # magnitude of a full-scale sample, the 0 dBFS reference
NOISE_STRIDE = 32  # the noise floor is measured on every 32nd sample of a block
NOISE_MIN_SAMPLES = 64  # fewer samples than this (after striding) are too few to measure anything
_LN2 = math.log(2.0)

_ADDR_SHORT_DF = (0, 4, 5)
_ADDR_LONG_DF = (16, 20, 21)
_EMPTY_U32 = np.empty(0, dtype=np.uint32)


def _build_magnitude_lut() -> np.ndarray:
    v = np.arange(65536, dtype=np.uint32)
    i = (v & 0xFF).astype(np.float32) - 127.5
    q = (v >> 8).astype(np.float32) - 127.5
    return np.hypot(i, q).astype(np.float32)


MAG_LUT = _build_magnitude_lut()  # indexed by little endian (I | Q << 8)


@dataclass(slots=True)
class Detection:
    frame: bytes
    sample: int  # absolute index of the first preamble sample
    rssi: float  # mean power over the frame in dBFS
    corrected: int = 0
    phase_corrected: bool = False


@dataclass(slots=True)
class DemodStats:
    samples: int = 0
    preambles: int = 0
    valid_crc: int = 0
    fixed: int = 0
    phase_corrected: int = 0
    df11: int = 0
    df17: int = 0
    df18: int = 0
    other_df: int = 0
    gaps: int = 0
    soft_fixed: int = 0  # frames repaired with soft decisions (mlse demodulator only; also counted in `fixed`)
    retimed: int = 0  # frames found with a runner-up timing hypothesis (mlse demodulator only)

    def as_dict(self) -> dict:
        return asdict(self)


def magnitude(block) -> np.ndarray:
    """float32 |I+jQ| of interleaved unsigned 8-bit samples."""
    if isinstance(block, np.ndarray):
        block = np.ascontiguousarray(block, dtype=np.uint8)
    raw = np.frombuffer(block, dtype="<u2", count=len(block) // 2)
    return MAG_LUT[raw]


def _icao_of(frames: np.ndarray, row: int) -> int:
    return (int(frames[row, 1]) << 16) | (int(frames[row, 2]) << 8) | int(frames[row, 3])


class Demodulator:
    """Stateful block demodulator.

    `process()` takes consecutive blocks of cu8 samples and returns the validated
    frames found in them.  Frames that straddle a block boundary are completed with
    the next block.  Frames needing error correction (or address/parity frames when
    `all_df` is set) are only accepted for ICAO addresses seen in a clean frame during
    the last `icao_ttl_s` seconds, which keeps false positives negligible.

    Subclasses replace the two stages of a scan: `_candidates` (where might a frame start?)
    and `_decode` (what do the samples after each candidate say?).
    """

    _history = 1  # samples kept before the first candidate position of the next block

    def __init__(
        self,
        fix_bits: int = 1,
        all_df: bool = False,
        icao_ttl_s: float = 60.0,
        threshold: float = 0.7,
    ):
        self.fix_bits = fix_bits
        self.all_df = all_df
        self.threshold = threshold
        self._ttl = int(icao_ttl_s * SAMPLE_RATE)
        self._tail = np.empty(0, dtype=np.float32)
        self._tail_base = 0
        self._next = 0
        self._skip_until = 0
        self._icao: dict[int, int] = {}
        self.stats = DemodStats()
        self.noise_dbfs: float | None = None  # noise floor of the latest block (see `_measure_noise`)

    def reset(self) -> None:
        self._tail = np.empty(0, dtype=np.float32)

    def process(self, block, start: int | None = None) -> list[Detection]:
        """Demodulate one block. `start` is the absolute index of its first sample."""
        mag = magnitude(block)
        self._measure_noise(mag)
        if start is not None and start != self._next:
            if self._next:
                self.stats.gaps += 1
            self.reset()
            self._tail_base = start
            self._next = start
        self.stats.samples += mag.size
        if self._tail.size:
            mag = np.concatenate((self._tail, mag))
        base = self._tail_base
        self._next = base + mag.size

        lo = self._history
        hi = mag.size - NEED
        if hi <= lo:
            self._tail = mag
            return []
        found = self._scan(mag, base, lo, hi)
        keep = hi - lo  # index 0 of the next buffer is the history for its first candidate
        self._tail = mag[keep:].copy()
        self._tail_base = base + keep
        return found

    # -- internals ------------------------------------------------------------------------

    def _measure_noise(self, mag: np.ndarray) -> None:
        """Noise floor of a block in dBFS (0 dBFS = full-scale I/Q).

        Frames fill only a few percent of the samples, so the median magnitude is the
        noise's median.  For Rayleigh distributed noise the mean power is median² / ln 2.
        """
        sample = mag[::NOISE_STRIDE]
        if sample.size < NOISE_MIN_SAMPLES:
            return
        median = float(np.median(sample))
        power = max(median * median / _LN2, 1e-3)
        self.noise_dbfs = 10.0 * math.log10(power / (FULL_SCALE * FULL_SCALE))

    def _scan(self, mag: np.ndarray, base: int, lo: int, hi: int) -> list[Detection]:
        pos = self._candidates(mag, lo, hi)
        if self._skip_until > base + lo:
            pos = pos[pos + base >= self._skip_until]
        if pos.size == 0:
            return []
        self.stats.preambles += pos.size
        self._expire(base + hi)

        accepted = self._decode(mag, pos, base)

        detections: list[Detection] = []
        skip = self._skip_until
        for row in sorted(accepted, key=lambda r: pos[r]):
            frame, fixed, phased = accepted[row]
            first = int(pos[row])
            if base + first < skip:
                continue
            nsamples = PREAMBLE_SAMPLES + 16 * len(frame)
            skip = base + first + nsamples
            span = mag[first : first + nsamples].astype(np.float64)
            rssi = 10.0 * np.log10(max(float(np.mean(span * span)), 1e-3) / (127.5 * 127.5))
            if phased:
                self.stats.phase_corrected += 1
            detections.append(Detection(frame, base + first, rssi, fixed, phased))
            self._count(frame)
        self._skip_until = skip
        return detections

    def _candidates(self, mag: np.ndarray, lo: int, hi: int) -> np.ndarray:
        """Indices in [lo, hi) of samples that may be the first preamble sample of a frame."""
        return self._preamble_candidates(mag, lo, hi)

    def _decode(self, mag: np.ndarray, pos: np.ndarray, base: int) -> dict[int, tuple[bytes, int, bool]]:
        """Decode the frame behind each candidate. Returns {row: (frame, corrected bits, phase retried)}."""
        data = sliding_window_view(mag, FULL_SAMPLES)[pos, PREAMBLE_SAMPLES:]
        accepted: dict[int, tuple[bytes, int, bool]] = {}
        for row, (frame, fixed) in self._classify(self._slice(data), pos, base).items():
            accepted[row] = (frame, fixed, False)

        left = np.ones(pos.size, dtype=bool)
        left[list(accepted)] = False
        left = np.flatnonzero(left)
        if left.size:
            p = pos[left]
            out_of_phase = (
                (mag[p + 3] > mag[p + 2] / 3)
                | (mag[p + 10] > mag[p + 9] / 3)
                | (mag[p + 6] > mag[p + 7] / 3)
                | (mag[p - 1] > mag[p + 1] / 3)
            )
            retry = left[out_of_phase]
            if retry.size:
                bits = self._slice(self._phase_correct(data[retry]))
                for sub, (frame, fixed) in self._classify(bits, pos[retry], base).items():
                    accepted[int(retry[sub])] = (frame, fixed, True)
        return accepted

    @staticmethod
    def _preamble_candidates(mag: np.ndarray, lo: int, hi: int) -> np.ndarray:
        count = hi - lo

        def at(k: int) -> np.ndarray:
            return mag[lo + k : lo + k + count]

        m0, m1, m2, m3, m6, m7, m8, m9 = at(0), at(1), at(2), at(3), at(6), at(7), at(8), at(9)
        ok = m0 > m1
        ok &= m1 < m2
        ok &= m2 > m3
        ok &= m3 < m0
        ok &= at(4) < m0
        ok &= at(5) < m0
        ok &= m6 < m0
        ok &= m7 > m8
        ok &= m8 < m9
        ok &= m9 > m6
        high = (m0 + m2 + m7 + m9) * np.float32(1.0 / 6.0)
        for k in (4, 5, 11, 12, 13, 14):
            ok &= at(k) < high
        return np.flatnonzero(ok) + lo

    def _expire(self, now: int) -> None:
        cutoff = now - self._ttl
        if self._icao and min(self._icao.values()) < cutoff:
            self._icao = {k: v for k, v in self._icao.items() if v >= cutoff}

    def _count(self, frame: bytes) -> None:
        df = frame[0] >> 3
        if df == 17:
            self.stats.df17 += 1
        elif df == 18:
            self.stats.df18 += 1
        elif df == 11:
            self.stats.df11 += 1
        else:
            self.stats.other_df += 1

    def _slice(self, data: np.ndarray) -> np.ndarray:
        """PPM slicing: bit = (first chip > second chip); near ties repeat the previous bit."""
        first = data[:, 0 : 2 * LONG_BITS : 2]
        second = data[:, 1 : 2 * LONG_BITS : 2]
        delta = first - second
        bits = delta > 0
        weak = np.abs(delta) < self.threshold
        weak[:, 0] = False
        for i in np.flatnonzero(weak.any(axis=0)):
            bits[:, i] = np.where(weak[:, i], bits[:, i - 1], bits[:, i])
        return bits

    @staticmethod
    def _phase_correct(data: np.ndarray) -> np.ndarray:
        """dump1090 style retry for out of phase signals: scale the next bit's first chip."""
        data = data.copy()
        for i in range(LONG_BITS - 1):
            one = data[:, 2 * i] > data[:, 2 * i + 1]
            data[:, 2 * i + 2] *= np.where(one, np.float32(1.25), np.float32(0.8))
        return data

    def _classify(
        self, bits: np.ndarray, pos: np.ndarray, base: int
    ) -> dict[int, tuple[bytes, int]]:
        """Validate sliced candidates. Returns {row: (frame, corrected bit count)}."""
        frames = np.packbits(bits, axis=1)
        df = frames[:, 0] >> 3
        accepted: dict[int, tuple[bytes, int]] = {}

        long_rows = np.flatnonzero((df == 17) | (df == 18))
        short_rows = np.flatnonzero(df == 11)
        syn_long = syndromes(frames[long_rows]) if long_rows.size else _EMPTY_U32
        syn_short = syndromes(frames[short_rows, :7]) if short_rows.size else _EMPTY_U32

        for rows, syn, nbytes in ((long_rows, syn_long, 14), (short_rows, syn_short, 7)):
            for row in rows[syn == 0]:
                row = int(row)
                accepted[row] = (frames[row, :nbytes].tobytes(), 0)
                self._icao[_icao_of(frames, row)] = base + int(pos[row])
                self.stats.valid_crc += 1

        # DF11 replies to selective interrogations carry the interrogator id in the parity
        for row in short_rows[(syn_short > 0) & (syn_short < 80)]:
            row = int(row)
            icao = _icao_of(frames, row)
            if icao in self._icao:
                accepted[row] = (frames[row, :7].tobytes(), 0)
                self._icao[icao] = base + int(pos[row])

        if self._icao:
            if self.fix_bits > 0:
                self._fix_rows(frames, long_rows, syn_long, 14, (17, 18), accepted, pos, base)
                self._fix_rows(frames, short_rows, syn_short, 7, (11,), accepted, pos, base)
            if self.all_df:
                self._address_rows(frames, df, accepted)
        return accepted

    def _fix_rows(self, frames, rows, syn, nbytes, allowed_df, accepted, pos, base) -> None:
        if rows.size == 0:
            return
        table = error_table(nbytes * 8, self.fix_bits)
        hit, idx = table.lookup(syn)
        for j in np.flatnonzero(hit):
            row = int(rows[j])
            if row in accepted:
                continue
            flips = table.positions[int(idx[j])]
            fixed = bytearray(frames[row, :nbytes].tobytes())
            for bit in flips:
                fixed[bit >> 3] ^= 0x80 >> (bit & 7)
            icao = int.from_bytes(fixed[1:4], "big")
            if (fixed[0] >> 3) in allowed_df and icao in self._icao:
                accepted[row] = (bytes(fixed), len(flips))
                self._icao[icao] = base + int(pos[row])
                self.stats.fixed += 1

    def _address_rows(self, frames, df, accepted) -> None:
        """DF0/4/5/16/20/21: the CRC remainder is the address, valid only if already known."""
        known = np.fromiter(self._icao, dtype=np.int64, count=len(self._icao))
        for dfs, nbytes in ((_ADDR_SHORT_DF, 7), (_ADDR_LONG_DF, 14)):
            rows = np.flatnonzero(np.isin(df, dfs))
            if rows.size == 0:
                continue
            addr = syndromes(frames[rows, :nbytes])
            for row in rows[np.isin(addr, known)]:
                accepted[int(row)] = (frames[row, :nbytes].tobytes(), 0)
