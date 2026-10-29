import queue
import threading
import time

import numpy as np

from currentdongle import dwf
from currentdongle.acquisition import AcqConfig, run
from currentdongle.processing import Calibration, Processor


class FakeAD2:
    """Streams a constant 1 V / 0.1 V, fails mid-stream, then is missing for a few opens."""

    RATE = 10_000.0
    plan = {}

    def __init__(self, config=1):
        p = FakeAD2.plan
        p["opens"] = p.get("opens", 0) + 1
        if p["opens"] in p.get("missing_on_opens", ()):
            raise dwf.DeviceNotFound("gone")
        self.fail_after = p.get("fail_after", {}).get(p["opens"])
        self.t0 = None
        self.sent = 0

    def monitor(self):
        return {"usb_v": 5.0, "usb_a": 0.3, "temp_c": 40.0, "aux_v": 0.0, "aux_a": 0.0, "supply_enabled": True}

    def set_supply(self, volts, enable=True):
        FakeAD2.plan.setdefault("supply", []).append(volts)

    def configure_record(self, rate, offsets, volts_range=5.0, filt="average"):
        return dwf.ScopeSetup(self.RATE, (5.5, 5.5), offsets, 16384)

    def start(self):
        self.t0 = time.monotonic()

    def stop(self):
        pass

    def poll(self):
        el = time.monotonic() - self.t0
        if self.fail_after is not None and el > self.fail_after:
            raise dwf.DwfError("FDwfAnalogInStatus failed: device disconnected")
        due = int(el * self.RATE) - self.sent
        avail = max(0, min(due, 16384))
        self.sent += avail
        time.sleep(0.001)
        return 2, avail, 0, 0

    def read(self, ch, out, n):
        out[:n] = 1.0 if ch == 0 else 0.1

    def gen_buffer_size(self):
        return 1024

    def gen_off(self):
        FakeAD2.plan.setdefault("gen", []).append(("off",))
        self.gen_until = None

    def gen_dc(self, volts):
        FakeAD2.plan.setdefault("gen", []).append(("dc", volts))
        self.gen_until = None

    def gen_pulses(self, high_v, width_s, period_s, count):
        FakeAD2.plan.setdefault("gen", []).append(("pulses", high_v, width_s, period_s, count))
        self.gen_until = time.monotonic() + count * period_s

    def gen_state(self):
        until = getattr(self, "gen_until", None)
        if until is None:
            return dwf.STATE_DONE
        return dwf.STATE_RUNNING if time.monotonic() < until else dwf.STATE_DONE

    def close(self, keep_supply=False):
        pass

    def abandon(self):
        pass


def _collect(cfg, seconds, ctrl=None, during=None):
    out, stop = queue.Queue(), threading.Event()
    th = threading.Thread(target=run, args=(cfg, out, stop, ctrl, FakeAD2))
    th.start()
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        if during:
            during(time.monotonic() - t0)
        time.sleep(0.05)
    stop.set()
    th.join(5)
    msgs = []
    while not out.empty():
        msgs.append(out.get())
    return msgs


def test_reconnects_after_usb_drop():
    FakeAD2.plan = {"fail_after": {1: 0.3}, "missing_on_opens": (2, 3)}
    cfg = AcqConfig(vdc=3.3, settle_s=0, retry_s=0.05, stall_s=0.5, block_s=0.01)
    msgs = _collect(cfg, 1.2)

    kinds = [m[0] for m in msgs]
    assert kinds[0] == "setup" and kinds[-1] == "closed" and "error" not in kinds
    events = [m[1]["type"] for m in msgs if m[0] == "event"]
    assert events.count("disconnected") == 1 and events.count("reconnected") == 1
    rec = next(m[1] for m in msgs if m[0] == "event" and m[1]["type"] == "reconnected")
    assert rec["down_s"] > 0.05
    assert FakeAD2.plan["supply"] == [3.3, 3.3]  # supply restored

    # block indices: contiguous, then one jump covering the downtime
    proc = Processor(Calibration.nominal(0.5, "2k"), FakeAD2.RATE, 3.15, 0.5e-3)
    for m in msgs:
        if m[0] == "block":
            proc.process(m[1])
    assert proc.gap_samples >= int(0.05 * FakeAD2.RATE)
    assert proc.gap_samples / FakeAD2.RATE < rec["down_s"] + 0.2
    assert not proc.summary()["valid"]


