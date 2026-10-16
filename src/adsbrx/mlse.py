"""Sequence-detecting demodulator for 2 MS/s Mode S magnitudes (the default, `--demod mlse`).

The classic demodulator decides every bit by comparing two neighbouring magnitude samples.  At
2 MS/s there is exactly one sample per 0.5 us chip, and where the sample clock happens to fall
inside a chip decides how much of the band-limited pulse a sample sees.  Close to a chip edge the
comparison breaks down at moderate signal strength, so a quarter of all timing phases is hardly
decodable.  This demodulator keeps the 2 MS/s input but decodes it differently:

1. A matched filter on sample energies, evaluated for a grid of sub-sample timing phases at once,
   finds the preambles.
2. Every timing-phase template is fitted to the preamble samples by weighted least squares.  That
   gives the timing phase and the amplitude of the burst, and candidates that do not look like a
   preamble (a large misfit for their signal strength) are dropped.
3. A two-state Viterbi search picks the 112 bits whose expected samples are closest to the
   received magnitudes.  The expectation comes from a template per (timing phase, three-chip
   context), which models the ringing of the receiver's band-limiting filter.  A frame whose first
   bit is 0 (DF < 16) has only 56 bits: the search for those ends after bit 55 and ignores the
   silence behind it.
4. The best few timing hypotheses are tried in turn until a CRC matches.  If none does, the least
   reliable bits of the most likely hypothesis are flipped (Chase decoding), but only for
   aircraft that were already seen in a clean frame, exactly like the classic bit repair.  A
   table of the syndromes that so few bit errors can produce spares hopeless candidates that
   search.

The templates assume a front-end bandwidth (`cutoff_hz`); a moderate mismatch costs little.
"""

from __future__ import annotations

import functools
import itertools

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from .crc import syndromes
from .demod import FULL_SAMPLES, LONG_BITS, PREAMBLE_SAMPLES, SAMPLE_RATE, SHORT_BITS, Demodulator

PREAMBLE_CHIPS = (1, 0, 1, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0)
DATA_CHIPS = 2 * LONG_BITS

_FIT = 16  # preamble samples k = -1 .. 14 take part in detection and timing/amplitude fit
_RADIUS = 4  # a candidate must be the best score within this many samples
_ROWS = 2048  # candidates per Viterbi batch (bounds memory)
_SCORE_ROWS = 16384  # positions per matched-filter batch
_CHASE_ELEMS = 1 << 22  # bound on the pattern syndromes computed at once
_REACH_FLIPS = 4  # the syndrome pre-filter is tabulated for up to this many bit errors; more would be too large
_NOISE_STRETCH = 256  # samples per stretch for the noise estimate
_QUIET_POWER = 2.0 * 0.947  # mean |noise|^2 is 2 sigma^2; the 20th percentile of 256-sample means is 0.947 of it
_RICE = np.pi / 2  # E|noise| = sigma * sqrt(pi / 2)
_MIN_SIGMA = 1.0  # a noise-free input must not make the detector divide by zero
_FIT_FLOOR = 50.0  # preamble chi-square limit: _FIT_FLOOR + _FIT_SLOPE * (amplitude / sigma)^2
_FIT_SLOPE = 0.5  # real preambles drift from the template slowly with signal strength, data does not
_SNR_STEP = 2.0**0.5  # the weighted timing fit uses weights for amplitude / sigma = _SNR_STEP ** level
_SNR_LEVELS = 14  # up to _SNR_STEP ** 13, about 90 (a full-scale signal over 1.4 counts of noise)
_PAD = 4  # silent chips around the training frames


# -------------------------------------------------------------------------------------------
# templates
# -------------------------------------------------------------------------------------------


def _de_bruijn(order: int) -> list[int]:
    """Binary sequence in which every `order`-bit pattern occurs exactly once (cyclically)."""
    a = [0] * (2 * order)
    seq: list[int] = []

    def build(t: int, p: int) -> None:
        if t > order:
            if order % p == 0:
                seq.extend(a[1 : p + 1])
            return
        a[t] = a[t - p]
        build(t + 1, p)
        for j in range(a[t - p] + 1, 2):
            a[t] = j
            build(t + 1, t)

    build(1, 1)
    return seq


