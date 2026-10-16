"""Threaded receive pipeline: source thread -> demodulator thread -> asyncio event loop."""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import modes
from .demod import SAMPLE_RATE, Demodulator
from .modes import ModeSMessage
from .recorder import Recorder
from .sources import Source

log = logging.getLogger("adsbrx")


@dataclass(slots=True)
class Batch:
    """Everything decoded from one block of samples."""

    messages: list[ModeSMessage]
    now: float  # stream clock (epoch seconds) at the end of the block
    stats: dict  # demodulator counters
    dropped_blocks: int  # blocks a live source had to discard because decoding lagged
    noise: float | None = None  # noise floor of the block in dBFS, if the demodulator measures it


class Pipeline:
    """Runs `source` and the demodulator in threads and hands batches to `on_batch`.

    `on_batch` is always called in the thread of `loop`, so it can freely touch the
    tracker, the output servers and everything else that lives on the event loop.
    `done` resolves to None, or to the exception that ended the run, once all
    batches have been delivered.

    A `recorder` gets every block the source delivers, before the decoder's queue can
    drop any; if it fails (full disk, ...) the run ends with its error.
    """

    def __init__(
        self,
        source: Source,
        demod: Demodulator,
        on_batch: Callable[[Batch], None],
        loop: asyncio.AbstractEventLoop,
        *,
        t0: float | None = None,
        queue_blocks: int = 64,
        max_inflight: int = 256,
        recorder: Recorder | None = None,
    ):
        self.source = source
        self.demod = demod
        self.on_batch = on_batch
        self.loop = loop
        self.recorder = recorder
        self.t0 = time.time() if t0 is None else t0
        self.dropped_blocks = 0
        self.error: BaseException | None = None
        self.done: asyncio.Future = loop.create_future()
        self._queue: queue.Queue = queue.Queue(maxsize=queue_blocks)
        self._inflight = threading.BoundedSemaphore(max_inflight)
        self._stopping = threading.Event()
        self._decoder_exited = threading.Event()
        self._threads = [
            threading.Thread(target=self._decode_main, name="adsbrx-decoder", daemon=True),
            threading.Thread(target=self._read_main, name="adsbrx-source", daemon=True),
        ]

    # -- control (any thread) -------------------------------------------------------------

    def start(self) -> None:
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stopping.set()
        self.source.stop()

    def join(self, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            if thread.is_alive():
                thread.join(max(0.0, deadline - time.monotonic()))

    def _fail(self, exc: BaseException) -> None:
        if self.error is None:
            self.error = exc

    # -- source thread --------------------------------------------------------------------

    def _put(self, index: int, data: bytes) -> None:
        recorder = self.recorder
        if recorder is not None:
            recorder.write(data, wait=not self.source.live)
            if recorder.error is not None:
                self._fail(recorder.error)
                self.source.stop()
                return
        if self.source.live:
            try:
                self._queue.put_nowait((index, data))
            except queue.Full:
                self.dropped_blocks += 1
            return
        while not self._stopping.is_set():
            try:
                self._queue.put((index, data), timeout=0.2)
                return
            except queue.Full:
                continue

    def _read_main(self) -> None:
        try:
            self.source.run(self._put)
        except BaseException as exc:  # reported through `done`
            self._fail(exc)
        finally:
            while not self._decoder_exited.is_set():
                try:
                    self._queue.put(None, timeout=0.2)
                    break
                except queue.Full:
                    continue

    # -- decoder thread -------------------------------------------------------------------

    def _decode_main(self) -> None:
        demod, t0 = self.demod, self.t0
        try:
            while True:
                try:
                    item = self._queue.get(timeout=0.25)
                except queue.Empty:
                    if self._stopping.is_set():
                        break
                    continue
                if item is None:
                    break
                index, data = item
                messages = []
                for det in demod.process(data, start=index):
                    msg = modes.decode(
                        det.frame,
                        ts=t0 + det.sample / SAMPLE_RATE,
                        sample=det.sample,
                        rssi=det.rssi,
                        corrected=det.corrected,
                    )
                    if msg is not None:
                        messages.append(msg)
                batch = Batch(
                    messages,
                    t0 + (index + len(data) // 2) / SAMPLE_RATE,
                    demod.stats.as_dict(),
                    self.dropped_blocks,
                    getattr(demod, "noise_dbfs", None),
                )
                if not self._post(batch):
                    break
        except BaseException as exc:
            self._fail(exc)
            self.source.stop()
        finally:
            self._decoder_exited.set()
            try:
                self.loop.call_soon_threadsafe(self._resolve)
            except RuntimeError:  # the event loop is already closed
                pass

    def _post(self, batch: Batch) -> bool:
        while not self._inflight.acquire(timeout=0.25):
            if self._stopping.is_set():
                return False
        try:
            self.loop.call_soon_threadsafe(self._deliver, batch)
        except RuntimeError:
            self._inflight.release()
            return False
        return True

    # -- event loop thread ----------------------------------------------------------------

    def _deliver(self, batch: Batch) -> None:
        try:
            self.on_batch(batch)
        except Exception:
            log.exception("error while handling decoded messages")
        finally:
            self._inflight.release()

    def _resolve(self) -> None:
        if not self.done.done():
            self.done.set_result(self.error)
