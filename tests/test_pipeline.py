import asyncio
import logging
import threading
import time

import pytest
from fakes import SlowDisk

from adsbrx import modes
from adsbrx.demod import SAMPLE_RATE, Demodulator, DemodStats
from adsbrx.pipeline import Batch, Pipeline
from adsbrx.recorder import Recorder
from adsbrx.sim.scenario import SimStream, default_scenario
from adsbrx.sources import SimSource, Source
from adsbrx.tracker import Tracker

T0 = 1_700_000_000.0

# Python 3.10 on Windows sleeps in 15.6 ms steps, so a producer paced at 1 ms can be as slow as 15 ms per block.
# The consumer must stay several times slower than that for blocks to be dropped on every platform.
SLOW_CONSUMER = 0.05


class BlockSource(Source):
    name = "blocks"

    def __init__(self, blocks, live=False, error=None, pace=0.0):
        super().__init__()
        self.blocks, self.live, self.error, self.pace = blocks, live, error, pace

    def run(self, sink):
        index = 0
        for data in self.blocks:
            if self.stopped:
                return
            sink(index, data)
            index += len(data) // 2
            if self.pace:
                time.sleep(self.pace)
        if self.error:
            raise self.error


def run_pipeline(source, on_batch=None, demod=None, timeout=30.0, **kwargs):
    async def main():
        loop = asyncio.get_running_loop()
        batches = []

        def handler(batch):
            batches.append(batch)
            if on_batch:
                on_batch(batch, pipe)

        pipe = Pipeline(source, demod or Demodulator(), handler, loop, t0=T0, **kwargs)
        pipe.start()
        result = await asyncio.wait_for(pipe.done, timeout)
        pipe.join(2.0)
        return pipe, batches, result

    return asyncio.run(main())


def test_decodes_simulated_traffic_end_to_end():
    stream = SimStream(default_scenario(respawn=False), seed=3)
    block = 131072
    count = 153  # 10 s
    source = BlockSource([stream.read(block) for _ in range(count)])
    tracker = Tracker(receiver=default_scenario().receiver)

    def feed(batch, pipe):
        for msg in batch.messages:
            tracker.update(msg)

    pipe, batches, error = run_pipeline(source, feed)
    assert error is None and pipe.error is None
    assert len(batches) == count
    assert batches[-1].now == pytest.approx(T0 + count * block / SAMPLE_RATE)
    assert batches[-1].stats["samples"] == count * block
    assert all(b2.now > b1.now for b1, b2 in zip(batches, batches[1:]))
    assert all(-36 < b.noise < -29 for b in batches)  # the simulator's noise floor is about -32 dBFS
    assert len(tracker.visible()) >= 8
    assert tracker.positions > 50 and tracker.rejected_positions == 0
    stamps = [m.ts for b in batches for m in b.messages]
    assert stamps == sorted(stamps) and T0 <= stamps[0] and stamps[-1] <= T0 + count * block / SAMPLE_RATE
    assert not any(t.is_alive() for t in pipe._threads)


def test_source_error_is_reported_after_pending_batches():
    stream = SimStream(default_scenario(respawn=False), seed=1)
    boom = RuntimeError("usb unplugged")
    source = BlockSource([stream.read(65536) for _ in range(5)], error=boom)
    pipe, batches, error = run_pipeline(source)
    assert error is boom and len(batches) == 5


def test_decoder_error_stops_the_source():
    class BrokenDemod:
        stats = DemodStats()

        def process(self, block, start=None):
            raise ValueError("bad block")

    source = BlockSource([b"\x00\x00" * 1000] * 50)
    pipe, batches, error = run_pipeline(source, demod=BrokenDemod())
    assert isinstance(error, ValueError) and batches == []
    assert source.stopped


def test_handler_exceptions_do_not_stop_the_pipeline(caplog):
    source = BlockSource([bytes(2 * 4096)] * 20)
    calls = []

    def bad_handler(batch, pipe):
        calls.append(batch)
        raise RuntimeError("handler bug")

    with caplog.at_level(logging.ERROR, logger="adsbrx"):
        pipe, batches, error = run_pipeline(source, bad_handler)
    assert error is None and len(calls) == 20
    assert "error while handling decoded messages" in caplog.text


def test_blocking_source_is_never_dropped_even_with_a_slow_consumer():
    count, size = 40, 8192
    source = BlockSource([bytes(2 * size)] * count)
    pipe, batches, error = run_pipeline(
        source, lambda b, p: time.sleep(0.002), queue_blocks=2, max_inflight=1
    )
    assert error is None and pipe.dropped_blocks == 0
    assert batches[-1].stats["samples"] == count * size and batches[-1].stats["gaps"] == 0