def _ppm(bits) -> np.ndarray:
    bits = np.asarray(bits, dtype=np.float64)
    chips = np.empty(bits.size * 2)
    chips[0::2] = bits
    chips[1::2] = 1.0 - bits
    return chips


def _envelope(chips: np.ndarray, phases: int, cutoff_hz: float) -> np.ndarray:
    """Band-limited chip train on a grid of `phases` points per chip, `_PAD` silent chips either side."""
    half = 3 * phases
    n = np.arange(-half, half + 1)
    kernel = np.sinc(2.0 * cutoff_hz / (SAMPLE_RATE * phases) * n) * np.blackman(2 * half + 1)
    kernel /= kernel.sum()
    up = np.zeros((chips.size + 2 * _PAD) * phases)
    up[_PAD * phases : (_PAD + chips.size) * phases] = np.repeat(chips, phases)
    return np.convolve(up, kernel, mode="same")


class Bank:
    """Noise-free sample values (at unit amplitude) for every sub-sample timing phase.

    `data[p, c]` is the expected magnitude of a sample taken at timing phase `p` (0 = on the chip
    edge, phases/2 = at its centre) when the chip it falls in and its two neighbours are
    `c = previous * 4 + current * 2 + next`.  `pre[p, k + 1]` is the expected magnitude at the
    k-th preamble sample (k = -1 .. 14).  Both are averages over uniformly random message bits.
    """

    def __init__(self, cutoff_hz: float = 1.0e6, phases: int = 12):
        if phases < 4:
            raise ValueError("at least 4 timing phases are needed")
        self.cutoff_hz = float(cutoff_hz)
        self.phases = phases
        lead = len(PREAMBLE_CHIPS)

        # Data chips: a de Bruijn sequence makes every bit pattern around a chip equally frequent.
        bits = _de_bruijn(6) * 3
        chips = np.concatenate([np.array(PREAMBLE_CHIPS, dtype=np.float64), _ppm(bits)])
        env = _envelope(chips, phases, cutoff_hz)
        padded = np.concatenate(([0.0], chips, [0.0]))
        ctx = (padded[:-2] * 4 + padded[1:-1] * 2 + padded[2:]).astype(np.int64)
        data = np.zeros((phases, 8))
        middle = np.arange(lead + 128, lead + 256)  # the second copy of the sequence
        start = np.arange(lead)  # the preamble supplies the silent contexts data never has
        for p in range(phases):
            samples = env[(_PAD + np.arange(chips.size)) * phases + p]
            known = np.zeros(8, dtype=bool)
            for rows in (middle, start):
                total = np.bincount(ctx[rows], samples[rows], minlength=8)
                count = np.bincount(ctx[rows], minlength=8)
                fill = (count > 0) & ~known
                data[p, fill] = total[fill] / count[fill]
                known |= fill
        self.data = data

        # Preamble samples depend on the first data bit only.
        pre = np.zeros((phases, _FIT))
        for first in (0, 1):
            chips = np.concatenate([np.array(PREAMBLE_CHIPS, dtype=np.float64), _ppm([first, 0, 1, 0, 1, 0, 1, 0])])
            env = _envelope(chips, phases, cutoff_hz)
            for p in range(phases):
                pre[p] += 0.5 * env[(_PAD - 1 + np.arange(_FIT)) * phases + p]
        self.pre = pre

        energy = pre * pre
        centred = energy - energy.mean(axis=1, keepdims=True)
        self.energy = energy  # (P, 16)
        self.energy_t = np.ascontiguousarray(energy.T, dtype=np.float32)  # (16, P)
        self.energy_sq = (energy * energy).sum(axis=1)  # (P,)
        # matched filter on energies, unit norm per phase: scores are in noise standard deviations
        unit = centred / np.sqrt((centred * centred).sum(axis=1, keepdims=True))
        self.matched = np.ascontiguousarray(unit, dtype=np.float32)  # (P, 16)

        # Weighted timing fit.  In units of sigma^2 an energy sample of the template at amplitude
        # snr has variance 4 (1 + snr^2 E), so the weights depend on the amplitude; they are
        # tabulated for a ladder of amplitudes and every candidate uses the closest rung.
        snr = _SNR_STEP ** np.arange(_SNR_LEVELS)
        inv = 1.0 / (4.0 * (1.0 + snr[:, None, None] ** 2 * energy[None]))  # (L, P, 16)
        self.fit_weight = np.ascontiguousarray(inv.transpose(0, 2, 1), dtype=np.float32)  # (L, 16, P)
        self.fit_product = np.ascontiguousarray((inv * energy[None]).transpose(0, 2, 1), dtype=np.float32)
        self.fit_norm = (inv * energy[None] ** 2).sum(axis=2)  # (L, P)


