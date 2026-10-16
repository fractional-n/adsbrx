"""Raw I/Q recording: the writer thread, the ways it can fail, and what a receiver may rely on."""

import errno
import threading
import time

import pytest
from fakes import SlowDisk

from adsbrx.recorder import Recorder, RecorderError

BLOCK = 4096


def blocks(count: int, size: int = BLOCK) -> list[bytes]:
    """Distinguishable blocks: block n is filled with the byte n."""
    return [bytes([n % 256]) * size for n in range(count)]


def recorder_on(disk: SlowDisk, tmp_path, **kwargs) -> Recorder:
    rec = Recorder(str(tmp_path / "rec.cu8"), opener=lambda path: disk, **kwargs)
    rec.start()
    return rec


# ------------------------------------------------------------------------------ the file


def test_every_block_reaches_the_file_in_order(tmp_path):
    path = tmp_path / "rec.cu8"
    rec = Recorder(str(path))
    rec.start()
    data = blocks(50)
    for block in data:
        rec.write(block)
    rec.close()
    assert path.read_bytes() == b"".join(data)
    assert (rec.bytes_written, rec.dropped_blocks, rec.error) == (50 * BLOCK, 0, None)


def test_an_existing_file_is_never_overwritten(tmp_path):
    path = tmp_path / "rec.cu8"
    path.write_bytes(b"precious")
    with pytest.raises(RecorderError, match="already exists"):
        Recorder(str(path)).start()
    assert path.read_bytes() == b"precious"
    with pytest.raises(RecorderError, match="cannot record to"):
        Recorder(str(tmp_path)).start()  # a folder is in the way, too


def test_an_unusable_path_is_reported_with_the_reason(tmp_path):
    path = tmp_path / "no" / "such" / "dir" / "rec.cu8"
    with pytest.raises(RecorderError) as exc:
        Recorder(str(path)).start()
    assert str(exc.value).startswith(f"cannot record to {path}: ")
    assert "No such file" in str(exc.value)


def test_a_recording_that_stayed_empty_is_removed(tmp_path):
    path = tmp_path / "rec.cu8"
    rec = Recorder(str(path))
    rec.start()
    assert path.exists()  # created at once, so a bad path or an existing file is found before the receiver starts
    rec.close()
    assert not path.exists() and rec.error is None


def test_closing_twice_or_without_starting_is_harmless(tmp_path):
    path = tmp_path / "rec.cu8"
    Recorder(str(path)).close()
    rec = Recorder(str(path))
    rec.start()
    rec.write(b"abcd")
    rec.close()
    rec.close()
    assert path.read_bytes() == b"abcd" and rec.error is None


def test_writing_after_closing_is_ignored_and_never_waits(tmp_path):
    path = tmp_path / "rec.cu8"
    rec = Recorder(str(path), queue_blocks=1)
    rec.start()
    rec.write(b"abcd")
    rec.close()
    started = time.monotonic()
    rec.write(b"late", wait=True)
    rec.write(b"late", wait=False)
    assert time.monotonic() - started < 1.0
    assert path.read_bytes() == b"abcd" and rec.dropped_blocks == 0


# --------------------------------------------------------------------------- a slow disk


def test_a_live_source_drops_blocks_instead_of_waiting_for_the_disk(tmp_path):
    disk = SlowDisk()
    disk.hold()
    rec = recorder_on(disk, tmp_path, queue_blocks=4)
    data = blocks(20)
    rec.write(data[0], wait=False)
    assert disk.busy.wait(5)  # the writer is now stuck inside the first write
    started = time.monotonic()
    for block in data[1:]:
        rec.write(block, wait=False)
    assert time.monotonic() - started < 1.0  # nothing here waited for the disk
    assert rec.dropped_blocks == 15  # four fit in the queue, the other fifteen are lost
    disk.released.set()
    rec.close()
    assert disk.saved == b"".join(data[:5])
    assert (rec.bytes_written, rec.error) == (5 * BLOCK, None)


def test_a_replayed_source_waits_for_the_disk_and_loses_nothing(tmp_path):
    disk = SlowDisk()
    disk.hold()
    rec = recorder_on(disk, tmp_path, queue_blocks=2)
    data = blocks(8)
    producer = threading.Thread(target=lambda: [rec.write(block) for block in data])
    producer.start()
    assert disk.busy.wait(5)
    producer.join(0.3)
    assert producer.is_alive()  # the queue is full and the disk is held back: the source has to wait
    disk.released.set()
    producer.join(10)
    assert not producer.is_alive()
    rec.close()
    assert disk.saved == b"".join(data)
    assert (rec.dropped_blocks, rec.error) == (0, None)


def test_a_stuck_disk_cannot_hang_the_shutdown(tmp_path):
    disk = SlowDisk()
    disk.hold()
    rec = recorder_on(disk, tmp_path, queue_blocks=1)
    rec.write(b"x" * BLOCK, wait=False)
    assert disk.busy.wait(5)
    started = time.monotonic()
    rec.close(timeout=0.3)
    assert time.monotonic() - started < 5.0
    assert "did not finish writing" in str(rec.error)
    disk.released.set()  # let the writer thread end
    rec._thread.join(5)
    assert not rec._thread.is_alive()


# -------------------------------------------------------------------------- a failing disk


@pytest.mark.parametrize("wait", [True, False])
def test_a_full_disk_stops_the_recording_without_hanging_the_source(tmp_path, wait):
    disk = SlowDisk(fail_after=2)
    rec = recorder_on(disk, tmp_path, queue_blocks=2)
    data = blocks(12)
    started = time.monotonic()
    for block in data:
        rec.write(block, wait=wait)
        if rec.error is None:
            time.sleep(0.01)  # give the writer a chance to get there
    assert time.monotonic() - started < 5.0

    rec.close()
    assert isinstance(rec.error, RecorderError)
    assert str(rec.error) == f"cannot write to {tmp_path / 'rec.cu8'}: No space left on device"
    assert disk.saved == b"".join(data[:2])  # what was written before the failure is kept
    assert rec.bytes_written == 2 * BLOCK

    before = rec.dropped_blocks
    started = time.monotonic()
    rec.write(b"more", wait=wait)  # once failed, writing is a no-op that never waits
    assert time.monotonic() - started < 1.0 and rec.dropped_blocks == before


def test_an_unexpected_writer_failure_is_reported_too(tmp_path):
    class Broken(SlowDisk):
        def write(self, data):
            raise ValueError("boom")

    rec = recorder_on(Broken(), tmp_path)
    rec.write(b"abcd")
    rec.close()
    assert "boom" in str(rec.error)


def test_a_failure_while_closing_the_file_is_reported(tmp_path):
    class Unflushable(SlowDisk):
        def close(self):
            super().close()
            raise OSError(errno.EIO, "Input/output error")

    disk = Unflushable()
    rec = recorder_on(disk, tmp_path)
    rec.write(b"abcd")
    rec.close()
    assert "Input/output error" in str(rec.error)
    assert rec.bytes_written == 4
