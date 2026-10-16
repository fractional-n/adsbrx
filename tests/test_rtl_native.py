import ctypes
import os
import threading

import pytest
from fakes import FakeRtlLib, FakeRtlLibWithBias

from adsbrx import rtl_native
from adsbrx.rtl_native import RtlSdrDevice, RtlSdrError, list_devices, load_library, nearest_gain


def test_list_devices():
    devices = list_devices(FakeRtlLib(devices=2))
    assert [d.index for d in devices] == [0, 1]
    assert devices[0].name == "Generic RTL2832U OEM"
    assert (devices[0].manufacturer, devices[0].product, devices[0].serial) == (
        "Realtek", "RTL2838UHIDIR", "00000001",
    )
    assert list_devices(FakeRtlLib(devices=0)) == []


def test_nearest_gain():
    assert nearest_gain([0, 9, 14, 27, 402, 496], 40.0) == 402
    assert nearest_gain([0, 9, 14, 27, 402, 496], 100.0) == 496
    assert nearest_gain([0, 9, 14, 27, 402, 496], 0.0) == 0


class TestDevice:
    def test_open_reports_name_and_tuner(self):
        dev = RtlSdrDevice(0, lib=FakeRtlLib())
        assert dev.name == "Generic RTL2832U OEM" and dev.tuner == "R820T"
        assert dev.tuner_gains()[-1] == 496

    def test_unknown_tuner(self):
        assert RtlSdrDevice(0, lib=FakeRtlLib(tuner=99)).tuner == "unknown"

    def test_no_device_gives_helpful_error(self):
        with pytest.raises(RtlSdrError, match="No RTL-SDR device found.*WinUSB"):
            RtlSdrDevice(0, lib=FakeRtlLib(devices=0))

    def test_bad_index(self):
        with pytest.raises(RtlSdrError, match="index 3"):
            RtlSdrDevice(3, lib=FakeRtlLib(devices=2))

    def test_open_failure(self):
        with pytest.raises(RtlSdrError, match="rtlsdr_open failed.*code -6"):
            RtlSdrDevice(0, lib=FakeRtlLib(open_rc=-6))

    def test_configure_max_gain(self):
        lib = FakeRtlLib()
        applied = RtlSdrDevice(0, lib=lib).configure(1_090_000_000, 2_000_000, "max", ppm=0)
        assert applied == 49.6
        assert lib.calls == [
            ("rate", 2_000_000), ("freq", 1_090_000_000), ("agc", 0),
            ("gain_mode", 1), ("gain", 496), ("reset", None),
        ]

    def test_configure_numeric_gain_snaps_to_supported_value(self):
        lib = FakeRtlLib()
        assert RtlSdrDevice(0, lib=lib).configure(gain=40) == 40.2
        assert ("gain", 402) in lib.calls

    def test_configure_auto_gain(self):
        lib = FakeRtlLib()
        assert RtlSdrDevice(0, lib=lib).configure(gain="auto", agc=True) is None
        assert ("gain_mode", 0) in lib.calls and ("agc", 1) in lib.calls
        assert not any(name == "gain" for name, _ in lib.calls)

    def test_ppm_unchanged_return_code_is_accepted(self):
        lib = FakeRtlLib(set_ppm_rc=-2)
        RtlSdrDevice(0, lib=lib).configure(ppm=45)
        assert ("ppm", 45) in lib.calls

    def test_ppm_error(self):
        with pytest.raises(RtlSdrError, match="set_freq_correction"):
            RtlSdrDevice(0, lib=FakeRtlLib(set_ppm_rc=-1)).configure(ppm=45)

    def test_bias_tee(self):
        lib = FakeRtlLibWithBias()
        RtlSdrDevice(0, lib=lib).configure(bias_tee=True)
        assert ("bias", 1) in lib.calls
        with pytest.raises(RtlSdrError, match="bias tee"):
            RtlSdrDevice(0, lib=FakeRtlLib()).configure(bias_tee=True)

    def test_read_async_delivers_blocks(self):
        blocks = [bytes([i]) * 1000 for i in range(5)]
        lib = FakeRtlLib(blocks=blocks)
        got = []
        RtlSdrDevice(0, lib=lib).read_async(got.append)
        assert got == blocks
        assert ("read_async", 262144) in lib.calls

    def test_exception_in_callback_cancels_and_propagates(self):
        lib = FakeRtlLib(blocks=[b"\x00" * 10] * 5)

        def boom(data):
            raise ValueError("sink failed")

        with pytest.raises(ValueError, match="sink failed"):
            RtlSdrDevice(0, lib=lib).read_async(boom)
        assert lib.cancelled and lib.read_count == 1

    def test_cancel_and_close_are_safe_to_repeat(self):
        lib = FakeRtlLib()
        dev = RtlSdrDevice(0, lib=lib)
        dev.cancel()
        dev.close()
        dev.close()
        dev.cancel()
        assert lib.closed

    def test_cancel_after_close_never_reaches_the_library(self):
        lib = FakeRtlLib()
        dev = RtlSdrDevice(0, lib=lib)
        dev.close()
        dev.cancel()
        assert not lib.cancelled

    def test_close_waits_for_a_cancel_in_flight(self):
        entered, release = threading.Event(), threading.Event()

        class SlowCancelLib(FakeRtlLib):
            def rtlsdr_cancel_async(self, dev):
                entered.set()
                assert release.wait(10)
                return super().rtlsdr_cancel_async(dev)

        lib = SlowCancelLib()
        dev = RtlSdrDevice(0, lib=lib)
        canceller = threading.Thread(target=dev.cancel)
        closer = threading.Thread(target=dev.close)
        canceller.start()
        assert entered.wait(10)
        closer.start()
        closer.join(0.3)
        assert closer.is_alive() and not lib.closed
        release.set()
        canceller.join(10)
        closer.join(10)
        assert lib.cancelled and lib.closed


class TestLoadLibrary:
    def test_helpful_error_when_missing(self, monkeypatch):
        def refuse(*args, **kwargs):
            raise OSError("not found")

        monkeypatch.setattr(rtl_native.ctypes, "CDLL", refuse)
        monkeypatch.setattr(rtl_native.ctypes.util, "find_library", lambda name: None)
        with pytest.raises(RtlSdrError, match="librtlsdr was not found.*--dll-dir"):
            load_library(r"C:\does\not\exist")

    def test_candidates_use_explicit_directory_first(self, tmp_path, monkeypatch):
        (tmp_path / rtl_native._library_names()[0]).write_bytes(b"")
        monkeypatch.delenv(rtl_native.ENV_DLL_DIR, raising=False)
        assert rtl_native._candidate_paths(str(tmp_path))[0] == str(tmp_path / rtl_native._library_names()[0])

    def test_environment_variable_directory(self, tmp_path, monkeypatch):
        (tmp_path / rtl_native._library_names()[0]).write_bytes(b"")
        monkeypatch.setenv(rtl_native.ENV_DLL_DIR, str(tmp_path))
        assert str(tmp_path / rtl_native._library_names()[0]) in rtl_native._candidate_paths(None)

    @pytest.mark.skipif(
        not os.environ.get("ADSBRX_TEST_RTLSDR_DLL_DIR"), reason="set ADSBRX_TEST_RTLSDR_DLL_DIR to test a real librtlsdr"
    )
    def test_real_library_loads_and_enumerates(self):
        lib = load_library(os.environ["ADSBRX_TEST_RTLSDR_DLL_DIR"])
        assert lib.rtlsdr_get_device_count() >= 0
        assert isinstance(list_devices(lib), list)
        assert ctypes.sizeof(ctypes.c_void_p) in (4, 8)
