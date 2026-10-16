"""Compare the demodulators on synthetic weak signals: ``python -m adsbrx.sim.bench``.

Mode S frames from a pool of aircraft are rendered at several peak amplitudes (ADC counts) and
sub-sample timing phases into Gaussian noise of a fixed level.  The stream starts with strong
frames so that every demodulator learns the addresses (error repair only accepts addresses it has
seen).  The report lists, per demodulator, the share of frames recovered at each amplitude, its
speed, and its false frames: frames that were never sent, counted on the signal stream and on a
separate noise-only stream.

The front end is a simulation (see sim.modulator), not a measurement of a real dongle.  Use the
numbers to compare demodulators with each other, not to predict absolute sensitivity.
"""

from __future__ import annotations

import argparse
import bisect
import math
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

from ..app import Config, build_demodulator
from ..demod import SAMPLE_RATE, Demodulator
from ..mlse import MlseDemodulator
from ..sources import BLOCK_SAMPLES
from .encoder import build_df17
from .modulator import Burst, Channel, IqSynthesizer

DEFAULT_AMPLITUDES = (24.0, 30.0, 36.0, 40.0, 48.0, 60.0, 80.0, 100.0)
SPACING = 700  # samples between frames: a frame is 248 samples long, so they never overlap
PRIMER_AMPLITUDE = 127.0
SIGNAL_POOL = 30  # aircraft in the signal stream
NOISE_POOL = 300  # aircraft known while listening to noise: more addresses, more chances for a false repair
MARGIN = 2048  # no burst starts earlier than this many samples before the block it reaches into


@dataclass(frozen=True)
class Spec:
    sample: int
    frame: bytes
    amplitude: float
    phase: int
    carrier: float


@dataclass
class Stream:
    blocks: list[bytes]
    sent: dict[bytes, float | None]  # frame -> peak amplitude; None for the strong frames teaching addresses
    samples: int
    lead: int = 0  # speed and candidate counts ignore the blocks starting before this sample

    @property
    def seconds(self) -> float:
        return self.samples / SAMPLE_RATE