def test_live_source_drops_blocks_instead_of_waiting():
    count, size = 200, 8192
    source = BlockSource([bytes(2 * size)] * count, live=True, pace=0.001)
    started = time.monotonic()
    pipe, batches, error = run_pipeline(
        source, lambda b, p: time.sleep(SLOW_CONSUMER), queue_blocks=2, max_inflight=1
    )
    assert error is None
    assert pipe.dropped_blocks > 20
    assert batches[-1].dropped_blocks == pipe.dropped_blocks
    assert batches[-1].stats["gaps"] > 0  # the demodulator noticed the missing samples
    assert batches[-1].stats["samples"] + pipe.dropped_blocks * size <= count * size
    assert time.monotonic() - started < 10.0


def test_the_recorder_gets_every_block_even_when_the_decoder_has_to_drop_some(tmp_path):
    count, size = 200, 8192
    data = [bytes([n % 251]) * (2 * size) for n in range(count)]
    path = tmp_path / "rec.cu8"
    recorder = Recorder(str(path), queue_blocks=count)
    recorder.start()
    source = BlockSource(data, live=True, pace=0.001)
    pipe, batches, error = run_pipeline(
        source, lambda b, p: time.sleep(SLOW_CONSUMER), queue_blocks=2, max_inflight=1, recorder=recorder
    )
    recorder.close()
    assert error is None and pipe.dropped_blocks > 20  # the decoder fell behind ...
    assert path.read_bytes() == b"".join(data)  # ... but the recording has every block
    assert recorder.dropped_blocks == 0


def test_a_replayed_source_is_recorded_completely_while_the_disk_keeps_it_waiting(tmp_path):
    data = [bytes([n]) * (2 * 4096) for n in range(60)]
    path = tmp_path / "rec.cu8"
    recorder = Recorder(str(path), queue_blocks=2)
    recorder.start()
    pipe, batches, error = run_pipeline(BlockSource(data), recorder=recorder)
    recorder.close()
    assert error is None and pipe.dropped_blocks == 0 and len(batches) == len(data)
    assert path.read_bytes() == b"".join(data) and recorder.dropped_blocks == 0


@pytest.mark.parametrize("live", [False, True])
def test_a_recorder_failure_ends_the_run_with_its_error_and_stops_the_source(live):
    disk = SlowDisk(fail_after=3)
    recorder = Recorder("rec.cu8", opener=lambda path: disk)
    recorder.start()
    source = BlockSource([bytes(2 * 4096)] * 400, live=live, pace=0.002)
    pipe, batches, error = run_pipeline(source, recorder=recorder)
    recorder.close()
    assert error is recorder.error and pipe.error is recorder.error
    assert str(error) == "cannot write to rec.cu8: No space left on device"
    assert source.stopped and len(batches) < 400
    assert disk.saved == bytes(3 * 2 * 4096)
    assert not any(t.is_alive() for t in pipe._threads)


def test_stop_ends_an_endless_source():
    source = SimSource(realtime=False, seed=2)
    seen = []

    def stopper(batch, pipe):
        seen.append(batch)
        if len(seen) == 5:
            pipe.stop()

    pipe, batches, error = run_pipeline(source, stopper)
    assert error is None and 5 <= len(batches) < 5000
    assert source.stopped and not any(t.is_alive() for t in pipe._threads)


def test_stop_while_the_source_is_blocked_in_a_full_queue():
    class Endless(Source):
        def run(self, sink):
            while not self.stopped:
                sink(0, bytes(2 * 4096))

    requested = threading.Event()

    def stop_once(batch, pipe):
        if not requested.is_set():
            requested.set()
            pipe.stop()

    pipe, batches, error = run_pipeline(Endless(), stop_once, queue_blocks=1, max_inflight=1)
    assert error is None and requested.is_set()
    assert not any(t.is_alive() for t in pipe._threads)


def test_a_demodulator_without_a_noise_measurement_gives_batches_without_noise():
    class QuietDemod:
        stats = DemodStats()

        def process(self, block, start=None):
            return []

    source = BlockSource([bytes(2 * 4096)] * 3)
    pipe, batches, error = run_pipeline(source, demod=QuietDemod())
    assert error is None and [b.noise for b in batches] == [None, None, None]


def test_batch_type():
    batch = Batch([], 1.0, {}, 0)
    assert batch.messages == [] and batch.now == 1.0 and batch.noise is None
    assert Batch([], 1.0, {}, 0, -21.5).noise == -21.5
    assert modes.decode(bytes.fromhex("8D4840D6202CC371C32CE0576098")).callsign == "KLM1023"
