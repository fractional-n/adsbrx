"""The weak-signal benchmark (`python -m adsbrx.sim.bench`): small runs, so that it stays quick."""

import re
from types import SimpleNamespace

import pytest

from adsbrx.demod import SAMPLE_RATE, Demodulator
from adsbrx.mlse import MlseDemodulator
from adsbrx.sim import bench
from adsbrx.sim.bench import (
    NOISE_POOL,
    SIGNAL_POOL,
    Variant,
    format_report,
    main,
    measure,
    noise_stream,
    peak_snr_db,
    run_benchmark,
    signal_stream,
    variants,
)
from adsbrx.sim.modulator import Channel

SMALL = ["--frames", "2", "--amplitudes", "100", "--phases", "4", "--noise-seconds", "0.3"]


def test_peak_snr_is_the_peak_power_over_the_noise_of_one_complex_sample():
    assert peak_snr_db(100.0, 8.5) == pytest.approx(18.4, abs=0.05)
    assert peak_snr_db(24.0, 8.5) == pytest.approx(6.0, abs=0.05)
    assert peak_snr_db(20.0, 1.0) - peak_snr_db(10.0, 1.0) == pytest.approx(6.02, abs=0.01)


def test_the_variants_are_the_shipped_configurations():
    found = {v.label: v.make() for v in variants()}
    assert list(found) == [
        "classic --no-fix",
        "classic",
        "classic --aggressive",
        "mlse --no-fix",
        "mlse, no soft repair",
        "mlse (default)",
        "mlse --aggressive",
    ]
    assert [type(found[name]) for name in list(found)[:3]] == [Demodulator] * 3
    assert [found[name].fix_bits for name in list(found)[:3]] == [0, 1, 2]
    assert all(isinstance(found[name], MlseDemodulator) for name in list(found)[3:])
    assert found["mlse, no soft repair"].soft_flips == 0
    assert (found["mlse (default)"].soft_window, found["mlse (default)"].soft_flips) == (12, 3)
    assert found["mlse --aggressive"].soft_flips == 4
    assert variants()[0].make() is not variants()[0].make()  # a fresh demodulator for every stream