@functools.lru_cache(maxsize=8)
def get_bank(cutoff_hz: float = 1.0e6, phases: int = 12) -> Bank:
    return Bank(cutoff_hz, phases)


# -------------------------------------------------------------------------------------------
# Viterbi search
# -------------------------------------------------------------------------------------------


def viterbi(data: np.ndarray, last: np.ndarray, mu: np.ndarray, reliability: bool = False):
    """Most likely 112 message bits for each row.

    `data` (R, 224) are the magnitudes of the 224 data chips, `last` (R,) the final preamble
    sample and `mu` (R, 8) the expected magnitude per three-chip context.  The metric is the sum of
    squared differences, so the best path is the maximum likelihood one for equal noise.
    Returns (bits (R, 112) uint8, cost (R,), reliability (R, 112) or None).  The reliability of a
    bit is how much worse the best path with that bit flipped is (max-log-MAP).

    A message whose first bit is 0 (downlink format below 16) has 56 bits, and the 56 chips after
    them are silence, not data.  Left to the search they would be decoded as data bits too, and
    where the last pulse leaks into the next chip they can pull the final bit of the message to
    the wrong side.  Such rows therefore have their first 56 bits (and reliabilities) re-decided
    with the trellis ending there; the bits after them are meaningless.  `cost` is that of the
    full 112 bit path.
    """
    rows = data.shape[0]
    d = np.ascontiguousarray(data.T, dtype=np.float32)  # (224, R)
    m = np.ascontiguousarray(mu.T, dtype=np.float32)  # (8, R)
    last = np.asarray(last, dtype=np.float32)

    def expect(prev: int, cur: int, nxt: int) -> np.ndarray:
        return m[prev * 4 + cur * 2 + nxt]

    # Branch j joins bit j (state u) to bit j + 1 (state v).  Bit j's second chip is sample
    # 2j + 1 (value 1 - u, between chip u and the first chip of the next bit, v); the first chip
    # of bit j + 1 is sample 2j + 2 (value v, between 1 - u and 1 - v).
    second, first = d[1:222:2], d[2:224:2]
    branch = np.empty((111, 2, 2, rows), dtype=np.float32)
    for u in (0, 1):
        for v in (0, 1):
            a = second - expect(u, 1 - u, v)
            b = first - expect(1 - u, v, 1 - v)
            branch[:, u, v] = a * a + b * b
    # Bit 0 starts after the last (silent) preamble chip; the last data chip is followed by silence.
    start = np.stack(
        [(last - expect(0, 0, v)) ** 2 + (d[0] - expect(0, v, 1 - v)) ** 2 for v in (0, 1)]
    )
    end = np.stack([(d[223] - expect(u, 1 - u, 0)) ** 2 for u in (0, 1)])

    alpha = np.empty((112, 2, rows), dtype=np.float32)
    alpha[0] = start
    came_from_one = np.empty((111, 2, rows), dtype=np.bool_)
    best = start
    for j in range(111):
        total = best[:, None, :] + branch[j]  # (u, v, R)
        pick = total[1] < total[0]
        best = np.where(pick, total[1], total[0])
        came_from_one[j] = pick
        alpha[j + 1] = best
    final = best + end
    state = final.argmin(axis=0)
    cost = final.min(axis=0)

    bits = np.empty((rows, LONG_BITS), dtype=np.uint8)
    index = np.arange(rows)
    bits[:, 111] = state
    for j in range(110, -1, -1):
        state = came_from_one[j][state, index].astype(np.intp)
        bits[:, j] = state

    # The forward pass up to bit 55 does not depend on what follows; only the termination changes.
    short = np.flatnonzero(bits[:, 0] == 0)
    if short.size:
        last_bit = SHORT_BITS - 1
        end_short = np.stack(
            [(d[2 * SHORT_BITS - 1, short] - expect(u, 1 - u, 0)[short]) ** 2 for u in (0, 1)]
        )
        state = (alpha[last_bit][:, short] + end_short).argmin(axis=0)
        bits[short, last_bit] = state
        for j in range(last_bit - 1, -1, -1):
            state = came_from_one[j][state, short].astype(np.intp)
            bits[short, j] = state

    if not reliability:
        return bits, cost, None
    rel = np.empty((112, rows), dtype=np.float32)
    beta = end
    for j in range(111, -1, -1):
        through = alpha[j] + beta
        rel[j] = np.abs(through[0] - through[1])
        if j:
            joined = branch[j - 1] + beta[None, :, :]
            beta = np.minimum(joined[:, 0], joined[:, 1])
    if short.size:
        beta = end_short
        for j in range(SHORT_BITS - 1, -1, -1):
            through = alpha[j][:, short] + beta
            rel[j, short] = np.abs(through[0] - through[1])
            if j:
                joined = branch[j - 1][:, :, short] + beta[None, :, :]
                beta = np.minimum(joined[:, 0], joined[:, 1])
    return bits, cost, np.ascontiguousarray(rel.T)


