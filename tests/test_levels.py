import json
import math

import pytest

from adsbrx.levels import LevelMeter

T0 = 1_700_000_000.0


def strict_json(document):
    return json.loads(json.dumps(document, allow_nan=False))


def test_a_new_meter_reads_nothing():
    meter = LevelMeter()
    assert meter.reading() == {"window": 10, "noise": None, "signal": None, "peak": None, "snr": None, "messages": 0}
    assert strict_json(meter.document()) == {"now": None, **meter.reading(), "history": []}


def test_noise_alone_gives_no_signal_or_snr():
    meter = LevelMeter()
    meter.update(T0 + 0.1, -21.34)
    reading = meter.reading()
    assert reading["noise"] == -21.3  # rounded to a tenth
    assert reading["signal"] is None and reading["peak"] is None and reading["snr"] is None
    assert reading["messages"] == 0


def test_signal_is_the_power_mean_of_the_frames_and_peak_the_strongest():
    meter = LevelMeter()
    meter.update(T0 + 0.1, -35.0, [-20.0, -30.0])
    reading = meter.reading()
    assert reading["signal"] == pytest.approx(10 * math.log10((1e-2 + 1e-3) / 2), abs=0.05)  # -22.6, not -25
    assert reading["peak"] == -20.0
    assert reading["snr"] == pytest.approx(reading["signal"] + 35.0, abs=0.11)
    assert reading["messages"] == 2


def test_noise_is_the_median_so_a_noisy_block_does_not_move_it():
    meter = LevelMeter()
    for index, noise in enumerate((-30.0, -20.0, -31.0, -29.0, -30.5)):
        meter.update(T0 + 0.1 * index, noise)
    assert meter.reading()["noise"] == -30.0

    spread = LevelMeter()
    for second, noise in enumerate((-30.0, -10.0, -30.0)):  # one second with an interfering burst
        spread.update(T0 + second, noise)
    assert spread.reading()["noise"] == -30.0


def test_only_the_last_window_counts():
    meter = LevelMeter(window=10)
    meter.update(T0 + 0.5, -20.0, [-10.0])
    meter.update(T0 + 9.5, -30.0, [-30.0])  # the first second is still inside the 10 seconds
    reading = meter.reading()
    assert reading["messages"] == 2 and reading["peak"] == -10.0
    assert reading["noise"] == -25.0

    meter.update(T0 + 10.5, -40.0)  # ... and now it is not
    reading = meter.reading()
    assert reading["messages"] == 1 and reading["peak"] == -30.0
    assert reading["noise"] == -35.0  # the median of the last two seconds (-30 and -40)

    meter.update(T0 + 60.5, -50.0)  # a long silence leaves only the newest second
    reading = meter.reading()
    assert reading["messages"] == 0 and reading["signal"] is None and reading["noise"] == -50.0


def test_the_window_can_be_chosen():
    meter = LevelMeter(window=3)
    for second in range(6):
        meter.update(T0 + second + 0.5, -30.0, [-20.0 - second])
    reading = meter.reading()
    assert reading["window"] == 3 and reading["messages"] == 3 and reading["peak"] == -23.0


def test_history_holds_the_finished_seconds_only():
    meter = LevelMeter()
    meter.update(T0 + 0.2, -30.0, [-20.0])
    meter.update(T0 + 0.7, -32.0, [-20.0, -30.0])
    assert meter.document()["history"] == []  # the second is not over yet
    meter.update(T0 + 1.1, -31.0)
    meter.update(T0 + 2.5, -33.0, [-25.0])
    history = meter.document()["history"]
    assert history == [
        [int(T0), -31.0, -21.5, -20.0, 3],
        [int(T0) + 1, -31.0, None, None, 0],
    ]


def test_history_since_returns_only_what_the_viewer_has_not_seen():
    meter = LevelMeter()
    for second in range(6):
        meter.update(T0 + second + 0.5, -30.0, [-20.0])
    stamps = [row[0] for row in meter.document()["history"]]
    assert stamps == [int(T0) + n for n in range(5)]
    assert [row[0] for row in meter.document(since=int(T0) + 2)["history"]] == [int(T0) + 3, int(T0) + 4]
    assert meter.document(since=int(T0) + 4)["history"] == []
    assert meter.document(since=T0 + 2.5)["history"][0][0] == int(T0) + 3
    assert [row[0] for row in meter.document(since=0)["history"]] == stamps


def test_history_keeps_only_the_newest_seconds():
    meter = LevelMeter(history=5)
    for second in range(30):
        meter.update(T0 + second, -30.0, [-20.0])
    stamps = [row[0] for row in meter.document()["history"]]
    assert stamps == [int(T0) + n for n in range(24, 29)]


def test_document_carries_the_clock_and_the_reading():
    meter = LevelMeter()
    meter.update(T0 + 0.25, -30.0, [-20.0])
    meter.update(T0 + 3.14159, -30.0)
    document = meter.document()
    assert document["now"] == round(T0 + 3.14159, 1)
    assert {key: document[key] for key in meter.reading()} == meter.reading()
    assert strict_json(document) == document


def test_a_clock_that_steps_back_is_counted_in_the_current_second():
    meter = LevelMeter()
    meter.update(T0 + 5.5, -30.0, [-20.0])
    meter.update(T0 + 3.0, -30.0, [-24.0])
    assert meter.document()["now"] == round(T0 + 5.5, 1)
    assert meter.document()["history"] == []
    assert meter.reading()["messages"] == 2
    meter.update(T0 + 6.5, -30.0)
    assert [row[0] for row in meter.document()["history"]] == [int(T0) + 5]


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_values_that_are_not_numbers_are_ignored(bad):
    meter = LevelMeter()
    meter.update(T0 + 0.1, bad, [bad])
    assert meter.reading() == {"window": 10, "noise": None, "signal": None, "peak": None, "snr": None, "messages": 0}
    meter.update(T0 + 0.2, -30.0, [bad, -25.0])
    reading = meter.reading()
    assert reading["noise"] == -30.0 and reading["signal"] == -25.0 and reading["messages"] == 1
    meter.update(T0 + 1.2, bad)
    strict_json(meter.document())


def test_the_signal_of_frames_can_come_from_any_iterable():
    meter = LevelMeter()
    meter.update(T0 + 0.1, -30.0, (x for x in (-20.0, -20.0)))
    meter.update(T0 + 0.2, -30.0, iter([-20.0]))
    reading = meter.reading()
    assert reading["messages"] == 3 and reading["signal"] == -20.0


def test_a_very_weak_frame_still_has_a_level():
    meter = LevelMeter()
    meter.update(T0 + 0.1, -45.0, [-80.0])
    assert meter.reading()["signal"] == -80.0
    assert meter.reading()["snr"] == -35.0
