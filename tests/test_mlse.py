"""The sequence-detecting demodulator: Viterbi search, soft-decision repair, and the decoder around them."""

import numpy as np
import pytest
from test_demod import random_df17, render

from adsbrx.demod import LONG_BITS, SAMPLE_RATE, SHORT_BITS, Demodulator
from adsbrx.mlse import Bank, MlseDemodulator, _de_bruijn, chase, get_bank, repairable, viterbi
from adsbrx.sim.encoder import (
    build_df11,
    build_df17,
    build_surveillance,
    encode_ac13,
    encode_ident,
    with_parity,
)
from adsbrx.sim.modulator import IqSynthesizer


def run(iq, block=131072, cls=MlseDemodulator, **kw):
    demod = cls(**kw)
    out = []
    step = block * 2
    for i in range(0, len(iq), step):
        out.extend(demod.process(iq[i : i + step], start=i // 2))
    return out, demod


def flip(frame, *positions):
    out = bytearray(frame)
    for p in positions:
        out[p >> 3] ^= 0x80 >> (p & 7)
    return bytes(out)


def as_rows(*frames):
    return np.frombuffer(b"".join(frames), dtype=np.uint8).reshape(len(frames), -1)


def distinct_positions(rng, count, nbits=LONG_BITS):
    return [int(p) for p in rng.choice(nbits, count, replace=False)]


def weak_stream(count, amp, sigma=8.5, phases=8, seed=7, pool=12):
    """Strong frames from `pool` aircraft, so that their addresses are known, then `count` weak ones."""
    rng = np.random.default_rng(seed)
    icaos = [int(x) for x in rng.integers(0x100000, 0xFFFFFF, pool)]
    synth = IqSynthesizer(sigma, seed=seed + 1)
    bursts, weak, t = [], [], 600
    strong = []
    for icao in icaos:
        frame = build_df17(icao, int.from_bytes(rng.bytes(7), "big"))
        bursts.append(synth.make_burst(t, frame, 127.0, 4))
        strong.append(frame)
        t += 700
    for i in range(count):
        frame = build_df17(icaos[int(rng.integers(pool))], int.from_bytes(rng.bytes(7), "big"))
        bursts.append(synth.make_burst(t, frame, amp, i % phases))
        weak.append(frame)
        t += 700
    return synth.synthesize(0, t + 600, bursts), weak, strong


# ---------------------------------------------------------------------------------- templates


def test_de_bruijn_sequence_contains_every_pattern_once():
    seq = _de_bruijn(6)
    assert len(seq) == 64
    cyclic = seq + seq[:5]
    assert len({tuple(cyclic[i : i + 6]) for i in range(64)}) == 64


def test_bank_describes_a_band_limited_pulse_train():
    bank = get_bank()
    assert bank.data.shape == (12, 8) and bank.pre.shape == (12, 16)
    assert get_bank() is bank and get_bank(1.0e6, 8) is not bank
    assert np.all(np.abs(bank.data[:, 0b000]) < 0.05)  # silence stays silent
    assert np.all(bank.data[:, 0b111] == 0)  # three pulses in a row never occur in PPM, so there is no template
    centre = bank.data[6]  # a sample at the middle of its chip
    assert centre[0b010] > 0.7 and centre[0b101] < 0.3  # sees a pulse, does not see a gap
    assert centre[0b011] > centre[0b010]  # and the pulse after it adds to it
    # the filter is symmetric in time: mirroring the timing phase swaps the neighbours
    assert np.allclose(bank.data[::-1][:, [0b001, 0b011]], bank.data[:, [0b100, 0b110]])
    assert np.allclose(bank.data[::-1][:, [0b010, 0b101]], bank.data[:, [0b010, 0b101]])
    assert np.all(bank.pre.max(axis=1) > 0.5)


def test_bad_settings_are_rejected():
    with pytest.raises(ValueError):
        Bank(phases=3)
    with pytest.raises(ValueError):
        MlseDemodulator(hypotheses=0)
    with pytest.raises(ValueError):
        MlseDemodulator(phases=6, hypotheses=7)


# ------------------------------------------------------------------------------------ viterbi


def ideal_row(bank, bits, phase, amp, tail=0):
    """What the receiver would see without noise: the data chips, the last preamble sample and the levels.

    `tail` bits' worth of silence follow the message, as they do behind a 56 bit frame.
    """
    chips = np.zeros(2 * (len(bits) + tail))
    chips[: 2 * len(bits) : 2] = bits
    chips[1 : 2 * len(bits) : 2] = 1 - np.asarray(bits)
    around = np.concatenate(([0.0], chips, [0.0]))  # the quiet chips before and after
    context = (around[:-2] * 4 + around[1:-1] * 2 + around[2:]).astype(int)
    levels = amp * bank.data[phase]
    return levels[context], levels[int(chips[0])], levels


def test_viterbi_recovers_noise_free_frames_at_every_timing_phase():
    bank = get_bank()
    rng = np.random.default_rng(2)
    bits = rng.integers(0, 2, (12, LONG_BITS))
    bits[:, 0] = 1  # long frames: DF >= 16
    parts = [ideal_row(bank, row, phase, 40.0) for phase, row in enumerate(bits)]
    data, last, mu = (np.stack(x) for x in zip(*parts))
    got, cost, rel = viterbi(data, last, mu, reliability=True)
    assert (got == bits).all()
    assert cost.max() < 1e-2
    assert rel.shape == (12, LONG_BITS) and rel.min() > 0
    only_bits = viterbi(data, last, mu)
    assert (only_bits[0] == bits).all() and only_bits[2] is None


def test_viterbi_rows_do_not_influence_each_other():
    bank = get_bank()
    rng = np.random.default_rng(3)
    parts = []
    for first_bit, phase in ((0, 1), (1, 6), (0, 9), (1, 3)):  # short and long frames side by side
        bits = rng.integers(0, 2, LONG_BITS)
        bits[0] = first_bit
        parts.append(ideal_row(bank, bits, phase, 30.0))
    noisy = [(d + rng.normal(0, 6.0, d.shape), l, m) for d, l, m in parts]
    both = viterbi(*(np.stack(x) for x in zip(*noisy)), reliability=True)
    for row, part in enumerate(noisy):
        alone = viterbi(*(np.asarray(x)[None] for x in part), reliability=True)
        assert (alone[0][0] == both[0][row]).all()
        assert np.allclose(alone[1][0], both[1][row]) and np.allclose(alone[2][0], both[2][row])


def test_viterbi_reliability_is_lowest_where_the_samples_cannot_decide():
    bank = get_bank()
    bits = np.random.default_rng(4).integers(0, 2, LONG_BITS)
    data, last, mu = ideal_row(bank, bits, 6, 40.0)
    k = 50
    data[2 * k : 2 * k + 2] = data[2 * k : 2 * k + 2].mean()  # both chips of bit k at the same level
    _, _, rel = viterbi(data[None], np.array([last]), mu[None], reliability=True)
    assert rel[0, k] == rel[0].min()
    assert rel[0, k] < 0.3 * np.median(np.delete(rel[0], k))  # the neighbouring samples still say a little


def short_rows(rng, count, phase, amp):
    """Noise-free 56 bit frames (first bit 0, so DF < 16) with silence behind them, and their bits."""
    bits = rng.integers(0, 2, (count, SHORT_BITS))
    bits[:, 0] = 0
    bits[::2, -1] = 0  # in every other frame the last chip carries a pulse that leaks into the silence
    bits[1::2, -1] = 1
    parts = [ideal_row(get_bank(), row, phase, amp, tail=SHORT_BITS) for row in bits]
    return bits, *(np.stack(x) for x in zip(*parts))


@pytest.mark.parametrize("phase", range(12))
def test_viterbi_ends_a_short_frame_after_its_56_bits(phase):
    bits, data, last, mu = short_rows(np.random.default_rng(30 + phase), 24, phase, 100.0)
    got, _, rel = viterbi(data, last, mu, reliability=True)
    assert (got[:, :SHORT_BITS] == bits).all()
    # silence is not data: the bits at the end are as certain as the ones before them
    assert rel[:, :SHORT_BITS].min() > 0.5 * np.median(rel[:, :SHORT_BITS])


def test_viterbi_ignores_what_follows_a_short_frame():
    rng = np.random.default_rng(31)
    bits, data, last, mu = short_rows(rng, 24, 5, 60.0)
    data += rng.normal(0, 2.0, data.shape)
    noisier = data.copy()
    noisier[:, 2 * SHORT_BITS :] = rng.uniform(0, 150, noisier[:, 2 * SHORT_BITS :].shape)  # whatever comes after
    a_bits, _, a_rel = viterbi(data, last, mu, reliability=True)
    b_bits, _, b_rel = viterbi(noisier, last, mu, reliability=True)
    assert (a_bits[:, :SHORT_BITS] == bits).all()
    assert (a_bits[:, :SHORT_BITS] == b_bits[:, :SHORT_BITS]).all()
    assert (a_rel[:, :SHORT_BITS] == b_rel[:, :SHORT_BITS]).all()
    assert (a_bits[:, SHORT_BITS:] != b_bits[:, SHORT_BITS:]).any()  # the rest does follow its samples


# ---------------------------------------------------------------------------- soft repair


def test_chase_finds_the_cheapest_flips():
    good = build_df17(0x840ABC, encode_ident("JAL123", 3))
    rel = np.full((1, 112), 100.0, dtype=np.float32)
    rel[0, 20], rel[0, 77] = 1.0, 2.0
    (options,) = chase(as_rows(flip(good, 20, 77)), rel, window=8, flips=2)
    assert len(options) == 1
    cost, cols = options[0]
    assert cost == pytest.approx(3.0) and set(cols) == {20, 77}


def test_chase_respects_the_window_and_the_flip_limit():
    good = build_df17(0x840ABC, encode_ident("JAL123", 3))
    rel = np.full((1, 112), 100.0, dtype=np.float32)
    rel[0, [10, 40, 90]] = [1.0, 2.0, 3.0]
    rel[0, 60] = 500.0
    three = as_rows(flip(good, 10, 40, 90))
    assert chase(three, rel, 8, 2) == [[]]  # needs three flips
    assert [set(cols) for _, cols in chase(three, rel, 8, 3)[0]] == [{10, 40, 90}]
    assert chase(as_rows(flip(good, 10, 60)), rel, 4, 2) == [[]]  # bit 60 is not among the 4 weakest
    assert chase(as_rows(flip(good, 10, 60)), rel, 4, 0) == [[]]


def test_chase_skips_frames_that_pass_and_keeps_rows_apart():
    good = build_df17(0x840ABC, encode_ident("JAL123", 3))
    rel = np.full((3, 112), 100.0, dtype=np.float32)
    rel[1, 10] = 1.0
    rel[2, 40] = 1.0
    found = chase(as_rows(good, flip(good, 10), flip(good, 40)), rel, 6, 2)
    assert found[0] == []
    assert [set(c) for _, c in found[1]] == [{10}]
    assert [set(c) for _, c in found[2]] == [{40}]
    assert chase(np.zeros((0, 14), dtype=np.uint8), np.zeros((0, 112), dtype=np.float32), 6, 2) == []


def test_chase_works_on_short_frames():
    good = build_df11(0x840111)
    assert len(good) == 7
    rel = np.full((1, 112), 100.0, dtype=np.float32)
    rel[0, 30] = 1.0
    (options,) = chase(as_rows(flip(good, 30)), rel, window=6, flips=1)
    assert [set(c) for _, c in options] == [{30}]
    assert chase(as_rows(flip(good, 30)), rel, window=500, flips=1)[0]  # a window larger than the frame is fine


@pytest.mark.parametrize("flips", [1, 2, 3, 4])
def test_repairable_accepts_every_frame_within_the_flip_limit(flips):
    rng = np.random.default_rng(flips)
    frames = random_df17(rng, 25)
    damaged = [flip(f, *distinct_positions(rng, flips)) for f in frames]
    assert repairable(as_rows(*damaged), flips).all()
    assert not repairable(as_rows(*frames), flips).any()  # frames that pass need no repair


def test_repairable_rejects_what_is_too_far_off():
    rng = np.random.default_rng(9)
    frames = random_df17(rng, 40)
    three = as_rows(*(flip(f, *distinct_positions(rng, 3)) for f in frames))
    assert not repairable(three, 1).any()
    four = as_rows(*(flip(f, *distinct_positions(rng, 4)) for f in frames))
    assert repairable(four, 3).mean() < 0.1  # about 1 in 70 random syndromes can be reached with 3 flips


def test_repairable_covers_short_frames():
    rng = np.random.default_rng(10)
    frames = [build_df11(int(x)) for x in rng.integers(0x100000, 0xFFFFFF, 20)]
    damaged = as_rows(*(flip(f, *distinct_positions(rng, 3, 56)) for f in frames))
    assert repairable(damaged, 3).all() and not repairable(as_rows(*frames), 3).any()


def test_repairable_never_rejects_what_chase_could_repair():
    rng = np.random.default_rng(8)
    frames = random_df17(rng, 80)
    counts = rng.integers(0, 5, len(frames))
    rows = as_rows(*(flip(f, *distinct_positions(rng, int(c))) for f, c in zip(frames, counts)))
    rel = rng.random((len(rows), 112)).astype(np.float32)
    found = chase(rows, rel, 24, 3)
    ok = repairable(rows, 3)
    assert any(found)
    assert all(ok[i] for i, options in enumerate(found) if options)


def test_soft_repair_needs_a_known_address_and_a_plausible_frame():
    icao = 0x840ABC
    good = build_df17(icao, encode_ident("JAL123", 3))
    bits = np.zeros((1, LONG_BITS), dtype=np.uint8)
    bits[0] = np.unpackbits(np.frombuffer(flip(good, 20, 77), dtype=np.uint8))
    rel = np.full((1, LONG_BITS), 100.0, dtype=np.float32)
    rel[0, 20], rel[0, 77] = 1.0, 2.0
    rows, pos = np.array([0]), np.array([500])

    demod = MlseDemodulator()
    accepted = {}
    demod._soft_repair(bits, rel, rows, pos, 1000, accepted)
    assert accepted == {}  # an unknown address proves nothing

    demod._icao[icao] = 0
    demod._soft_repair(bits, rel, rows, pos, 1000, accepted)
    assert accepted == {0: (good, 2, False)}
    assert demod._icao[icao] == 1500
    assert (demod.stats.fixed, demod.stats.soft_fixed) == (1, 1)

    again = {0: (b"taken", 0, False)}
    demod._soft_repair(bits, rel, rows, pos, 1000, again)  # candidates inside accepted frames stay as they are
    assert again == {0: (b"taken", 0, False)}

    stranger = MlseDemodulator()
    stranger._icao[0x123456] = 0
    other = {}
    stranger._soft_repair(bits, rel, rows, pos, 1000, other)
    assert other == {}


def test_soft_repair_leaves_other_frame_types_alone():
    icao = 0x840ABC
    df4 = build_surveillance(4, icao, encode_ac13(12000))
    bits = np.zeros((1, LONG_BITS), dtype=np.uint8)
    bits[0, :56] = np.unpackbits(np.frombuffer(flip(df4, 20), dtype=np.uint8))
    rel = np.full((1, LONG_BITS), 100.0, dtype=np.float32)
    rel[0, 20] = 1.0
    demod = MlseDemodulator(all_df=True)
    demod._icao[icao] = 0
    accepted = {}
    demod._soft_repair(bits, rel, np.array([0]), np.array([500]), 0, accepted)
    assert accepted == {}  # address-parity frames are never repaired: any change gives some other aircraft


# ---------------------------------------------------------------------------------- decoder


def test_noise_level_is_tracked_from_the_quiet_stretches():
    for sigma in (2.0, 5.0, 12.0):
        demod = MlseDemodulator()
        demod.process(IqSynthesizer(sigma, seed=3).synthesize(0, 262144), start=0)
        assert demod.sigma == pytest.approx(sigma, rel=0.08)
    rng = np.random.default_rng(4)
    _, demod = run(render(random_df17(rng, 150), amp=60, sigma=4.0)[0])  # frames fill a third of the time
    assert demod.sigma == pytest.approx(4.0, rel=0.1)
    assert MlseDemodulator(sigma=7.5).sigma == 7.5
    assert MlseDemodulator(sigma=0.1).sigma == 1.0  # never zero


def test_noise_floor_is_measured_for_the_receive_level_display():
    iq = IqSynthesizer(8.5, seed=3).synthesize(0, 131072)
    mlse, classic = MlseDemodulator(), Demodulator()
    assert mlse.noise_dbfs is None
    mlse.process(iq, start=0)
    classic.process(iq, start=0)
    assert mlse.noise_dbfs == classic.noise_dbfs
    assert mlse.noise_dbfs == pytest.approx(10 * np.log10(2 * 8.5**2 / 127.5**2), abs=0.5)


@pytest.mark.parametrize("phase", range(8))
def test_clean_frames_decode_at_every_timing_phase(phase):
    rng = np.random.default_rng(phase)
    frames = random_df17(rng, 60)
    iq, starts = render(frames, amp=60, phase=phase)
    got, demod = run(iq)
    assert [d.frame for d in got] == frames
    assert demod.stats.valid_crc == len(frames)
    for det, start in zip(got, starts):
        assert abs(det.sample - start) <= 1


@pytest.mark.parametrize("amp, sigma", [(120.0, 1.5), (30.0, 2.0)])
def test_very_strong_and_weak_but_clean_signals_decode(amp, sigma):
    rng = np.random.default_rng(12)
    frames = random_df17(rng, 40)
    iq, _ = render(frames, amp=amp, phase=3, sigma=sigma)
    got, _ = run(iq)
    assert [d.frame for d in got] == frames


@pytest.mark.parametrize("phase", range(8))
def test_strong_short_frames_decode_exactly_at_every_timing_phase(phase):
    # Frames of known and of new aircraft: a last bit decoded wrongly would show up as an
    # interrogator code of a known aircraft, or as a frame that only decodes at the runner-up timing.
    icaos = [0x840A01, 0x4840D6, 0xA1B2C3, 0x3C6444]
    frames = [build_df11(icaos[i % len(icaos)], ca=5) for i in range(32)]
    synth = IqSynthesizer(2.0, seed=40 + phase)
    bursts = [synth.make_burst(600 + 800 * i, frame, 120.0, phase) for i, frame in enumerate(frames)]
    iq = synth.synthesize(0, 600 + 800 * len(frames) + 600, bursts)
    got, demod = run(iq)
    assert [d.frame for d in got] == frames
    assert all(d.corrected == 0 for d in got)
    assert demod.stats.valid_crc == len(frames) and demod.stats.retimed == 0


@pytest.mark.parametrize("block", [333, 1000, 4096, 65537, 1 << 20])
def test_block_boundaries_do_not_lose_or_duplicate_frames(block):
    rng = np.random.default_rng(11)
    frames = random_df17(rng, 60)
    iq, _ = render(frames, amp=70, phase=4, spacing=517)
    ref, _ = run(iq, block=1 << 22, sigma=2.0)
    got, _ = run(iq, block=block, sigma=2.0)
    assert [(d.sample, d.frame) for d in got] == [(d.sample, d.frame) for d in ref]
    assert [d.frame for d in got] == frames


@pytest.mark.parametrize("sigma", [2.0, 8.5])
def test_noise_does_not_turn_into_frames(sigma):
    rng = np.random.default_rng(5)
    synth = IqSynthesizer(sigma, seed=9)
    seconds = 3
    total = seconds * SAMPLE_RATE  # nothing but noise
    iq = b"".join(synth.synthesize(a, min(131072, total - a)) for a in range(0, total, 131072))
    for options in ({"fix_bits": 1}, {"fix_bits": 2}):
        demod = MlseDemodulator(**options)
        for frame in random_df17(rng, 300):  # many known aircraft: plenty of addresses to hit by chance
            demod._icao[int.from_bytes(frame[1:4], "big")] = 0
        got = []
        for a in range(0, len(iq), 2 * 131072):
            got.extend(demod.process(iq[a : a + 2 * 131072], start=a // 2))
        assert got == []
        assert demod.stats.preambles < 1500 * seconds  # the detector stays quiet on noise


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
    bad = flip(good, 51)

    got, _ = run(render([bad])[0])
    assert got == []

    got, demod = run(render([good, bad], spacing=900)[0])
    assert [d.frame for d in got] == [good, good]
    assert [d.corrected for d in got] == [0, 1]
    assert demod.stats.fixed == 1

    got, demod = run(render([good, bad], spacing=900)[0], fix_bits=0)
    assert [d.frame for d in got] == [good]
    assert demod.stats.fixed == 0


def test_two_bit_error_only_with_aggressive_mode():
    icao = 0x840ABD
    good = build_df17(icao, encode_ident("ANA777", 3))
    bad = flip(good, 45, 73)
    iq = render([good, bad], spacing=900)[0]
    assert [d.frame for d in run(iq, fix_bits=1)[0]] == [good]  # a clean signal gives the soft search nothing to go on
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
    for fix_bits in (0, 1, 2):
        got, _ = run(iq, fix_bits=fix_bits)
        assert {d.frame for d in got} <= set(frames)


def test_stream_gap_is_counted_and_state_recovers():
    rng = np.random.default_rng(31)
    frames = random_df17(rng, 6)
    iq, _ = render(frames, spacing=700)
    half = (len(iq) // 4) * 2
    demod = MlseDemodulator()
    first = demod.process(iq[:half], start=0)
    rest = demod.process(iq[half:], start=len(iq) // 2 + 10_000)  # 10 000 samples were dropped
    assert demod.stats.gaps == 1
    assert len(first) + len(rest) >= len(frames) - 2
    again = demod.process(iq[:half], start=50_000_000)
    assert demod.stats.gaps == 2
    assert all(d.sample >= 50_000_000 for d in again)


# ------------------------------------------------------------------- weak-signal advantage


def test_weak_frames_are_decoded_more_often_than_by_the_classic_demodulator():
    iq, weak, strong = weak_stream(240, amp=48.0)
    sent = set(weak) | set(strong)
    classic = {d.frame for d in run(iq, cls=Demodulator)[0]}
    plain, plain_demod = run(iq, soft_flips=0)
    soft, soft_demod = run(iq)
    plain = {d.frame for d in plain}
    soft = {d.frame for d in soft}
    for found in (classic, plain, soft):
        assert found <= sent  # nothing that was never sent
    wanted = set(weak)
    assert len(plain & wanted) >= len(classic & wanted) + 0.1 * len(wanted)
    assert len(soft & wanted) >= len(plain & wanted) + 5
    assert plain_demod.stats.soft_fixed == 0 and soft_demod.stats.soft_fixed >= 5
    assert soft_demod.stats.fixed >= soft_demod.stats.soft_fixed


def test_runner_up_timing_hypotheses_find_more():
    iq, weak, strong = weak_stream(240, amp=48.0)
    one, one_demod = run(iq, hypotheses=1, soft_flips=0)
    three, three_demod = run(iq, hypotheses=3, soft_flips=0)
    assert one_demod.stats.retimed == 0 and three_demod.stats.retimed > 0
    assert three_demod.stats.valid_crc >= one_demod.stats.valid_crc + 5
    assert {d.frame for d in one + three} <= set(weak) | set(strong)


def test_the_syndrome_filter_loses_nothing():
    class Unfiltered(MlseDemodulator):
        def _repairable(self, bits):
            return np.ones(bits.shape[0], dtype=bool)

    iq, _, _ = weak_stream(240, amp=44.0, seed=5)
    for options in ({}, {"fix_bits": 2, "soft_window": 14, "soft_flips": 4}):
        with_filter, a = run(iq, **options)
        without, b = run(iq, cls=Unfiltered, **options)
        assert a.stats.soft_fixed > 0
        assert [(d.sample, d.frame, d.corrected) for d in with_filter] == [(d.sample, d.frame, d.corrected) for d in without]
        assert a.stats.soft_fixed == b.stats.soft_fixed


def test_addresses_learned_in_the_same_block_enable_the_soft_repair():
    iq, _, _ = weak_stream(150, amp=44.0, seed=6)
    assert len(iq) // 2 < 131072  # one block: every address is learned from frames of that very block
    _, demod = run(iq)
    assert demod.stats.soft_fixed > 0
