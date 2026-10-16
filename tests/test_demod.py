import numpy as np
import pytest

from adsbrx.demod import Demodulator, magnitude
from adsbrx.sim.encoder import (
    build_df11,
    build_df17,
    build_surveillance,
    encode_airborne_position,
    encode_ident,
    encode_ac13,
    with_parity,
)
from adsbrx.sim.modulator import IqSynthesizer


def random_df17(rng, n):
    frames = []
    for _ in range(n):
        icao = int(rng.integers(0x100000, 0xFFFFFF))
        if rng.integers(0, 2):
            me = encode_airborne_position(
                int(rng.integers(0, 1 << 17)), int(rng.integers(0, 1 << 17)), bool(rng.integers(0, 2)), int(rng.integers(0, 40000))
            )
        else:
            me = encode_ident("ANA%03d" % rng.integers(0, 999), 3)
        frames.append(build_df17(icao, me))
    return frames


def render(frames, amp=60.0, phase=4, spacing=700, sigma=2.0, seed=5, start=400):
    synth = IqSynthesizer(sigma, seed=seed)
    bursts, t = [], start
    for f in frames:
        bursts.append(synth.make_burst(t, f, amp, phase))
        t += spacing
    return synth.synthesize(0, t + 600, bursts), [b.sample for b in bursts]