class _Builder:
    """Collects the bursts of a stream, then renders them block by block."""

    def __init__(self, seed: int, phases: int):
        self.rng = np.random.default_rng(seed)
        self.phases = phases
        self.specs: list[Spec] = []
        self.sent: dict[bytes, float | None] = {}
        self.position = 4 * SPACING

    def add(self, icao: int, amplitude: float, phase: int, tag: float | None) -> None:
        frame = build_df17(icao, int.from_bytes(self.rng.bytes(7), "big"))
        carrier = float(self.rng.uniform(0.0, 2.0 * np.pi))
        self.specs.append(Spec(self.position, frame, amplitude, phase, carrier))
        self.sent[frame] = tag
        self.position += SPACING

    def primer(self, pool: int) -> list[int]:
        icaos = [int(x) for x in self.rng.integers(0x100000, 0xFFFFFF, pool)]
        for icao in icaos:
            self.add(icao, PRIMER_AMPLITUDE, self.phases // 2, None)
        return icaos

    def render(self, extra: int, sigma: float, channel: Channel, seed: int, lead: int = 0) -> Stream:
        total = self.position + extra
        synth = IqSynthesizer(sigma, channel, seed)
        starts = [spec.sample for spec in self.specs]
        blocks = []
        for start in range(0, total, BLOCK_SAMPLES):
            count = min(BLOCK_SAMPLES, total - start)
            first = bisect.bisect_left(starts, start - MARGIN)
            last = bisect.bisect_left(starts, start + count + MARGIN)
            bursts = [Burst(s.sample, s.frame, s.amplitude, s.phase, s.carrier) for s in self.specs[first:last]]
            blocks.append(synth.synthesize(start, count, bursts))
        return Stream(blocks, self.sent, total, lead)


def signal_stream(
    frames: int, amplitudes: Sequence[float], phases: int, sigma: float, channel: Channel, seed: int
) -> Stream:
    """`frames` frames for every (amplitude, timing phase) pair, in random order."""
    builder = _Builder(seed, phases)
    icaos = builder.primer(SIGNAL_POOL)
    cells = [(a, p) for a in amplitudes for p in range(phases) for _ in range(frames)]
    for index in builder.rng.permutation(len(cells)):
        amplitude, phase = cells[int(index)]
        icao = icaos[int(builder.rng.integers(len(icaos)))]
        builder.add(icao, float(amplitude), phase, float(amplitude))
    return builder.render(2 * SPACING, sigma, channel, seed + 1)


def noise_stream(seconds: float, phases: int, sigma: float, channel: Channel, seed: int) -> Stream:
    """Strong frames from many aircraft, then only noise (which is all that is timed)."""
    builder = _Builder(seed + 2, phases)
    builder.primer(NOISE_POOL)
    return builder.render(int(seconds * SAMPLE_RATE), sigma, channel, seed + 3, lead=builder.position)


# ------------------------------------------------------------------------------ measurement


@dataclass(frozen=True)
class Variant:
    label: str
    make: Callable[[], Demodulator]


def variants() -> list[Variant]:
    def shipped(demod: str, fix_bits: int) -> Callable[[], Demodulator]:
        return lambda: build_demodulator(Config(demod=demod, fix_bits=fix_bits))

    return [
        Variant("classic --no-fix", shipped("classic", 0)),
        Variant("classic", shipped("classic", 1)),
        Variant("classic --aggressive", shipped("classic", 2)),
        Variant("mlse --no-fix", shipped("mlse", 0)),
        Variant("mlse, no soft repair", lambda: MlseDemodulator(fix_bits=1, soft_flips=0)),
        Variant("mlse (default)", shipped("mlse", 1)),
        Variant("mlse --aggressive", shipped("mlse", 2)),
    ]


@dataclass
class Row:
    label: str
    rates: list[float]  # frames decoded per amplitude [%]
    dense_msps: float  # speed on the signal stream
    idle_msps: float  # speed on the noise-only stream
    candidates_per_s: float  # preamble candidates per second of noise
    false_frames: int

    @property
    def mean(self) -> float:
        return sum(self.rates) / len(self.rates)


@dataclass
class Report:
    amplitudes: tuple[float, ...]
    sigma: float
    cutoff_mhz: float
    phases: int
    frames: int
    signal_seconds: float
    noise_seconds: float
    rows: list[Row]


@dataclass
class Decoded:
    frames: list[bytes]
    msps: float  # speed over the timed blocks
    candidates_per_s: float  # preamble candidates per second over the timed blocks


def _decode(demod: Demodulator, stream: Stream) -> Decoded:
    frames: list[bytes] = []
    start = timed_samples = candidates = 0
    timed_s = 0.0
    for block in stream.blocks:
        before = demod.stats.preambles
        began = time.perf_counter()
        frames.extend(d.frame for d in demod.process(block, start))
        elapsed = time.perf_counter() - began
        if start >= stream.lead:
            timed_s += elapsed
            candidates += demod.stats.preambles - before
            timed_samples += len(block) // 2
        start += len(block) // 2
    timed_s = max(timed_s, 1e-9)
    return Decoded(frames, timed_samples / timed_s / 1e6, candidates / max(timed_samples / SAMPLE_RATE, 1e-9))


def measure(variant: Variant, signal: Stream, noise: Stream, amplitudes: Sequence[float]) -> Row:
    dense = _decode(variant.make(), signal)
    got = set(dense.frames)
    rates = []
    for amplitude in amplitudes:
        wanted = [frame for frame, tag in signal.sent.items() if tag == amplitude]
        rates.append(100.0 * sum(frame in got for frame in wanted) / len(wanted))
    idle = _decode(variant.make(), noise)
    false = sum(frame not in signal.sent for frame in dense.frames)
    false += sum(frame not in noise.sent for frame in idle.frames)
    return Row(variant.label, rates, dense.msps, idle.msps, idle.candidates_per_s, false)


def run_benchmark(
    *,
    frames: int = 40,
    amplitudes: Sequence[float] = DEFAULT_AMPLITUDES,
    sigma: float = 8.5,
    phases: int = 12,
    cutoff_mhz: float = 1.0,
    noise_seconds: float = 10.0,
    seed: int = 1,
    chosen: Sequence[Variant] | None = None,
) -> Report:
    channel = Channel(cutoff_hz=cutoff_mhz * 1e6, oversample=phases)
    amplitudes = tuple(float(a) for a in amplitudes)
    signal = signal_stream(frames, amplitudes, phases, sigma, channel, seed)
    noise = noise_stream(noise_seconds, phases, sigma, channel, seed)
    rows = [measure(v, signal, noise, amplitudes) for v in (chosen if chosen is not None else variants())]
    return Report(amplitudes, sigma, cutoff_mhz, phases, frames, signal.seconds, noise_seconds, rows)


# ------------------------------------------------------------------------------ report


def peak_snr_db(amplitude: float, sigma: float) -> float:
    """Peak signal power over the noise power of one complex sample (two components of rms `sigma`)."""
    return 10.0 * math.log10(amplitude * amplitude / (2.0 * sigma * sigma))


def format_report(report: Report) -> str:
    width = max(len(row.label) for row in report.rows)
    total = len(report.amplitudes) * report.phases * report.frames
    lines = [
        f"Weak-signal benchmark (simulation): noise {report.sigma:g} counts rms per I/Q branch, "
        f"front end {report.cutoff_mhz:g} MHz wide",
        f"{total:,} frames ({report.phases} timing phases x {report.frames} per amplitude) in "
        f"{report.signal_seconds:.1f} s of signal, plus {report.noise_seconds:g} s of noise only",
        "",
        "Frames decoded [%]",
        f"{'peak amplitude [counts]':<{width}}" + "".join(f"{a:7g}" for a in report.amplitudes) + "    mean",
        f"{'peak SNR [dB]':<{width}}" + "".join(f"{peak_snr_db(a, report.sigma):7.1f}" for a in report.amplitudes),
    ]
    for row in report.rows:
        lines.append(f"{row.label:<{width}}" + "".join(f"{rate:7.1f}" for rate in row.rates) + f"{row.mean:8.1f}")
    lines += [
        "",
        "Cost (speed in MS/s: the receiver needs 2.0)",
        f"{'':<{width}}   dense   noise  candidates/s  false frames",
    ]
    for row in report.rows:
        lines.append(
            f"{row.label:<{width}}{row.dense_msps:8.1f}{row.idle_msps:8.1f}{row.candidates_per_s:14.0f}{row.false_frames:14d}"
        )
    lines += [
        "",
        "dense: the signal stream, a frame every 350 us (far busier than real traffic); "
        "noise: only noise (an idle band);",
        "candidates/s: preamble candidates per second of noise; "
        "false frames: frames decoded that were never sent, in both streams.",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------------------ command line


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"invalid value {text!r} (a positive whole number)")
    return value


def _positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"invalid value {text!r} (a positive number)")
    return value


