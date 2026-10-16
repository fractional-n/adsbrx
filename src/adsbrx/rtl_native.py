"""Minimal ctypes binding to librtlsdr (rtlsdr.dll, librtlsdr.so, librtlsdr.dylib).

Only what a receiver needs is bound. pyrtlsdr is deliberately not used: it calls
newer librtlsdr functions at import time and fails with the older builds that ship
with many Windows SDR programs.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass

ENV_DLL_DIR = "ADSBRX_RTLSDR_DLL_DIR"
TUNER_NAMES = {0: "unknown", 1: "E4000", 2: "FC0012", 3: "FC0013", 4: "FC2580", 5: "R820T", 6: "R828D"}
_BUF_LEN = 262144  # bytes per USB transfer: 131072 samples, about 65 ms at 2 MS/s

_READ_CB = ctypes.CFUNCTYPE(None, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint32, ctypes.c_void_p)


class RtlSdrError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeviceInfo:
    index: int
    name: str
    manufacturer: str
    product: str
    serial: str


def _library_names() -> list[str]:
    if sys.platform == "win32":
        return ["rtlsdr.dll", "librtlsdr.dll"]
    if sys.platform == "darwin":
        return ["librtlsdr.dylib", "librtlsdr.0.dylib"]
    return ["librtlsdr.so.2", "librtlsdr.so.0", "librtlsdr.so"]


def _candidate_paths(dll_dir: str | None) -> list[str]:
    names = _library_names()
    dirs: list[str] = []
    if dll_dir:
        dirs.append(dll_dir)
    if os.environ.get(ENV_DLL_DIR):
        dirs.append(os.environ[ENV_DLL_DIR])
    dirs.extend(p for p in os.environ.get("PATH", "").split(os.pathsep) if p)
    dirs.append(os.getcwd())
    found = []
    for d in dirs:
        for name in names:
            path = os.path.join(d, name)
            if os.path.isfile(path) and path not in found:
                found.append(path)
    return found


def _bind(lib) -> None:
    """Declare the C signatures; the optional bias tee call may be missing in old builds."""
    c_int, c_u32, c_vp, c_ip = ctypes.c_int, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
    signatures = {
        "rtlsdr_get_device_count": ([], c_u32),
        "rtlsdr_get_device_name": ([c_u32], ctypes.c_char_p),
        "rtlsdr_get_device_usb_strings": ([c_u32, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p], c_int),
        "rtlsdr_open": ([ctypes.POINTER(c_vp), c_u32], c_int),
        "rtlsdr_close": ([c_vp], c_int),
        "rtlsdr_set_center_freq": ([c_vp, c_u32], c_int),
        "rtlsdr_set_freq_correction": ([c_vp, c_int], c_int),
        "rtlsdr_get_tuner_type": ([c_vp], c_int),
        "rtlsdr_get_tuner_gains": ([c_vp, c_ip], c_int),
        "rtlsdr_set_tuner_gain": ([c_vp, c_int], c_int),
        "rtlsdr_set_tuner_gain_mode": ([c_vp, c_int], c_int),
        "rtlsdr_set_sample_rate": ([c_vp, c_u32], c_int),
        "rtlsdr_set_agc_mode": ([c_vp, c_int], c_int),
        "rtlsdr_reset_buffer": ([c_vp], c_int),
        "rtlsdr_read_async": ([c_vp, _READ_CB, c_vp, c_u32, c_u32], c_int),
        "rtlsdr_cancel_async": ([c_vp], c_int),
    }
    for name, (argtypes, restype) in signatures.items():
        func = getattr(lib, name)
        func.argtypes, func.restype = argtypes, restype
    bias = getattr(lib, "rtlsdr_set_bias_tee", None)
    if bias is not None:
        bias.argtypes, bias.restype = [c_vp, c_int], c_int


def load_library(dll_dir: str | None = None):
    """Load librtlsdr. Search order: `dll_dir`, $ADSBRX_RTLSDR_DLL_DIR, PATH, the system."""
    errors: list[str] = []
    candidates: list = _candidate_paths(dll_dir)
    system = ctypes.util.find_library("rtlsdr")
    if system:
        candidates.append(system)
    candidates.extend(n for n in _library_names() if n not in candidates)
    for target in candidates:
        directory = os.path.dirname(target) if os.path.isabs(target) else ""
        handle = None
        try:
            if directory and hasattr(os, "add_dll_directory"):
                handle = os.add_dll_directory(directory)
            lib = ctypes.CDLL(target)
            _bind(lib)
            return lib
        except (OSError, AttributeError) as exc:
            errors.append(f"{target}: {exc}")
        finally:
            if handle is not None:
                handle.close()
    hint = (
        "librtlsdr was not found. Install it (Windows: copy rtlsdr.dll and libusb-1.0.dll from an SDR "
        "program or the rtl-sdr release, then pass --dll-dir or set ADSBRX_RTLSDR_DLL_DIR; "
        "Linux: apt install librtlsdr0; macOS: brew install librtlsdr)."
    )
    detail = "; ".join(errors[-3:])
    raise RtlSdrError(f"{hint}" + (f" Tried: {detail}" if detail else ""))


def _text(value) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else (value or "")


def list_devices(lib=None, dll_dir: str | None = None) -> list[DeviceInfo]:
    lib = lib or load_library(dll_dir)
    devices = []
    for index in range(int(lib.rtlsdr_get_device_count())):
        manufacturer, product, serial = (ctypes.create_string_buffer(256) for _ in range(3))
        rc = lib.rtlsdr_get_device_usb_strings(index, manufacturer, product, serial)
        devices.append(
            DeviceInfo(
                index,
                _text(lib.rtlsdr_get_device_name(index)),
                _text(manufacturer.value) if rc == 0 else "",
                _text(product.value) if rc == 0 else "",
                _text(serial.value) if rc == 0 else "",
            )
        )
    return devices


def nearest_gain(gains: list[int], wanted_db: float) -> int:
    """The supported gain (tenths of dB) closest to `wanted_db`."""
    return min(gains, key=lambda g: abs(g - wanted_db * 10))


class RtlSdrDevice:
    """One opened dongle."""

    def __init__(self, index: int = 0, lib=None, dll_dir: str | None = None):
        self.lib = lib or load_library(dll_dir)
        self.index = index
        count = int(self.lib.rtlsdr_get_device_count())
        if count == 0:
            raise RtlSdrError(
                "No RTL-SDR device found. Check the USB connection; on Windows the WinUSB driver "
                "must be installed for the dongle (Zadig: Options > List All Devices, "
                "'Bulk-In, Interface (Interface 0)' > WinUSB)."
            )
        if index >= count:
            raise RtlSdrError(f"Device index {index} not available ({count} device(s) found).")
        self.name = _text(self.lib.rtlsdr_get_device_name(index))
        handle = ctypes.c_void_p()
        rc = self.lib.rtlsdr_open(ctypes.pointer(handle), index)
        if rc != 0 or not handle.value:
            raise RtlSdrError(
                f"rtlsdr_open failed for '{self.name}' (code {rc}). Is another program (SDR#, rtl_tcp, "
                "dump1090) using it, or is the driver not WinUSB/libusb?"
            )
        self._dev = handle
        self._closed = False
        self._lock = threading.Lock()  # cancel() may come from another thread than close()
        self._callback = None  # keeps the C callback alive while streaming
        self.tuner = TUNER_NAMES.get(int(self.lib.rtlsdr_get_tuner_type(self._dev)), "unknown")

    def _check(self, rc: int, what: str, ok: tuple[int, ...] = (0,)) -> None:
        if rc not in ok:
            raise RtlSdrError(f"{what} failed (code {rc})")

    def tuner_gains(self) -> list[int]:
        """Supported manual gains in tenths of dB."""
        count = int(self.lib.rtlsdr_get_tuner_gains(self._dev, None))
        if count <= 0:
            return []
        values = (ctypes.c_int * count)()
        self.lib.rtlsdr_get_tuner_gains(self._dev, values)
        return list(values)

    def configure(
        self,
        freq_hz: int = 1_090_000_000,
        sample_rate: int = 2_000_000,
        gain: str | float = "max",
        ppm: int = 0,
        agc: bool = False,
        bias_tee: bool = False,
    ) -> float | None:
        """Tune and set the gain. Returns the manual gain in dB (None for automatic gain)."""
        lib, dev = self.lib, self._dev
        self._check(lib.rtlsdr_set_sample_rate(dev, int(sample_rate)), "set_sample_rate")
        self._check(lib.rtlsdr_set_center_freq(dev, int(freq_hz)), "set_center_freq")
        if ppm:
            self._check(lib.rtlsdr_set_freq_correction(dev, int(ppm)), "set_freq_correction", ok=(0, -2))
        self._check(lib.rtlsdr_set_agc_mode(dev, 1 if agc else 0), "set_agc_mode")
        applied: float | None = None
        if str(gain).lower() == "auto":
            self._check(lib.rtlsdr_set_tuner_gain_mode(dev, 0), "set_tuner_gain_mode")
        else:
            gains = self.tuner_gains()
            if str(gain).lower() == "max":
                tenth = max(gains) if gains else 496
            else:
                tenth = nearest_gain(gains, float(gain)) if gains else int(round(float(gain) * 10))
            self._check(lib.rtlsdr_set_tuner_gain_mode(dev, 1), "set_tuner_gain_mode")
            self._check(lib.rtlsdr_set_tuner_gain(dev, tenth), "set_tuner_gain")
            applied = tenth / 10.0
        bias = getattr(lib, "rtlsdr_set_bias_tee", None)
        if bias_tee:
            if bias is None:
                raise RtlSdrError("This librtlsdr build does not support the bias tee (rtlsdr_set_bias_tee).")
            self._check(bias(dev, 1), "set_bias_tee")
        elif bias is not None:
            bias(dev, 0)
        self._check(lib.rtlsdr_reset_buffer(dev), "reset_buffer")
        return applied

    def read_async(self, on_data: Callable[[bytes], None], buf_len: int = _BUF_LEN) -> None:
        """Stream cu8 data into `on_data` until `cancel()` is called. Blocks."""
        failure: list[BaseException] = []

        def trampoline(buf, length, _ctx):
            try:
                on_data(ctypes.string_at(buf, length))
            except BaseException as exc:  # an exception must not unwind into C code
                failure.append(exc)
                self.cancel()

        self._callback = _READ_CB(trampoline)
        try:
            rc = self.lib.rtlsdr_read_async(self._dev, self._callback, None, 0, int(buf_len))
        finally:
            self._callback = None
        if failure:
            raise failure[0]
        if rc != 0:
            raise RtlSdrError(f"read_async stopped with code {rc} (device unplugged?)")

    def cancel(self) -> None:
        """Ask a running `read_async` to stop. Safe from any thread, and after `close()`."""
        with self._lock:
            if not self._closed:
                self.lib.rtlsdr_cancel_async(self._dev)

    def close(self) -> None:
        # The flag is flipped under the lock so a cancel() in flight on another thread finishes first;
        # rtlsdr_close itself runs outside it because librtlsdr waits there for the stream to wind down.
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.lib.rtlsdr_close(self._dev)