def run(iq, block=131072, **kw):
    demod = Demodulator(**kw)
    out = []
    step = block * 2
    for i in range(0, len(iq), step):
        out.extend(demod.process(iq[i : i + step], start=i // 2))
    return out, demod


def test_magnitude_matches_definition():
    raw = bytes([127, 128, 255, 0, 0, 255])
    mag = magnitude(raw)
    expect = np.hypot(np.array([127, 255, 0]) - 127.5, np.array([128, 0, 255]) - 127.5)
    assert np.allclose(mag, expect)


@pytest.mark.parametrize("phase", [1, 2, 3, 4, 5, 6])
def test_clean_frames_decode_at_all_good_phases(phase):
    rng = np.random.default_rng(phase)
    frames = random_df17(rng, 80)
    iq, starts = render(frames, amp=60, phase=phase)
    got, demod = run(iq)
    assert [d.frame for d in got] == frames
    assert demod.stats.valid_crc == len(frames)
    for det, start in zip(got, starts):
        assert abs(det.sample - start) <= 1


@pytest.mark.parametrize("block", [333, 1000, 4096, 65537, 1 << 20])
def test_block_boundaries_do_not_lose_or_duplicate_frames(block):
    rng = np.random.default_rng(11)
    frames = random_df17(rng, 60)
    iq, _ = render(frames, amp=70, phase=4, spacing=517)
    ref, _ = run(iq, block=1 << 22)
    got, _ = run(iq, block=block)
    assert [(d.sample, d.frame) for d in got] == [(d.sample, d.frame) for d in ref]
    assert [d.frame for d in got] == frames


def test_noise_only_gives_no_frames():
    synth = IqSynthesizer(2.0, seed=9)
    demod = Demodulator()
    frames = 0
    for i in range(30):
        frames += len(demod.process(synth.synthesize(i * 65536, 65536), start=i * 65536))
    assert frames == 0
    assert demod.stats.samples == 30 * 65536


def test_rssi_tracks_amplitude():
    rng = np.random.default_rng(3)
    frames = random_df17(rng, 10)
    weak, _ = run(render(frames, amp=30)[0])
    strong, _ = run(render(frames, amp=90)[0])
    mean_weak = np.mean([d.rssi for d in weak])
    mean_strong = np.mean([d.rssi for d in strong])
    assert mean_strong - mean_weak == pytest.approx(20 * np.log10(3), abs=1.0)
    assert -15 < mean_strong < 0


def test_single_bit_error_needs_known_icao():
    icao = 0x840ABC
    good = build_df17(icao, encode_ident("JAL123", 3))
    bad = bytearray(good)
    bad[6] ^= 0x10  # flip one data bit, CRC no longer matches
    bad = bytes(bad)

    # without a prior clean frame from the same aircraft the frame must be rejected
    got, _ = run(render([bad])[0])
    assert got == []

    # after a clean frame (known address) it is repaired
    got, demod = run(render([good, bad], spacing=900)[0])
    assert [d.frame for d in got] == [good, good]
    assert [d.corrected for d in got] == [0, 1]
    assert demod.stats.fixed == 1

    got, _ = run(render([good, bad], spacing=900)[0], fix_bits=0)
    assert [d.frame for d in got] == [good]


def test_two_bit_error_only_with_aggressive_mode():
    icao = 0x840ABD
    good = build_df17(icao, encode_ident("ANA777", 3))
    bad = bytearray(good)
    bad[5] ^= 0x04
    bad[9] ^= 0x40
    bad = bytes(bad)
    iq = render([good, bad], spacing=900)[0]
    assert [d.frame for d in run(iq, fix_bits=1)[0]] == [good]
    got = run(iq, fix_bits=2)[0]
    assert [d.frame for d in got] == [good, good]
    assert got[1].corrected == 2


def test_df11_with_interrogator_id_requires_known_icao():
    icao = 0x840111
    allcall = build_df11(icao)
    selective = with_parity(allcall[:4], address=5)  # interrogator id 5 overlaid on parity
    assert run(render([selective])[0])[0] == []
    got, demod = run(render([allcall, selective], spacing=900)[0])
    assert [d.frame for d in got] == [allcall, selective]
    assert demod.stats.df11 == 2


def test_address_parity_frames_only_in_mode_s_mode():
    icao = 0x840222
    ident = build_df17(icao, encode_ident("SKY001", 3))
    df4 = build_surveillance(4, icao, encode_ac13(12000))
    df20 = build_surveillance(20, icao, encode_ac13(12000))
    stranger = build_surveillance(4, 0x123456, encode_ac13(5000))
    iq = render([ident, df4, df20, stranger], spacing=900)[0]
    assert [d.frame for d in run(iq)[0]] == [ident]
    got = run(iq, all_df=True)[0]
    assert [d.frame for d in got] == [ident, df4, df20]


def test_overlapping_frames_never_produce_false_frames():
    rng = np.random.default_rng(21)
    frames = random_df17(rng, 40)
    iq, _ = render(frames, amp=60, phase=4, spacing=120)  # heavy overlap
    got, _ = run(iq)
    assert {d.frame for d in got} <= set(frames)


def test_stream_gap_is_counted_and_state_recovers():
    rng = np.random.default_rng(31)
    frames = random_df17(rng, 6)
    iq, _ = render(frames, spacing=700)
    half = (len(iq) // 4) * 2
    demod = Demodulator()
    first = demod.process(iq[:half], start=0)
    rest = demod.process(iq[half:], start=len(iq) // 2 + 10_000)  # 10 000 samples were dropped
    assert demod.stats.gaps == 1
    assert len(first) + len(rest) >= len(frames) - 2
    again = demod.process(iq[:half], start=50_000_000)
    assert demod.stats.gaps == 2
    assert all(d.sample >= 50_000_000 for d in again)


# ---------------------------------------------------------------------------------- noise floor


def expected_noise_dbfs(sigma):
    """Mean power of I/Q noise with `sigma` per component, relative to a full-scale (127.5) sample."""
    return 10 * np.log10(2 * sigma**2 / 127.5**2)


def test_noise_floor_is_unknown_until_a_block_has_been_seen():
    assert Demodulator().noise_dbfs is None


@pytest.mark.parametrize("sigma", [4.0, 8.5, 20.0, 40.0])
def test_noise_floor_matches_the_power_of_the_noise(sigma):
    demod = Demodulator()
    demod.process(IqSynthesizer(sigma, seed=3).synthesize(0, 131072), start=0)
    assert demod.noise_dbfs == pytest.approx(expected_noise_dbfs(sigma), abs=0.5)


def test_noise_floor_follows_the_latest_block():
    demod = Demodulator()
    demod.process(IqSynthesizer(4.0, seed=3).synthesize(0, 131072), start=0)
    quiet = demod.noise_dbfs
    demod.process(IqSynthesizer(16.0, seed=4).synthesize(0, 131072), start=131072)
    assert demod.noise_dbfs - quiet == pytest.approx(20 * np.log10(16 / 4), abs=0.5)


def test_noise_floor_is_not_raised_much_by_the_frames_in_the_block():
    rng = np.random.default_rng(3)
    iq, _ = render(random_df17(rng, 40), amp=60, sigma=8.5, spacing=3000)  # frames use about 8 % of the time
    _, demod = run(iq)
    assert demod.noise_dbfs == pytest.approx(expected_noise_dbfs(8.5), abs=0.75)


def test_noise_floor_needs_a_few_thousand_samples():
    synth = IqSynthesizer(8.5, seed=3)
    demod = Demodulator()
    demod.process(synth.synthesize(0, 2016), start=0)  # every 32nd sample: 63, too few
    assert demod.noise_dbfs is None
    demod.process(synth.synthesize(2016, 2048), start=2016)
    assert demod.noise_dbfs == pytest.approx(expected_noise_dbfs(8.5), abs=2.0)
    before = demod.noise_dbfs
    demod.process(IqSynthesizer(40.0, seed=5).synthesize(0, 1000), start=4064)  # too short: leaves it alone
    assert demod.noise_dbfs == before


def test_noise_floor_of_a_silent_block_is_finite():
    demod = Demodulator()
    demod._measure_noise(np.zeros(4096, dtype=np.float32))
    assert demod.noise_dbfs is not None and -80 < demod.noise_dbfs < -60