def _amplitudes(text: str) -> tuple[float, ...]:
    values = tuple(_positive_float(part) for part in text.split(",") if part.strip())
    if not values or len(set(values)) != len(values):
        raise argparse.ArgumentTypeError(f"invalid amplitudes {text!r} (distinct numbers separated by commas)")
    return values


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m adsbrx.sim.bench", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--frames", type=_positive_int, default=40, metavar="N", help="frames per amplitude and timing phase (default 40)")
    parser.add_argument(
        "--amplitudes",
        type=_amplitudes,
        default=DEFAULT_AMPLITUDES,
        metavar="A,B,...",
        help="peak amplitudes in ADC counts (default %s)" % ",".join(f"{a:g}" for a in DEFAULT_AMPLITUDES),
    )
    parser.add_argument("--sigma", type=_positive_float, default=8.5, help="noise rms per I/Q branch in counts (default 8.5)")
    parser.add_argument("--phases", type=_positive_int, default=12, help="timing phases to test (default 12)")
    parser.add_argument("--cutoff", type=_positive_float, default=1.0, metavar="MHZ", help="front end bandwidth in MHz (default 1.0)")
    parser.add_argument("--noise-seconds", type=_positive_float, default=10.0, metavar="S", help="length of the noise-only stream (default 10)")
    parser.add_argument("--seed", type=int, default=1, help="random seed (default 1)")
    args = parser.parse_args(argv)

    print(
        format_report(
            run_benchmark(
                frames=args.frames,
                amplitudes=args.amplitudes,
                sigma=args.sigma,
                phases=args.phases,
                cutoff_mhz=args.cutoff,
                noise_seconds=args.noise_seconds,
                seed=args.seed,
            )
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
