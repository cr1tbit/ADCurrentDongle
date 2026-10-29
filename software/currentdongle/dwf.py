"""Minimal ctypes binding for the WaveForms SDK (libdwf) - only what the dongle needs."""

import sys
from ctypes import (CDLL, POINTER, byref, c_byte, c_char_p, c_double, c_int, c_ubyte, c_void_p,
                    create_string_buffer)
from dataclasses import dataclass

import numpy as np

if sys.platform.startswith("win"):
    _lib = CDLL("dwf.dll")
elif sys.platform.startswith("darwin"):
    _lib = CDLL("/Library/Frameworks/dwf.framework/dwf")
else:
    _lib = CDLL("libdwf.so")

_HDWF = c_int
_PD = POINTER(c_double)
_PI = POINTER(c_int)

_SIGNATURES = {
    "FDwfGetLastErrorMsg": [c_char_p],
    "FDwfGetVersion": [c_char_p],
    "FDwfEnum": [c_int, _PI],
    "FDwfEnumSN": [c_int, c_char_p],
    "FDwfEnumDeviceName": [c_int, c_char_p],
    "FDwfEnumConfig": [c_int, _PI],
    "FDwfEnumConfigInfo": [c_int, c_int, _PI],
    "FDwfDeviceConfigOpen": [c_int, c_int, POINTER(_HDWF)],
    "FDwfDeviceClose": [_HDWF],
    "FDwfDeviceAutoConfigureSet": [_HDWF, c_int],
    "FDwfDeviceParamSet": [_HDWF, c_int, c_int],
    "FDwfAnalogInReset": [_HDWF],
    "FDwfAnalogInConfigure": [_HDWF, c_int, c_int],
    "FDwfAnalogInStatus": [_HDWF, c_int, POINTER(c_byte)],
    "FDwfAnalogInStatusRecord": [_HDWF, _PI, _PI, _PI],
    "FDwfAnalogInStatusData": [_HDWF, c_int, _PD, c_int],
    "FDwfAnalogInFrequencySet": [_HDWF, c_double],
    "FDwfAnalogInFrequencyGet": [_HDWF, _PD],
    "FDwfAnalogInBufferSizeInfo": [_HDWF, _PI, _PI],
    "FDwfAnalogInAcquisitionModeSet": [_HDWF, c_int],
    "FDwfAnalogInRecordLengthSet": [_HDWF, c_double],
    "FDwfAnalogInChannelEnableSet": [_HDWF, c_int, c_int],
    "FDwfAnalogInChannelFilterSet": [_HDWF, c_int, c_int],
    "FDwfAnalogInChannelRangeSet": [_HDWF, c_int, c_double],
    "FDwfAnalogInChannelRangeGet": [_HDWF, c_int, _PD],
    "FDwfAnalogInChannelOffsetSet": [_HDWF, c_int, c_double],
    "FDwfAnalogInChannelOffsetGet": [_HDWF, c_int, _PD],
    "FDwfAnalogIOReset": [_HDWF],
    "FDwfAnalogIOConfigure": [_HDWF],
    "FDwfAnalogIOStatus": [_HDWF],
    "FDwfAnalogIOEnableSet": [_HDWF, c_int],
    "FDwfAnalogIOEnableStatus": [_HDWF, _PI],
    "FDwfAnalogIOChannelNodeSet": [_HDWF, c_int, c_int, c_double],
    "FDwfAnalogIOChannelNodeStatus": [_HDWF, c_int, c_int, _PD],
    "FDwfAnalogOutReset": [_HDWF, c_int],
    "FDwfAnalogOutConfigure": [_HDWF, c_int, c_int],
    "FDwfAnalogOutStatus": [_HDWF, c_int, POINTER(c_byte)],
    "FDwfAnalogOutNodeEnableSet": [_HDWF, c_int, c_int, c_int],
    "FDwfAnalogOutNodeFunctionSet": [_HDWF, c_int, c_int, c_ubyte],
    "FDwfAnalogOutNodeFrequencySet": [_HDWF, c_int, c_int, c_double],
    "FDwfAnalogOutNodeAmplitudeSet": [_HDWF, c_int, c_int, c_double],
    "FDwfAnalogOutNodeOffsetSet": [_HDWF, c_int, c_int, c_double],
    "FDwfAnalogOutNodeSymmetrySet": [_HDWF, c_int, c_int, c_double],
    "FDwfAnalogOutRunSet": [_HDWF, c_int, c_double],
    "FDwfAnalogOutWaitSet": [_HDWF, c_int, c_double],
    "FDwfAnalogOutRepeatSet": [_HDWF, c_int, c_int],
    "FDwfAnalogOutIdleSet": [_HDWF, c_int, c_int],
    "FDwfAnalogOutNodeDataInfo": [_HDWF, c_int, c_int, _PI, _PI],
    "FDwfAnalogOutNodeDataSet": [_HDWF, c_int, c_int, _PD, c_int],
    # scope single-shot + trigger
    "FDwfAnalogInBufferSizeSet": [_HDWF, c_int],
    "FDwfAnalogInStatusSamplesValid": [_HDWF, _PI],
    "FDwfAnalogInTriggerSourceSet": [_HDWF, c_ubyte],
    "FDwfAnalogInTriggerPositionSet": [_HDWF, c_double],
    "FDwfAnalogInTriggerAutoTimeoutSet": [_HDWF, c_double],
    "FDwfAnalogInTriggerTypeSet": [_HDWF, c_int],
    "FDwfAnalogInTriggerChannelSet": [_HDWF, c_int],
    "FDwfAnalogInTriggerConditionSet": [_HDWF, c_int],
    "FDwfAnalogInTriggerLevelSet": [_HDWF, c_double],
    # logic analyzer single-shot (clip flags on DIO)
    "FDwfDigitalInReset": [_HDWF],
    "FDwfDigitalInConfigure": [_HDWF, c_int, c_int],
    "FDwfDigitalInStatus": [_HDWF, c_int, POINTER(c_byte)],
    "FDwfDigitalInStatusData": [_HDWF, c_void_p, c_int],
    "FDwfDigitalInInternalClockInfo": [_HDWF, _PD],
    "FDwfDigitalInDividerSet": [_HDWF, c_int],
    "FDwfDigitalInSampleFormatSet": [_HDWF, c_int],
    "FDwfDigitalInBufferSizeSet": [_HDWF, c_int],
    "FDwfDigitalInBufferSizeInfo": [_HDWF, _PI],
    "FDwfDigitalInAcquisitionModeSet": [_HDWF, c_int],
    "FDwfDigitalInTriggerSourceSet": [_HDWF, c_ubyte],
    "FDwfDigitalInTriggerPositionSet": [_HDWF, c_int],
}
for _name, _args in _SIGNATURES.items():
    _fn = getattr(_lib, _name)
    _fn.argtypes = _args
    _fn.restype = c_int

