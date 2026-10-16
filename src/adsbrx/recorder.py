"""Raw I/Q recording: saves the cu8 stream to a file while receiving, for replay with `--file`."""

from __future__ import annotations

import os
import queue
import threading
import time
from collections.abc import Callable
from typing import BinaryIO


class RecorderError(RuntimeError):
    """The recording cannot be made (bad path, existing file, full disk, ...)."""


def create_file(path: str) -> BinaryIO:
    """Creates `path` for writing; an existing file is never overwritten."""
    try:
        return open(path, "xb")
    except FileExistsError as exc:
        raise RecorderError(f"cannot record to {path}: it already exists (delete it or choose another name)") from exc
    except OSError as exc:
        raise RecorderError(f"cannot record to {path}: {exc.strerror or exc}") from exc


class Recorder:
    """Writes the blocks it is given to a file, on a thread of its own.

    A slow disk therefore cannot hold up the dongle: for a live source `write()` never waits, and when
    the writer falls behind the block is dropped and counted instead, just as the decoder's blocks are.
    """

    def __init__(self, path: str, *, queue_blocks: int = 128, opener: Callable[[str], BinaryIO] = create_file):
        self.path = path
        self.bytes_written = 0
        self.dropped_blocks = 0  # blocks missing from the file because the disk was too slow
        self.error: RecorderError | None = None  # set when writing failed; nothing more is recorded then
        self._opener = opener
        self._queue: queue.Queue = queue.Queue(maxsize=queue_blocks)
        self._file: BinaryIO | None = None
        self._thread: threading.Thread | None = None
        self._closed = False

    def start(self) -> None:
        """Creates the file (raises RecorderError if that is not possible) and starts writing."""
        self._file = self._opener(self.path)
        self._thread = threading.Thread(target=self._write_main, name="adsbrx-recorder", daemon=True)
        self._thread.start()

    def write(self, data: bytes, wait: bool = True) -> None:
        """Queues one block (source thread). With `wait` False a full queue drops the block instead of blocking."""
        if wait:
            while not self._closed and self.error is None:
                try:
                    self._queue.put(data, timeout=0.2)
                    return
                except queue.Full:
                    continue
        elif not self._closed and self.error is None:
            try:
                self._queue.put_nowait(data)
            except queue.Full:
                self.dropped_blocks += 1

    def close(self, timeout: float = 10.0) -> None:
        """Writes out what is queued and closes the file. A recording that stayed empty is deleted."""
        thread = self._thread
        if thread is None or self._closed:
            return
        self._closed = True  # later writes are ignored, so nothing can queue up behind the end marker
        deadline = time.monotonic() + timeout
        while thread.is_alive() and time.monotonic() < deadline:
            try:
                self._queue.put(None, timeout=0.1)
                break
            except queue.Full:
                continue
        thread.join(max(0.0, deadline - time.monotonic()))
        if thread.is_alive():
            if self.error is None:
                self.error = RecorderError(f"the disk did not finish writing {self.path} within {timeout:.0f} s")
        elif self.bytes_written == 0:
            try:
                os.remove(self.path)
            except OSError:
                pass

    def _write_main(self) -> None:
        file = self._file
        assert file is not None
        try:
            while True:
                block = self._queue.get()
                if block is None:
                    break
                file.write(block)
                self.bytes_written += len(block)
        except Exception as exc:  # the receiver has to find out, whatever went wrong
            self.error = RecorderError(f"cannot write to {self.path}: {getattr(exc, 'strerror', None) or exc}")
        finally:
            try:
                file.close()
            except Exception as exc:
                if self.error is None:
                    self.error = RecorderError(f"cannot write to {self.path}: {getattr(exc, 'strerror', None) or exc}")