# -------------------------------------------------------------------------------------------
# soft-decision (Chase) repair
# -------------------------------------------------------------------------------------------


@functools.lru_cache(maxsize=4)
def _unit_syndromes(nbits: int) -> np.ndarray:
    eye = np.zeros((nbits, nbits // 8), dtype=np.uint8)
    pos = np.arange(nbits)
    eye[pos, pos >> 3] = 0x80 >> (pos & 7)
    return syndromes(eye)


@functools.lru_cache(maxsize=16)
def _combinations(window: int, count: int) -> np.ndarray:
    return np.array(list(itertools.combinations(range(window), count)), dtype=np.intp).reshape(-1, count)


@functools.lru_cache(maxsize=4)
def _reachable(nbits: int, flips: int) -> np.ndarray:
    """Bit table over all 2**24 syndromes: set for those that at most `flips` bit errors can cause."""
    unit = _unit_syndromes(nbits)
    table = np.zeros(1 << 24, dtype=np.bool_)
    table[0] = True
    values, top = unit, np.arange(nbits)
    for size in range(1, flips + 1):
        table[values] = True
        if size == flips:
            break
        # extend every pattern by one more bit above its highest
        parts = [values[top < j] ^ unit[j] for j in range(1, nbits)]
        values = np.concatenate(parts)
        if size + 1 < flips:
            top = np.concatenate([np.full(part.size, j) for j, part in enumerate(parts, 1)])
    return np.packbits(table, bitorder="little")


def repairable(frames: np.ndarray, flips: int) -> np.ndarray:
    """Which (R, nbytes) frames could be a valid frame with at most `flips` bits wrong.

    A necessary condition for `chase` to find something, and a table lookup instead of a search.
    Frames that are valid already do not count.
    """
    syn = syndromes(frames)
    packed = _reachable(frames.shape[1] * 8, flips)
    return (((packed[syn >> 3] >> (syn & 7)) & 1) != 0) & (syn != 0)


def chase(frames: np.ndarray, rel: np.ndarray, window: int, flips: int) -> list[list[tuple[float, tuple[int, ...]]]]:
    """Bit flips that turn each frame into a CRC-clean one, cheapest first.

    `frames` is (R, nbytes) uint8, `rel` (R, >= 8 * nbytes) the reliability of every bit.  Only
    patterns of up to `flips` flips among the `window` least reliable bits are tried; the cost of a
    pattern is the summed reliability of the flipped bits.  Frames that already pass are skipped.
    The caller has to judge whether a repaired frame is plausible.
    """
    count, nbytes = frames.shape
    nbits = nbytes * 8
    found: list[list[tuple[float, tuple[int, ...]]]] = [[] for _ in range(count)]
    if count == 0 or window < 1 or flips < 1:
        return found
    window = min(window, nbits)
    syn = syndromes(frames)
    broken = syn != 0
    order = np.argpartition(rel[:, :nbits], window - 1, axis=1)[:, :window]
    unit = _unit_syndromes(nbits)[order]  # (R, window)
    for size in range(1, min(flips, window) + 1):
        combo = _combinations(window, size)
        step = max(1, _CHASE_ELEMS // combo.size)
        for a in range(0, count, step):
            part = np.bitwise_xor.reduce(unit[a : a + step][:, combo], axis=2)
            hit_r, hit_c = np.nonzero((part == syn[a : a + step, None]) & broken[a : a + step, None])
            for r, c in zip(hit_r.tolist(), hit_c.tolist()):
                cols = order[a + r, combo[c]]
                found[a + r].append((float(rel[a + r, cols].sum()), tuple(int(v) for v in cols)))
    for options in found:
        options.sort()
    return found


# -------------------------------------------------------------------------------------------
# demodulator
# -------------------------------------------------------------------------------------------


class MlseDemodulator(Demodulator):
    """Drop-in replacement for `Demodulator` using sequence detection (see the module docstring).

    `phases` timing phases per chip are modelled, `hypotheses` of them are decoded per candidate,
    and `soft_flips` (at most, among the `soft_window` least reliable bits) soft repairs are tried
    when `fix_bits` > 0.  `min_snr` drops candidates whose fitted amplitude is below that many
    noise sigmas: such frames cannot be decoded and would only cost time.  `fit_floor` is the
    preamble misfit that is tolerated at any signal strength (it grows a little with it).  `sigma`
    fixes the noise level (counts per I/Q component) instead of tracking it.
    """

    _history = 1 + _RADIUS

    def __init__(
        self,
        fix_bits: int = 1,
        all_df: bool = False,
        icao_ttl_s: float = 60.0,
        threshold: float = 0.7,
        *,
        cutoff_hz: float = 1.0e6,
        phases: int = 12,
        hypotheses: int = 3,
        soft_window: int = 12,
        soft_flips: int = 3,
        detect_threshold: float = 5.0,
        min_snr: float = 2.5,
        fit_floor: float = _FIT_FLOOR,
        sigma: float | None = None,
    ):
        super().__init__(fix_bits, all_df, icao_ttl_s, threshold)
        if not 1 <= hypotheses <= phases:
            raise ValueError("hypotheses must be between 1 and the number of phases")
        self.bank = get_bank(cutoff_hz, phases)
        self.hypotheses = hypotheses
        self.soft_window = soft_window
        self.soft_flips = soft_flips
        self.detect_threshold = detect_threshold
        self.min_snr = min_snr
        self.fit_floor = fit_floor
        self._fixed_sigma = sigma
        self.sigma = max(float(sigma), _MIN_SIGMA) if sigma is not None else None

    # -- noise level ---------------------------------------------------------------------------

    def _track_noise(self, mag: np.ndarray) -> None:
        """Noise sigma from the quietest fifth of short stretches, so busy skies do not bias it."""
        if self._fixed_sigma is not None:
            return
        size = min(_NOISE_STRETCH, mag.size)
        count = mag.size // size
        stretches = mag[: count * size].reshape(count, size)
        power = np.einsum("ij,ij->i", stretches, stretches, dtype=np.float64) / size
        quiet = float(np.partition(power, count // 5)[count // 5])
        estimate = (quiet / _QUIET_POWER) ** 0.5
        self.sigma = max(estimate if self.sigma is None else 0.9 * self.sigma + 0.1 * estimate, _MIN_SIGMA)

    # -- candidates ----------------------------------------------------------------------------

    def _candidates(self, mag: np.ndarray, lo: int, hi: int) -> np.ndarray:
        self._track_noise(mag)
        var = np.float32(self.sigma * self.sigma)
        first, stop = lo - _RADIUS, hi + _RADIUS
        count = stop - first
        # position n is scored on mag[n - 1 : n + 15]
        seg = mag[first - 1 : stop + _FIT - 2]
        energy = seg * seg
        energy -= 2 * var
        score = np.empty(count, dtype=np.float32)
        stack = np.empty((_FIT, min(_SCORE_ROWS, count)), dtype=np.float32)
        for a in range(0, count, _SCORE_ROWS):
            n = min(_SCORE_ROWS, count - a)
            for k in range(_FIT):
                stack[k, :n] = energy[a + k : a + k + n]
            score[a : a + n] = (self.bank.matched @ stack[:, :n]).max(axis=0)
        score *= np.float32(0.5) / var
        idx = np.flatnonzero(score[_RADIUS : count - _RADIUS] > self.detect_threshold) + _RADIUS
        if idx.size:
            here = score[idx]
            keep = np.ones(idx.size, dtype=bool)
            for d in range(1, _RADIUS + 1):
                keep &= (here >= score[idx - d]) & (here > score[idx + d])
            idx = idx[keep]
        return idx.astype(np.int64) + first

    # -- decoding ------------------------------------------------------------------------------

    def _fit(self, mag: np.ndarray, pos: np.ndarray):
        """Timing phase hypotheses (best first), their amplitudes and the misfit of the best one.

        Every phase template is fitted to the 16 preamble energies by weighted least squares, the
        variance of an energy sample being 4 sigma^2 (sigma^2 + signal): bright samples are noisy
        and must not outvote the quiet ones, which are the ones telling the phases apart.  The
        misfit is the resulting chi-square statistic (about 14 for a genuine preamble).
        """
        bank = self.bank
        var = self.sigma**2
        windows = sliding_window_view(mag, _FIT)
        count = pos.size
        order = np.empty((count, self.hypotheses), dtype=np.intp)
        amp = np.empty((count, self.hypotheses))
        chi2 = np.empty(count)
        for a in range(0, count, _ROWS):
            pre = windows[pos[a : a + _ROWS] - 1].astype(np.float32)
            y = (pre * pre - np.float32(2.0 * var)) / np.float32(var)  # (R, 16), in units of sigma^2
            # the amplitude only has to be roughly right to pick the weights
            rough = np.sqrt(np.maximum((y @ bank.energy_t) / bank.energy_sq, 0.0).max(axis=1))
            rung = np.rint(np.log(np.maximum(rough, 1.0)) / np.log(_SNR_STEP)).astype(np.intp)
            np.clip(rung, 0, _SNR_LEVELS - 1, out=rung)
            scale = np.empty((y.shape[0], bank.phases))
            misfit = np.empty_like(scale)
            for r in np.unique(rung):
                rows = np.flatnonzero(rung == r)
                part = y[rows]
                num = (part @ bank.fit_product[r]).astype(np.float64)
                total = ((part * part) @ bank.fit_weight[r]).astype(np.float64)
                best = np.maximum(num / bank.fit_norm[r], 0.0)
                scale[rows] = best
                misfit[rows] = total - 2.0 * best * num + best * best * bank.fit_norm[r]
            best = np.argsort(misfit, axis=1)[:, : self.hypotheses]
            order[a : a + _ROWS] = best
            fitted = np.take_along_axis(scale, best, axis=1) * var  # amplitude^2 in counts^2
            amp[a : a + _ROWS] = np.sqrt(np.maximum(fitted, 1.0))
            chi2[a : a + _ROWS] = np.take_along_axis(misfit, best[:, :1], axis=1)[:, 0]
        return order, amp, chi2

    def _search(self, mag, pos, phase, amp, reliability=False):
        """Viterbi decode of the candidates at `pos` for the given timing phases and amplitudes."""
        windows = sliding_window_view(mag, FULL_SAMPLES)
        parts = []
        floor = _RICE * self.sigma**2
        for a in range(0, pos.size, _ROWS):
            rows = pos[a : a + _ROWS]
            expected = amp[a : a + _ROWS, None] * self.bank.data[phase[a : a + _ROWS]]
            mu = np.sqrt(expected * expected + floor)
            block = windows[rows]
            parts.append(viterbi(block[:, PREAMBLE_SAMPLES:], block[:, PREAMBLE_SAMPLES - 1], mu, reliability))
        bits = np.concatenate([p[0] for p in parts])
        cost = np.concatenate([p[1] for p in parts])
        rel = np.concatenate([p[2] for p in parts]) if reliability else None
        return bits, cost, rel

    @staticmethod
    def _uncovered(rows: np.ndarray, pos: np.ndarray, accepted: dict) -> np.ndarray:
        """Candidates that do not start inside (or at) an accepted frame."""
        if not accepted or rows.size == 0:
            return rows
        keys = np.fromiter(accepted, dtype=np.int64, count=len(accepted))
        starts = pos[keys]
        lengths = np.fromiter((len(accepted[int(k)][0]) for k in keys), dtype=np.int64, count=keys.size)
        ends = starts + PREAMBLE_SAMPLES + 16 * lengths
        here = pos[rows][:, None]
        return rows[~((here >= starts[None, :]) & (here < ends[None, :])).any(axis=1)]

    def _decode(self, mag, pos, base):
        accepted: dict[int, tuple[bytes, int, bool]] = {}
        order, amp, chi2 = self._fit(mag, pos)
        snr = amp[:, 0] / self.sigma
        live = np.flatnonzero((snr >= self.min_snr) & (chi2 <= self.fit_floor + _FIT_SLOPE * snr * snr))
        soft = self.fix_bits > 0 and self.soft_flips > 0
        best_cost = np.full(pos.size, np.inf, dtype=np.float32)
        best_hyp = np.zeros(pos.size, dtype=np.intp)
        best_bits = np.zeros((pos.size, LONG_BITS), dtype=np.uint8) if soft else None
        for hyp in range(self.hypotheses):
            if live.size == 0:
                break
            bits, cost, _ = self._search(mag, pos[live], order[live, hyp], amp[live, hyp])
            better = cost < best_cost[live]
            best_cost[live[better]] = cost[better]
            best_hyp[live[better]] = hyp
            if soft:
                best_bits[live[better]] = bits[better]
            for sub, (frame, fixed) in self._classify(bits, pos[live], base).items():
                accepted[int(live[sub])] = (frame, fixed, False)
                if hyp:
                    self.stats.retimed += 1
            live = self._uncovered(live, pos, accepted)
        if soft and live.size and self._icao:  # clean frames of this very block may have taught an address
            rows = live[self._repairable(best_bits[live])]
            if rows.size:
                hyp = best_hyp[rows]
                bits, _, rel = self._search(mag, pos[rows], order[rows, hyp], amp[rows, hyp], reliability=True)
                self._soft_repair(bits, rel, rows, pos, base, accepted)
        return accepted

    def _repairable(self, bits: np.ndarray) -> np.ndarray:
        """Candidates whose hard decision is at most `soft_flips` bit flips away from a valid frame."""
        if self.soft_flips > _REACH_FLIPS:
            return np.ones(bits.shape[0], dtype=bool)
        keep = np.zeros(bits.shape[0], dtype=bool)
        for nbits in (LONG_BITS, SHORT_BITS):
            keep |= repairable(np.packbits(bits[:, :nbits], axis=1), self.soft_flips)
        return keep

    def _soft_repair(self, bits, rel, rows, pos, base, accepted) -> None:
        for nbits, allowed in ((LONG_BITS, (17, 18)), (SHORT_BITS, (11,))):
            frames = np.packbits(bits[:, :nbits], axis=1)
            options = chase(frames, rel, self.soft_window, self.soft_flips)
            for sub, patterns in enumerate(options):
                row = int(rows[sub])
                if row in accepted:
                    continue
                for _, cols in patterns:
                    fixed = bytearray(frames[sub].tobytes())
                    for bit in cols:
                        fixed[bit >> 3] ^= 0x80 >> (bit & 7)
                    icao = int.from_bytes(fixed[1:4], "big")
                    if (fixed[0] >> 3) in allowed and icao in self._icao:
                        accepted[row] = (bytes(fixed), len(cols), False)
                        self._icao[icao] = base + int(pos[row])
                        self.stats.fixed += 1
                        self.stats.soft_fixed += 1
                        break