ACQMODE_SINGLE = 0
ACQMODE_RECORD = 3
FILTER = {"decimate": 0, "average": 1, "minmax": 2}
STATE_CONFIG, STATE_PREFILL, STATE_ARMED = 4, 5, 1
PARAM_ON_CLOSE = 4  # 0 continue, 1 stop, 2 shutdown
ENUM_CFG_AIN_BUFFER = 7

# Trigger sources (BYTE). A single-shot capture armed with TRIGSRC_ANALOG_OUT1 fires the
# instant the generator (W1) starts, so the capture window is synced to the applied sequence
# on the device itself - the PC only arms and reads back afterwards.
TRIGSRC_NONE = 0
TRIGSRC_DETECTOR_ANALOG_IN = 2   # trigger on an analog channel crossing a level (the pulse itself)
TRIGSRC_ANALOG_OUT1 = 7          # trigger when W1 starts
TRIGTYPE_EDGE = 0
TRIGCOND_RISING, TRIGCOND_FALLING = 0, 1
FUNC_CUSTOM = 31                 # arbitrary sample playback (defined glitch/rail sequences)

# AnalogIO channels/nodes of the Analog Discovery 2
AIO_VPOS, AIO_USB, AIO_AUX = 0, 2, 3
NODE_ENABLE, NODE_VOLTAGE = 0, 1

# Waveform generator: W1 (= WAV_GEN1 on the dongle), carrier node
GEN_CH = 0
NODE_CARRIER = 0
FUNC_DC, FUNC_PULSE = 0, 7
IDLE_DISABLE = 0  # output 0 V while not running
STATE_DONE, STATE_RUNNING = 2, 3


