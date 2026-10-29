"""
Analog Discovery 2 record-mode streaming benchmark.

Streams both scope channels continuously at a set of sample rates and reports
lost / corrupted samples. Only the scope is used - supplies and generators stay off.

    python3 scripts/stream_benchmark.py [seconds_per_rate] [rate ...]
"""

import sys
import time
from ctypes import CDLL, byref, c_byte, c_double, c_int, c_short, create_string_buffer

dwf = CDLL("libdwf.so")

ACQMODE_RECORD = c_int(3)
FILTER_DECIMATE = c_int(0)
FILTER_AVERAGE = c_int(1)
STATE_CONFIG, STATE_PREFILL, STATE_ARMED = 4, 5, 1


def last_error():
    msg = create_string_buffer(512)
    dwf.FDwfGetLastErrorMsg(msg)
    return msg.value.decode(errors="replace")


def open_device():
    hdwf = c_int()
    dwf.FDwfDeviceOpen(c_int(-1), byref(hdwf))
    if hdwf.value == 0:
        sys.exit("failed to open device: " + last_error())
    dwf.FDwfDeviceAutoConfigureSet(hdwf, c_int(0))
    return hdwf


def run(hdwf, rate, seconds, filt):
    for ch in (0, 1):
        dwf.FDwfAnalogInChannelEnableSet(hdwf, c_int(ch), c_int(1))
        dwf.FDwfAnalogInChannelRangeSet(hdwf, c_int(ch), c_double(5))
        dwf.FDwfAnalogInChannelFilterSet(hdwf, c_int(ch), filt)
    dwf.FDwfAnalogInAcquisitionModeSet(hdwf, ACQMODE_RECORD)
    dwf.FDwfAnalogInFrequencySet(hdwf, c_double(rate))
    dwf.FDwfAnalogInRecordLengthSet(hdwf, c_double(-1))  # infinite
    dwf.FDwfAnalogInConfigure(hdwf, c_int(1), c_int(0))
    time.sleep(0.5)  # let offsets settle

    real_rate = c_double()
    dwf.FDwfAnalogInFrequencyGet(hdwf, byref(real_rate))

    chunk = (c_short * 65536)()
    sts = c_byte()
    avail, lost, corrupt = c_int(), c_int(), c_int()
    n_samples = n_lost = n_corrupt = n_polls = 0
    max_avail = 0

    dwf.FDwfAnalogInConfigure(hdwf, c_int(0), c_int(1))
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        dwf.FDwfAnalogInStatus(hdwf, c_int(1), byref(sts))
        if n_samples == 0 and sts.value in (STATE_CONFIG, STATE_PREFILL, STATE_ARMED):
            continue
        dwf.FDwfAnalogInStatusRecord(hdwf, byref(avail), byref(lost), byref(corrupt))
        n_polls += 1
        n_lost += lost.value
        n_corrupt += corrupt.value
        max_avail = max(max_avail, avail.value)
        a = min(avail.value, len(chunk))
        if a:
            for ch in (0, 1):
                dwf.FDwfAnalogInStatusData16(hdwf, c_int(ch), chunk, c_int(0), c_int(a))
            n_samples += a
    elapsed = time.perf_counter() - t0
    dwf.FDwfAnalogInConfigure(hdwf, c_int(0), c_int(0))

    return {
        "rate": real_rate.value,
        "got": n_samples / elapsed,
        "lost": n_lost,
        "corrupt": n_corrupt,
        "polls": n_polls / elapsed,
        "max_avail": max_avail,
    }


def main():
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
    rates = [float(r) for r in sys.argv[2:]] or [100e3, 200e3, 500e3, 1e6, 1.5e6, 2e6]

    hdwf = open_device()
    buf_min, buf_max = c_int(), c_int()
    dwf.FDwfAnalogInBufferSizeInfo(hdwf, byref(buf_min), byref(buf_max))
    print(f"scope buffer max: {buf_max.value} samples, {seconds:g} s per rate, 2 channels\n")
    print(f"{'filter':8} {'set [S/s]':>10} {'got [S/s]':>10} {'lost':>9} {'corrupt':>9} {'polls/s':>8} {'max avail':>9}")
    try:
        for name, filt in (("decimate", FILTER_DECIMATE), ("average", FILTER_AVERAGE)):
            for rate in rates:
                r = run(hdwf, rate, seconds, filt)
                flag = "  <-- LOSS" if r["lost"] or r["corrupt"] else ""
                print(f"{name:8} {r['rate']:10.0f} {r['got']:10.0f} {r['lost']:9d} {r['corrupt']:9d} "
                      f"{r['polls']:8.0f} {r['max_avail']:9d}{flag}", flush=True)
    finally:
        dwf.FDwfDeviceCloseAll()


if __name__ == "__main__":
    main()
