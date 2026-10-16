"""Synthesise RTL-SDR style 8-bit IQ (2 MS/s) from Mode S frames.

Each frame is built from 0.5 us chips (preamble + PPM data), band-limited like the
receiver's front end, sampled at a chosen sub-sample timing phase and scaled to a
given peak amplitude (ADC counts).  Frames add together as complex baseband
signals with random carrier phases, so overlapping transmissions garble each other
like real ones do.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import numpy as np

SAMPLE_RATE = 2_000_000
PREAMBLE_CHIPS = (1, 0, 1, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0)
_PAD = 4  # silent chips rendered before and after a frame


def frame_chips(frame: bytes) -> np.ndarray:
    """0.5 us chips (1.0 / 0.0) of the preamble followed by the PPM encoded frame."""
    bits = np.unpackbits(np.frombuffer(frame, dtype=np.uint8))
    chips = np.empty(bits.size * 2, dtype=np.float32)
    chips[0::2] = bits
    chips[1::2] = 1 - bits
    return np.concatenate([np.array(PREAMBLE_CHIPS, dtype=np.float32), chips])


class Channel:
    """Band-limited channel; `cutoff_hz` is the one-sided complex baseband cutoff."""

    def __init__(self, cutoff_hz: float = 1.0e6, oversample: int = 8):
        self.cutoff_hz = cutoff_hz
        self.oversample = oversample
        fs_hi = SAMPLE_RATE * oversample
        half = 3 * oversample
        n = np.arange(-half, half + 1)
        kernel = np.sinc(2.0 * cutoff_hz / fs_hi * n) * np.blackman(2 * half + 1)
        self.kernel = (kernel / kernel.sum()).astype(np.float32)

    def render(self, frame: bytes, phase: int) -> np.ndarray:
        """Envelope sampled at 2 MS/s. Index `_PAD` is the first preamble chip.

        `phase` (0..oversample-1) is the sampling instant inside a chip in units of
        1/oversample chip: 0 samples on a chip edge (worst), oversample/2 at its centre (best).
        """
        over = self.oversample
        chips = frame_chips(frame)
        up = np.zeros((chips.size + 2 * _PAD) * over, dtype=np.float32)
        up[_PAD * over : (_PAD + chips.size) * over] = np.repeat(chips, over)
        filtered = np.convolve(up, self.kernel, mode="same")
        return filtered[phase % over :: over].astype(np.float32)


@dataclass(eq=False)
class Burst:
    """One transmission starting at stream sample `sample` (first preamble chip)."""

    sample: int
    frame: bytes
    amplitude: float
    phase: int = 4
    carrier: float = 0.0
    _env: np.ndarray | None = field(default=None, repr=False)

    @property
    def length(self) -> int:
        return len(self.frame) * 16 + 16 + 2 * _PAD

    @property
    def first(self) -> int:
        """Stream index of the first rendered sample."""
        return self.sample - _PAD

    @property
    def end(self) -> int:
        return self.first + self.length

    def envelope(self, channel: Channel) -> np.ndarray:
        if self._env is None:
            self._env = channel.render(self.frame, self.phase)
        return self._env


class IqSynthesizer:
    """Turns bursts into cu8 blocks; stateless apart from the noise generator."""

    def __init__(self, noise_sigma: float = 2.0, channel: Channel | None = None, seed: int | None = None):
        self.noise_sigma = noise_sigma
        self.channel = channel or Channel()
        self.rng = np.random.default_rng(seed)

    def make_burst(
        self, sample: int, frame: bytes, amplitude: float, phase: int | None = None
    ) -> Burst:
        if phase is None:
            phase = int(self.rng.integers(0, self.channel.oversample))
        return Burst(sample, frame, amplitude, phase, float(self.rng.uniform(0, 2 * np.pi)))

    def synthesize(self, start: int, count: int, bursts: Iterable[Burst] = ()) -> bytes:
        """cu8 bytes for stream samples [start, start+count)."""
        sig = np.zeros(count, dtype=np.complex64)
        for burst in bursts:
            lo, hi = max(burst.first, start), min(burst.end, start + count)
            if lo >= hi:
                continue
            env = burst.envelope(self.channel)[lo - burst.first : hi - burst.first]
            sig[lo - start : hi - start] += env * (burst.amplitude * np.exp(1j * burst.carrier))
        noise = self.rng.standard_normal((count, 2), dtype=np.float32) * np.float32(self.noise_sigma)
        out = np.empty((count, 2), dtype=np.float32)
        out[:, 0] = sig.real + noise[:, 0]
        out[:, 1] = sig.imag + noise[:, 1]
        out += 127.5
        np.clip(out, 0, 255, out=out)
        return np.rint(out).astype(np.uint8).tobytes()


def render_bursts(
    frames: Sequence[tuple[int, bytes, float]],
    total_samples: int,
    *,
    noise_sigma: float = 2.0,
    seed: int | None = 0,
    phase: int | None = None,
    channel: Channel | None = None,
) -> bytes:
    """Convenience: one cu8 buffer holding (start_sample, frame, amplitude) transmissions."""
    synth = IqSynthesizer(noise_sigma, channel, seed)
    bursts = [synth.make_burst(s, f, a, phase) for s, f, a in frames]
    return synth.synthesize(0, total_samples, bursts)