def test_first_open_fails_fast_without_device():
    FakeAD2.plan = {"missing_on_opens": range(1, 1000)}
    cfg = AcqConfig(vdc=3.3, settle_s=0, retry_s=0.05, open_timeout_s=0.2)
    t0 = time.monotonic()
    msgs = _collect(cfg, 0.5)
    assert time.monotonic() - t0 < 3
    assert any(m[0] == "error" and "DeviceNotFound" in m[1] for m in msgs)


def test_stall_counts_as_disconnect():
    class Stalling(FakeAD2):
        def poll(self):
            state, avail, lost, corrupt = super().poll()
            if FakeAD2.plan["opens"] == 1 and time.monotonic() - self.t0 > 0.2:
                return state, 0, 0, 0
            return state, avail, lost, corrupt

    FakeAD2.plan = {}
    out, stop = queue.Queue(), threading.Event()
    cfg = AcqConfig(vdc=3.3, settle_s=0, retry_s=0.05, stall_s=0.3, block_s=0.01)
    th = threading.Thread(target=run, args=(cfg, out, stop, None, Stalling))
    th.start()
    time.sleep(1.0)
    stop.set()
    th.join(5)
    events = []
    while not out.empty():
        k, p = out.get()
        if k == "event":
            events.append(p)
    lost = [e for e in events if e["type"] == "disconnected"]
    assert lost and "no data" in lost[0]["reason"]
    assert any(e["type"] == "reconnected" for e in events)


def test_set_vdc_control():
    FakeAD2.plan = {}
    ctrl = queue.Queue()
    cfg = AcqConfig(vdc=3.3, settle_s=0, retry_s=0.05, block_s=0.01)
    sent = []

    def during(t):
        if t > 0.2 and not sent:
            ctrl.put(("set_vdc", 2.5))
            sent.append(1)
    msgs = _collect(cfg, 0.6, ctrl, during)
    assert any(m[0] == "event" and m[1]["type"] == "vdc_set" and m[1]["vdc"] == 2.5 for m in msgs)
    assert FakeAD2.plan["supply"] == [3.3, 2.5]


def test_ring_and_rebin():
    from currentdongle.session import Ring, rebin
    r = Ring(100)
    for k in range(10):
        t = np.arange(k * 30, (k + 1) * 30, dtype=float)
        r.append([t, t, t, t])
    d = r.last(1e9)
    assert d.shape[1] <= 200 and d[0, -1] == 299
    np.testing.assert_array_equal(np.diff(d[0]), 1)
    last = r.last(9.5)
    assert last[0, 0] == 290
    t, mn, mean, mx = rebin(r.last(99), 10)
    assert len(t) == 10 and mn[0] == 200 and mx[-1] == 299
    # NaN gap marker survives re-binning
    d2 = np.array([np.arange(8.0), [0, 1, np.nan, 3, 4, 5, 6, 7], [0, 1, np.nan, 3, 4, 5, 6, 7],
                   [0, 1, np.nan, 3, 4, 5, 6, 7]])
    _, mn2, _, _ = rebin(d2, 4)
    assert np.isnan(mn2[1]) and mn2[0] == 0