def test_the_signal_stream_holds_the_requested_frames_after_a_primer():
    stream = signal_stream(3, (50.0, 90.0), 4, 8.5, Channel(1.0e6, 4), seed=5)
    tags = list(stream.sent.values())
    assert tags.count(None) == SIGNAL_POOL  # strong frames that teach the addresses
    assert tags.count(50.0) == tags.count(90.0) == 3 * 4
    assert len(stream.sent) == len(tags)  # every frame is different, so none can be mistaken for another
    assert sum(len(block) // 2 for block in stream.blocks) == stream.samples
    assert stream.lead == 0 and stream.seconds == pytest.approx(stream.samples / SAMPLE_RATE)


def test_the_noise_stream_is_mostly_noise_and_only_that_is_timed():
    stream = noise_stream(0.5, 4, 8.5, Channel(1.0e6, 4), seed=5)
    assert len(stream.sent) == NOISE_POOL and set(stream.sent.values()) == {None}
    assert stream.lead > 0 and stream.samples - stream.lead == int(0.5 * SAMPLE_RATE)


def test_streams_are_reproducible():
    one = signal_stream(1, (60.0,), 4, 8.5, Channel(1.0e6, 4), seed=7)
    two = signal_stream(1, (60.0,), 4, 8.5, Channel(1.0e6, 4), seed=7)
    other = signal_stream(1, (60.0,), 4, 8.5, Channel(1.0e6, 4), seed=8)
    assert one.blocks == two.blocks and one.sent == two.sent
    assert one.blocks != other.blocks


def test_a_small_benchmark_ranks_the_demodulators_and_finds_no_false_frames():
    report = run_benchmark(frames=6, amplitudes=(100.0,), phases=4, noise_seconds=0.3, chosen=[variants()[1], variants()[5]])
    classic, mlse = report.rows
    assert (classic.label, mlse.label) == ("classic", "mlse (default)")
    assert report.amplitudes == (100.0,) and report.frames == 6 and report.noise_seconds == 0.3
    for row in report.rows:
        assert len(row.rates) == 1 and 0.0 <= row.rates[0] <= 100.0 and row.mean == row.rates[0]
        assert row.false_frames == 0
        assert row.dense_msps > 0 and row.idle_msps > 0 and row.candidates_per_s >= 0
    assert mlse.rates[0] > classic.rates[0]  # at this level the classic demodulator loses a quarter of the frames


def test_false_frames_are_counted_in_both_streams():
    class Forger:
        """Reports a frame nobody sent for every block."""

        def __init__(self):
            self.stats = SimpleNamespace(preambles=0)

        def process(self, block, start):
            return [SimpleNamespace(frame=b"\x8d" + b"\xaa" * 13)]

    channel = Channel(1.0e6, 4)
    signal = signal_stream(1, (60.0,), 4, 8.5, channel, seed=3)
    noise = noise_stream(0.3, 4, 8.5, channel, seed=3)
    row = measure(Variant("forger", Forger), signal, noise, (60.0,))
    assert row.false_frames == len(signal.blocks) + len(noise.blocks)
    assert row.rates == [0.0]


def test_the_report_lists_every_variant_and_amplitude():
    report = run_benchmark(frames=2, amplitudes=(60.0, 100.0), phases=4, noise_seconds=0.3, chosen=variants()[:2])
    text = format_report(report)
    assert text.startswith("Weak-signal benchmark (simulation): noise 8.5 counts rms")
    assert "16 frames (4 timing phases x 2 per amplitude)" in text
    for name in ("classic --no-fix", "peak SNR [dB]", "candidates/s", "false frames"):
        assert name in text
    assert re.search(r"^classic\s+\d", text, re.MULTILINE)  # the plain classic row, not only "classic --no-fix"
    assert "mlse" not in text.split("Frames decoded")[1].split("Cost")[0]  # only the chosen ones
    amplitude_row = next(line for line in text.splitlines() if line.startswith("peak amplitude"))
    assert amplitude_row.split()[3:6] == ["60", "100", "mean"]


def test_the_command_line_runs_every_variant_and_prints_the_report(capsys):
    assert main(SMALL) == 0
    text = capsys.readouterr().out
    for v in variants():
        assert v.label in text
    assert text.count("\n") > 15 and "false frames" in text


def test_the_command_line_passes_its_options_on(monkeypatch, capsys):
    seen = {}

    def fake(**kwargs):
        seen.update(kwargs)
        return "a report"

    monkeypatch.setattr(bench, "run_benchmark", fake)
    monkeypatch.setattr(bench, "format_report", lambda report: f"printed {report}")
    argv = ["--frames", "7", "--amplitudes", "30,45.5", "--sigma", "6", "--phases", "8", "--cutoff", "0.8", "--noise-seconds", "2", "--seed", "9"]
    assert main(argv) == 0
    assert capsys.readouterr().out == "printed a report\n"
    assert seen == {
        "frames": 7, "amplitudes": (30.0, 45.5), "sigma": 6.0, "phases": 8, "cutoff_mhz": 0.8, "noise_seconds": 2.0, "seed": 9,
    }


@pytest.mark.parametrize(
    "argv",
    [
        ["--frames", "0"],
        ["--frames", "many"],
        ["--amplitudes", "50,50"],
        ["--amplitudes", ""],
        ["--amplitudes", "-3"],
        ["--amplitudes", "loud"],
        ["--sigma", "0"],
        ["--sigma", "nan"],
        ["--phases", "0"],
        ["--cutoff", "-1"],
        ["--noise-seconds", "0"],
        ["--seed", "x"],
    ],
)
def test_bad_options_are_usage_errors(capsys, argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert "invalid" in capsys.readouterr().err