class DwfError(RuntimeError):
    pass


class DeviceNotFound(DwfError):
    pass


def last_error() -> str:
    msg = create_string_buffer(512)
    _lib.FDwfGetLastErrorMsg(msg)
    return msg.value.decode(errors="replace").strip()


def _call(name, *args):
    if not getattr(_lib, name)(*args):
        raise DwfError(f"{name} failed: {last_error()}")


def version() -> str:
    buf = create_string_buffer(32)
    _lib.FDwfGetVersion(buf)
    return buf.value.decode()


def enumerate_devices() -> list[dict]:
    n = c_int()
    _call("FDwfEnum", 0, byref(n))
    devices = []
    for i in range(n.value):
        sn, name = create_string_buffer(32), create_string_buffer(64)
        _lib.FDwfEnumSN(i, sn)
        _lib.FDwfEnumDeviceName(i, name)
        n_cfg = c_int()
        _lib.FDwfEnumConfig(i, byref(n_cfg))
        buffers = []
        for c in range(n_cfg.value):
            v = c_int()
            _lib.FDwfEnumConfigInfo(c, ENUM_CFG_AIN_BUFFER, byref(v))
            buffers.append(v.value)
        devices.append({"index": i, "serial": sn.value.decode(), "name": name.value.decode(),
                        "ain_buffer_per_config": buffers})
    return devices


@dataclass
class ScopeSetup:
    rate: float
    ranges: tuple[float, float]
    offsets: tuple[float, float]
    buffer_size: int