def test_pulse_train_reports_done():
    FakeAD2.plan = {}
    ctrl = queue.Queue()
    cfg = AcqConfig(vdc=3.3, settle_s=0, retry_s=0.05, block_s=0.01)
    sent = []

    def during(t):
        if t > 0.1 and not sent:
            ctrl.put(("gen", {"mode": "pulses", "high_v": 5.0, "width_s": 1e-3, "period_s": 2e-3, "count": 100}))
            sent.append(1)
    msgs = _collect(cfg, 0.8, ctrl, during)
    ev = [m[1] for m in msgs if m[0] == "event"]
    done = [e for e in ev if e["type"] == "gen_done"]
    assert len(done) == 1 and 0.19 < done[0]["elapsed_s"] < 0.4
    assert ("pulses", 5.0, 1e-3, 2e-3, 100) in FakeAD2.plan["gen"]


def test_reconnect_restores_dc_but_not_a_train():
    FakeAD2.plan = {"fail_after": {1: 0.3}}
    ctrl = queue.Queue()
    ctrl.put(("gen", {"mode": "dc", "volts": 5.0}))
    cfg = AcqConfig(vdc=3.3, settle_s=0, retry_s=0.05, stall_s=0.5, block_s=0.01)
    msgs = _collect(cfg, 0.8, ctrl)
    ev = [m[1]["type"] for m in msgs if m[0] == "event"]
    assert "gen_restored" in ev
    assert FakeAD2.plan["gen"].count(("dc", 5.0)) == 2

    FakeAD2.plan = {"fail_after": {1: 0.3}}
    ctrl = queue.Queue()
    ctrl.put(("gen", {"mode": "pulses", "high_v": 5.0, "width_s": 1e-3, "period_s": 0.1, "count": 100}))
    msgs = _collect(cfg, 0.8, ctrl)
    ev = [m[1]["type"] for m in msgs if m[0] == "event"]
    assert "gen_aborted" in ev and "gen_restored" not in ev
    assert [g for g in FakeAD2.plan["gen"] if g[0] == "pulses"].__len__() == 1


def test_snap_period_and_validation():
    import pytest
    from currentdongle.pulsetest import snap_period, validate_gen
    # 1 ms in 10 ms = 102.4 samples of 1024 -> 102 -> period 10.0392 ms, width exact
    p = snap_period(1e-3, 10e-3, 1024)
    assert abs(1e-3 / p * 1024 - round(1e-3 / p * 1024)) < 1e-9
    assert abs(p - 10.0392e-3) < 1e-6
    assert snap_period(5e-3, 10e-3, 1024) == pytest.approx(10e-3)
    assert snap_period(1e-6, 1.0, 1024) == pytest.approx(1024e-6)  # at least one sample wide
    with pytest.raises(ValueError):
        validate_gen({"mode": "pulses", "high_v": 6, "width_s": 1e-3, "period_s": 1e-2, "count": 1})
    with pytest.raises(ValueError):
        validate_gen({"mode": "pulses", "high_v": 5, "width_s": 1e-2, "period_s": 1e-2, "count": 1})
    with pytest.raises(ValueError):
        validate_gen({"mode": "dc", "volts": -1})
    assert validate_gen({"mode": "off", "volts": 3}) == {"mode": "off"}


def test_binner_across_irregular_blocks():
    from currentdongle.session import Binner
    x = np.arange(1000, dtype=float)
    b = Binner(10, start_index=100)
    pos = 0
    for size in (7, 33, 1, 259, 600, 100):
        sl = slice(pos, pos + size)
        b.add(100 + pos, x[sl], -x[sl], 2 * x[sl])
        pos += size
    a = b.array(rate=1000.0)
    assert a.shape == (100, 10)
    np.testing.assert_allclose(a[:, 0], (np.arange(100) * 10 + 5) / 1000.0)
    np.testing.assert_allclose(a[:, 1], np.arange(100) * 10 + 4.5)     # v500 mean
    np.testing.assert_allclose(a[:, 2], np.arange(100) * 10)           # v500 min
    np.testing.assert_allclose(a[:, 6], -(np.arange(100) * 10))        # v50 max
    np.testing.assert_allclose(a[:, 7], 2 * (np.arange(100) * 10 + 4.5))
