"""Test doubles shared by several test modules."""

import ctypes
import errno
import io
import threading


class FakeRtlLib:
    """Stands in for the CDLL object of librtlsdr."""

    GAINS = (0, 9, 14, 27, 37, 77, 87, 125, 144, 157, 166, 197, 207, 229, 254, 280, 297, 328, 338, 364, 372, 386, 402,
             421, 434, 439, 445, 480, 496)

    def __init__(self, devices=1, tuner=5, open_rc=0, blocks=(), set_ppm_rc=0, read_rc=0):
        self.devices = devices
        self.tuner = tuner
        self.open_rc = open_rc
        self.blocks = list(blocks)
        self.set_ppm_rc = set_ppm_rc
        self.read_rc = read_rc
        self.calls = []
        self.cancelled = False
        self.closed = False
        self.read_count = 0

    def rtlsdr_get_device_count(self):
        return self.devices

    def rtlsdr_get_device_name(self, index):
        return b"Generic RTL2832U OEM"

    def rtlsdr_get_device_usb_strings(self, index, manufacturer, product, serial):
        manufacturer.value, product.value, serial.value = b"Realtek", b"RTL2838UHIDIR", b"00000001"
        return 0

    def rtlsdr_open(self, pointer, index):
        pointer.contents.value = 0x1234
        return self.open_rc

    def rtlsdr_close(self, dev):
        self.closed = True
        return 0

    def rtlsdr_get_tuner_type(self, dev):
        return self.tuner

    def rtlsdr_get_tuner_gains(self, dev, values):
        if values is not None:
            for i, g in enumerate(self.GAINS):
                values[i] = g
        return len(self.GAINS)

    def _record(self, name, value=None):
        self.calls.append((name, value))
        return 0

    def rtlsdr_set_sample_rate(self, dev, rate):
        return self._record("rate", rate)

    def rtlsdr_set_center_freq(self, dev, freq):
        return self._record("freq", freq)

    def rtlsdr_set_freq_correction(self, dev, ppm):
        self.calls.append(("ppm", ppm))
        return self.set_ppm_rc

    def rtlsdr_set_agc_mode(self, dev, on):
        return self._record("agc", on)

    def rtlsdr_set_tuner_gain_mode(self, dev, manual):
        return self._record("gain_mode", manual)

    def rtlsdr_set_tuner_gain(self, dev, tenth):
        return self._record("gain", tenth)

    def rtlsdr_reset_buffer(self, dev):
        return self._record("reset")

    def rtlsdr_cancel_async(self, dev):
        self.cancelled = True
        return 0

    def rtlsdr_read_async(self, dev, callback, ctx, buf_num, buf_len):
        self.calls.append(("read_async", buf_len))
        for block in self.blocks:
            if self.cancelled:
                break
            buf = (ctypes.c_ubyte * len(block)).from_buffer_copy(block)
            callback(buf, len(block), None)
            self.read_count += 1
        return self.read_rc


class FakeRtlLibWithBias(FakeRtlLib):
    def rtlsdr_set_bias_tee(self, dev, on):
        return self._record("bias", on)


class SlowDisk(io.BytesIO):
    """An in-memory recording file whose writes can be held back (`hold`) or made to fail (`fail_after`)."""

    def __init__(self, fail_after=None):
        super().__init__()
        self.released = threading.Event()
        self.released.set()
        self.busy = threading.Event()  # set as soon as a write has started
        self.fail_after = fail_after
        self.writes = 0
        self.saved = b""

    def hold(self):
        self.released.clear()

    def write(self, data):
        self.busy.set()
        assert self.released.wait(10), "the test never released the disk"
        if self.fail_after is not None and self.writes >= self.fail_after:
            raise OSError(errno.ENOSPC, "No space left on device")
        self.writes += 1
        return super().write(data)

    def close(self):
        self.saved = self.getvalue()  # a closed BytesIO forgets its content
        super().close()