class AD2:
    """Analog Discovery 2 handle. Use from a single process/thread only."""

    def __init__(self, device_index: int = -1, config: int = 1):
        self.h = _HDWF()
        n = c_int()
        _call("FDwfEnum", 0, byref(n))  # refresh the device list (needed after a USB drop)
        if n.value == 0:
            raise DeviceNotFound("no WaveForms device connected")
        _call("FDwfDeviceConfigOpen", device_index, config, byref(self.h))
        if self.h.value == 0:
            raise DwfError(f"failed to open device: {last_error()}")
        _call("FDwfDeviceAutoConfigureSet", self.h, 0)
        self._sts = c_byte()
        self._avail, self._lost, self._corrupt = c_int(), c_int(), c_int()

    # --- power supply -------------------------------------------------------

    def set_supply(self, volts: float, enable: bool = True):
        _call("FDwfAnalogIOChannelNodeSet", self.h, AIO_VPOS, NODE_VOLTAGE, volts)
        _call("FDwfAnalogIOChannelNodeSet", self.h, AIO_VPOS, NODE_ENABLE, float(enable))
        _call("FDwfAnalogIOEnableSet", self.h, int(enable))
        _call("FDwfAnalogIOConfigure", self.h)

    def monitor(self) -> dict:
        """USB/AUX supply, device temperature and whether the supplies are still enabled."""
        _call("FDwfAnalogIOStatus", self.h)
        vals = {}
        for key, ch, node in (("usb_v", AIO_USB, 0), ("usb_a", AIO_USB, 1), ("temp_c", AIO_USB, 2),
                              ("aux_v", AIO_AUX, 0), ("aux_a", AIO_AUX, 1)):
            d = c_double()
            _lib.FDwfAnalogIOChannelNodeStatus(self.h, ch, node, byref(d))
            vals[key] = d.value
        en = c_int()
        _lib.FDwfAnalogIOEnableStatus(self.h, byref(en))
        vals["supply_enabled"] = bool(en.value)
        return vals

    # --- scope record mode --------------------------------------------------

    def configure_record(self, rate: float, offsets: tuple[float, float],
                         volts_range: float = 5.0, filt: str = "average") -> ScopeSetup:
        _call("FDwfAnalogInReset", self.h)
        for ch in (0, 1):
            _call("FDwfAnalogInChannelEnableSet", self.h, ch, 1)
            _call("FDwfAnalogInChannelFilterSet", self.h, ch, FILTER[filt])
            _call("FDwfAnalogInChannelRangeSet", self.h, ch, volts_range)
            _call("FDwfAnalogInChannelOffsetSet", self.h, ch, offsets[ch])
        _call("FDwfAnalogInAcquisitionModeSet", self.h, ACQMODE_RECORD)
        _call("FDwfAnalogInFrequencySet", self.h, rate)
        _call("FDwfAnalogInRecordLengthSet", self.h, -1.0)  # infinite
        _call("FDwfAnalogInConfigure", self.h, 1, 0)

        real_rate, r0, r1, o0, o1 = (c_double() for _ in range(5))
        _call("FDwfAnalogInFrequencyGet", self.h, byref(real_rate))
        _call("FDwfAnalogInChannelRangeGet", self.h, 0, byref(r0))
        _call("FDwfAnalogInChannelRangeGet", self.h, 1, byref(r1))
        _call("FDwfAnalogInChannelOffsetGet", self.h, 0, byref(o0))
        _call("FDwfAnalogInChannelOffsetGet", self.h, 1, byref(o1))
        bmin, bmax = c_int(), c_int()
        _call("FDwfAnalogInBufferSizeInfo", self.h, byref(bmin), byref(bmax))
        return ScopeSetup(real_rate.value, (r0.value, r1.value), (o0.value, o1.value), bmax.value)

    def start(self):
        _call("FDwfAnalogInConfigure", self.h, 0, 1)

    def stop(self):
        _lib.FDwfAnalogInConfigure(self.h, 0, 0)

    def poll(self) -> tuple[int, int, int, int]:
        """Returns (state, available, lost, corrupted)."""
        _call("FDwfAnalogInStatus", self.h, 1, byref(self._sts))
        _call("FDwfAnalogInStatusRecord", self.h, byref(self._avail), byref(self._lost), byref(self._corrupt))
        return self._sts.value, self._avail.value, self._lost.value, self._corrupt.value

    def read(self, channel: int, out: np.ndarray, n: int):
        """Copy n available samples [V] of a channel into float64 array `out`."""
        _call("FDwfAnalogInStatusData", self.h, channel, out.ctypes.data_as(_PD), n)

    # --- scope single-shot (triggered) --------------------------------------

    def configure_single(self, rate: float, n_samples: int, offsets: tuple[float, float],
                         volts_range: float = 5.0, filt: str = "decimate") -> ScopeSetup:
        """Single acquisition: fill one buffer of n_samples at `rate`, then stop. Much faster
        than record mode (no streaming), limited by the scope buffer (16k in device config 1)."""
        _call("FDwfAnalogInReset", self.h)
        for ch in (0, 1):
            _call("FDwfAnalogInChannelEnableSet", self.h, ch, 1)
            _call("FDwfAnalogInChannelFilterSet", self.h, ch, FILTER[filt])
            _call("FDwfAnalogInChannelRangeSet", self.h, ch, volts_range)
            _call("FDwfAnalogInChannelOffsetSet", self.h, ch, offsets[ch])
        _call("FDwfAnalogInAcquisitionModeSet", self.h, ACQMODE_SINGLE)
        _call("FDwfAnalogInFrequencySet", self.h, rate)
        _call("FDwfAnalogInBufferSizeSet", self.h, n_samples)
        _call("FDwfAnalogInConfigure", self.h, 1, 0)  # apply, don't arm yet

        real_rate, r0, r1, o0, o1 = (c_double() for _ in range(5))
        _call("FDwfAnalogInFrequencyGet", self.h, byref(real_rate))
        _call("FDwfAnalogInChannelRangeGet", self.h, 0, byref(r0))
        _call("FDwfAnalogInChannelRangeGet", self.h, 1, byref(r1))
        _call("FDwfAnalogInChannelOffsetGet", self.h, 0, byref(o0))
        _call("FDwfAnalogInChannelOffsetGet", self.h, 1, byref(o1))
        return ScopeSetup(real_rate.value, (r0.value, r1.value), (o0.value, o1.value), n_samples)

    def set_scope_trigger(self, source: int, position_s: float = 0.0, timeout_s: float = 0.0,
                          level_v: float | None = None, channel: int = 0, rising: bool = True):
        """Arm-time trigger config. The trigger lands at buffer sample N/2 - position_s * rate
        (positive => trigger earlier, more post-trigger data; checked on the bench 2026-10-26).
        timeout_s = 0 waits forever, else auto-triggers after it.
        For TRIGSRC_DETECTOR_ANALOG_IN give an edge level_v on `channel`."""
        _call("FDwfAnalogInTriggerSourceSet", self.h, source)
        _call("FDwfAnalogInTriggerPositionSet", self.h, position_s)
        _call("FDwfAnalogInTriggerAutoTimeoutSet", self.h, timeout_s)
        if source == TRIGSRC_DETECTOR_ANALOG_IN and level_v is not None:
            _call("FDwfAnalogInTriggerTypeSet", self.h, TRIGTYPE_EDGE)
            _call("FDwfAnalogInTriggerChannelSet", self.h, channel)
            _call("FDwfAnalogInTriggerConditionSet", self.h,
                  TRIGCOND_RISING if rising else TRIGCOND_FALLING)
            _call("FDwfAnalogInTriggerLevelSet", self.h, level_v)
        _call("FDwfAnalogInConfigure", self.h, 1, 0)

    def arm_scope(self):
        """Arm the single acquisition; it then waits for the trigger (do this before the master)."""
        _call("FDwfAnalogInConfigure", self.h, 0, 1)

    def scope_state(self) -> int:
        _call("FDwfAnalogInStatus", self.h, 1, byref(self._sts))
        return self._sts.value

    def read_single(self, channel: int, n: int) -> np.ndarray:
        """Read the whole single-shot buffer of a channel [V] once the state is STATE_DONE."""
        out = np.empty(n, np.float64)
        _call("FDwfAnalogInStatusData", self.h, channel, out.ctypes.data_as(_PD), n)
        return out

    # --- logic analyzer single-shot (clip flags) ----------------------------

    def configure_digital_single(self, window_s: float, trigger_source: int,
                                 position_s: float = 0.0) -> tuple[float, int]:
        """Single logic capture of all 16 DIO lines over `window_s`, trigger in the middle like the
        scope's default. The logic buffer is small (1024 samples in device config 1), so the rate
        is chosen to span the window. position_s as for the scope (positive = trigger earlier in the
        buffer). Returns (real rate, samples)."""
        _call("FDwfDigitalInReset", self.h)
        base, mx = c_double(), c_int()
        _call("FDwfDigitalInInternalClockInfo", self.h, byref(base))
        _call("FDwfDigitalInBufferSizeInfo", self.h, byref(mx))
        n = mx.value
        div = max(1, int(np.ceil(base.value * window_s / n)))
        _call("FDwfDigitalInDividerSet", self.h, div)
        _call("FDwfDigitalInSampleFormatSet", self.h, 16)
        _call("FDwfDigitalInBufferSizeSet", self.h, n)
        _call("FDwfDigitalInAcquisitionModeSet", self.h, ACQMODE_SINGLE)
        _call("FDwfDigitalInTriggerSourceSet", self.h, trigger_source)
        after = int(round(n / 2 + position_s * base.value / div))  # samples after the trigger
        _call("FDwfDigitalInTriggerPositionSet", self.h, min(max(after, 0), n))
        _call("FDwfDigitalInConfigure", self.h, 1, 0)
        return base.value / div, n

    def arm_digital(self):
        _call("FDwfDigitalInConfigure", self.h, 0, 1)

    def digital_state(self) -> int:
        sts = c_byte()
        _call("FDwfDigitalInStatus", self.h, 1, byref(sts))
        return sts.value

    def read_digital(self, n: int) -> np.ndarray:
        """Read the whole single-shot buffer as uint16 (bit k = DIOk)."""
        out = np.empty(n, np.uint16)
        _call("FDwfDigitalInStatusData", self.h, out.ctypes.data_as(c_void_p), n * 2)
        return out

    # --- waveform generator (W1) ---------------------------------------------

    def gen_off(self):
        _call("FDwfAnalogOutReset", self.h, GEN_CH)
        _call("FDwfAnalogOutConfigure", self.h, GEN_CH, 0)

    def _gen_common(self, func: int):
        _call("FDwfAnalogOutReset", self.h, GEN_CH)
        _call("FDwfAnalogOutNodeEnableSet", self.h, GEN_CH, NODE_CARRIER, 1)
        _call("FDwfAnalogOutNodeFunctionSet", self.h, GEN_CH, NODE_CARRIER, func)
        _call("FDwfAnalogOutIdleSet", self.h, GEN_CH, IDLE_DISABLE)
        _call("FDwfAnalogOutWaitSet", self.h, GEN_CH, 0.0)

    def gen_dc(self, volts: float):
        self._gen_common(FUNC_DC)
        _call("FDwfAnalogOutNodeOffsetSet", self.h, GEN_CH, NODE_CARRIER, volts)
        _call("FDwfAnalogOutRunSet", self.h, GEN_CH, 0.0)    # continuous
        _call("FDwfAnalogOutRepeatSet", self.h, GEN_CH, 0)   # forever
        _call("FDwfAnalogOutConfigure", self.h, GEN_CH, 1)

    def gen_pulses(self, high_v: float, width_s: float, period_s: float, count: int):
        """Exactly `count` 0 -> high_v pulses, then 0 V (idle disabled)."""
        self._gen_common(FUNC_PULSE)
        _call("FDwfAnalogOutNodeFrequencySet", self.h, GEN_CH, NODE_CARRIER, 1.0 / period_s)
        _call("FDwfAnalogOutNodeAmplitudeSet", self.h, GEN_CH, NODE_CARRIER, high_v)
        _call("FDwfAnalogOutNodeOffsetSet", self.h, GEN_CH, NODE_CARRIER, 0.0)
        _call("FDwfAnalogOutNodeSymmetrySet", self.h, GEN_CH, NODE_CARRIER, 100.0 * width_s / period_s)
        _call("FDwfAnalogOutRunSet", self.h, GEN_CH, count * period_s)
        _call("FDwfAnalogOutRepeatSet", self.h, GEN_CH, 1)
        _call("FDwfAnalogOutConfigure", self.h, GEN_CH, 1)

    def gen_custom(self, samples: np.ndarray, rate: float, high_v: float, repeat: int = 1):
        """Play an arbitrary 0..1 sample sequence on W1 at `rate` S/s, scaled to 0..high_v.

        One period = all `samples`, played `repeat` times (then 0 V, idle disabled). This is the
        building block for a defined set/trigger sequence: hand it a V(t) table and it plays it
        once, deterministically, with the scope armed off TRIGSRC_ANALOG_OUT1."""
        s = np.clip(np.asarray(samples, np.float64), 0.0, 1.0) * 2.0 - 1.0  # SDK wants -1..1
        n = len(s)
        self._gen_common(FUNC_CUSTOM)
        _call("FDwfAnalogOutNodeDataSet", self.h, GEN_CH, NODE_CARRIER, s.ctypes.data_as(_PD), n)
        _call("FDwfAnalogOutNodeFrequencySet", self.h, GEN_CH, NODE_CARRIER, rate / n)
        _call("FDwfAnalogOutNodeAmplitudeSet", self.h, GEN_CH, NODE_CARRIER, high_v / 2.0)
        _call("FDwfAnalogOutNodeOffsetSet", self.h, GEN_CH, NODE_CARRIER, high_v / 2.0)
        _call("FDwfAnalogOutRunSet", self.h, GEN_CH, repeat * n / rate)
        _call("FDwfAnalogOutRepeatSet", self.h, GEN_CH, 1)
        _call("FDwfAnalogOutConfigure", self.h, GEN_CH, 1)

    def gen_buffer_size(self) -> int:
        """Samples per period the generator uses for standard waveforms (depends on the device config)."""
        mn, mx = c_int(), c_int()
        _call("FDwfAnalogOutNodeDataInfo", self.h, GEN_CH, NODE_CARRIER, byref(mn), byref(mx))
        return mx.value

    def gen_state(self) -> int:
        sts = c_byte()
        _call("FDwfAnalogOutStatus", self.h, GEN_CH, byref(sts))
        return sts.value

    def close(self, keep_supply: bool = False):
        if self.h.value == 0:
            return
        self.stop()
        try:
            self.gen_off()  # never leave the gate driven
        except DwfError:
            pass
        if keep_supply:
            _lib.FDwfDeviceParamSet(self.h, PARAM_ON_CLOSE, 0)
        else:
            _lib.FDwfAnalogIOEnableSet(self.h, 0)
            _lib.FDwfAnalogIOConfigure(self.h)
        _lib.FDwfDeviceClose(self.h)
        self.h = _HDWF(0)

    def abandon(self):
        """Release the handle of a device that dropped off USB, without talking to it."""
        if self.h.value:
            _lib.FDwfDeviceClose(self.h)
        self.h = _HDWF(0)
