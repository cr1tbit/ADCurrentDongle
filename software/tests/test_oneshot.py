import json
from pathlib import Path

import numpy as np
import pytest

from currentdongle import dwf
from currentdongle.export import read_trace_csv, write_oneshot_csv
from currentdongle.oneshot import CaptureCfg, Plan, Runner, Step, load_plan, validate_gen

N = 1024


class FakeSingleAD2:
    """Single-shot fake: records the call order and returns a synthesised pulse.

    ch1 carries the hi (x500) signal, ch0 the lo (x50) one; a clip bit (DIO7) is set during
    the pulse. scope/digital report DONE only after the generator has started, which lets the
    test prove the slaves are armed before the master."""

    RATE = 10e6

    def __init__(self):
        self.events = []
        self.gen_started = False
        self.armed_scope = self.armed_dig = False
        self.n = N

    def set_supply(self, volts, enable=True):
        self.events.append(("supply", volts, enable))

    def monitor(self):
        return {"supply_enabled": True, "temp_c": 40.0}

    def configure_single(self, rate, n, offsets, volts_range=5.0, filt="decimate"):
        self.events.append("configure_single")
        self.n = n
        return dwf.ScopeSetup(self.RATE, (5.0, 5.0), offsets, n)

    def set_scope_trigger(self, source, position_s=0.0, timeout_s=0.0, level_v=None,
                          channel=0, rising=True):
        self.events.append(("trigger", source))

    def configure_digital_single(self, window_s, trigger_source, position_s=0.0):
        self.events.append("configure_digital")
        return self.RATE / 4, self.n // 4  # small logic buffer at a quarter of the rate

    def arm_scope(self):
        self.events.append("arm_scope")
        self.armed_scope = True

    def arm_digital(self):
        self.events.append("arm_digital")
        self.armed_dig = True

    def _apply(self, tag):
        assert self.armed_scope, "generator started before the scope was armed"
        self.events.append(tag)
        self.gen_started = True

    def gen_dc(self, volts):
        self._apply(("gen", "dc"))

    def gen_pulses(self, high_v, width_s, period_s, count):
        self._apply(("gen", "pulses"))

    def gen_custom(self, samples, rate, high_v, repeat=1):
        self._apply(("gen", "custom", len(samples)))

    def gen_off(self):
        self.events.append("gen_off")

    def scope_state(self):
        # one prefill poll after arming, then armed until the generator starts
        if self.armed_scope and not self.gen_started and not getattr(self, "_prefilled", False):
            self._prefilled = True
            return dwf.STATE_PREFILL
        return dwf.STATE_DONE if self.gen_started else dwf.STATE_ARMED

    def digital_state(self):
        return dwf.STATE_DONE if self.gen_started else dwf.STATE_ARMED

    def gen_state(self):
        return dwf.STATE_DONE if self.gen_started else 0  # Ready

    def read_single(self, ch, n):
        v = np.zeros(n)
        v[100:300] = 2.0 if ch == 1 else 0.2  # ch1 = hi (x500), ch0 = lo (x50)
        return v

    def read_digital(self, n):
        d = np.zeros(n, np.uint16)
        # same time span as scope samples 100:300 (both triggers mid-buffer, logic at 1/4 rate)
        d[(100 - self.n // 2) // 4 + n // 2:(300 - self.n // 2) // 4 + n // 2] = 1 << 7
        return d

    def close(self, keep_supply=False):
        self.events.append("close")


def plan(**cap):
    c = CaptureCfg(samples=N, **cap)
    steps = [Step(op="supply", enable=True), Step(op="settle", seconds=1.0),
             Step(op="capture", label="p", gen={"mode": "pulses", "high_v": 5.0,
                  "width_s": 10e-6, "period_s": 20e-3, "count": 1})]
    return Plan(name="t", vdc=3.3, shunt=0.5, sw3="2k", hi_channel=1, capture=c, steps=steps)


def run(p):
    dev = FakeSingleAD2()
    caps = Runner(dev, p, cal=None, log=lambda *a: None, sleep=lambda s: None).run()
    return dev, caps


def test_arms_slaves_before_master():
    dev, caps = run(plan())
    assert dev.events.index("arm_scope") < dev.events.index(("gen", "pulses"))
    assert dev.events.index("configure_single") < dev.events.index("arm_scope")
    assert dev.events[-1] == "gen_off" or "gen_off" in dev.events
    assert len(caps) == 1


def test_channel_mapping_and_current():
    _, (cap,) = run(plan())
    assert cap.v_hi.max() == pytest.approx(2.0)   # ch1 -> hi
    assert cap.v_lo.max() == pytest.approx(0.2)   # ch0 -> lo
    # nominal: i_hi = 2.0 / (500 * 0.5) = 8 mA, i_lo = 0.2 / (50 * 0.5) = 8 mA
    assert cap.i_hi.max() == pytest.approx(8e-3)
    assert cap.i_lo.max() == pytest.approx(8e-3)
    assert cap.triggered
    assert len(cap.t) == N and cap.t[1] - cap.t[0] == pytest.approx(1 / FakeSingleAD2.RATE)


def test_digital_capture_and_clip_bits(tmp_path):
    p = plan(digital=True)
    dev, (cap,) = run(p)
    assert dev.events.index("arm_digital") < dev.events.index(("gen", "pulses"))
    assert cap.digital is not None
    out = tmp_path / "p.csv"
    write_oneshot_csv(out, cap, p.capture.digital_pins)
    meta, cols = read_trace_csv(out)
    assert meta["label"] == "p" and meta["triggered"] is True
    assert "a_clip" in cols and cols["a_clip"][200] == 1 and cols["a_clip"][0] == 0


def test_no_digital_has_no_clip_columns(tmp_path):
    _, (cap,) = run(plan(digital=False))
    out = tmp_path / "p.csv"
    write_oneshot_csv(out, cap, {"a_clip": 7})
    _, cols = read_trace_csv(out)
    assert "a_clip" not in cols
    assert set(cols) == {"t_s", "v500_V", "v50_V", "i500_A", "i50_A"}


def test_generator_waits_for_prefill():
    dev, _ = run(plan())
    assert dev._prefilled  # the runner polled through PREFILL before starting the generator


def test_missed_trigger_is_retried():
    dev = FakeSingleAD2()
    misses = {"left": 1}
    orig = dev.gen_pulses

    def flaky(*a):
        if misses["left"]:
            misses["left"] -= 1
            dev.events.append(("gen", "missed"))  # generator ran, trigger not seen: stays ARMED
            return
        orig(*a)
    dev.gen_pulses = flaky
    p = plan()
    p.capture.wall_timeout_s = 0.05
    (cap,) = Runner(dev, p, log=lambda *a: None, sleep=lambda s: None).run()
    assert cap.retries == 1 and cap.v_hi.max() == pytest.approx(2.0)


def test_wall_timeout():
    dev = FakeSingleAD2()
    dev.gen_pulses = lambda *a, **k: None  # never starts -> never DONE
    dev.armed_scope = True
    p = plan()
    p.capture.wall_timeout_s = 0.05
    p.capture.retries = 0
    with pytest.raises(TimeoutError):
        Runner(dev, p, log=lambda *a: None, sleep=lambda s: None).run()


def test_step_overrides_and_trigger_time_base():
    p = plan()
    p.steps[-1].capture = {"position_s": 20e-6}
    dev, (cap,) = run(p)
    # trigger at sample N/2 - position*rate = 512 - 200 = 312 -> t = 0 there
    assert cap.t[312] == pytest.approx(0.0, abs=1e-12)
    assert p.capture.position_s == 0.0  # the plan default is untouched
    p.steps[-1].capture = {"bogus": 1}
    with pytest.raises(ValueError):
        run(p)


def test_custom_gen_validation():
    assert validate_gen({"mode": "custom", "samples": [0, 1, 0], "rate": 1e6, "high_v": 5})["repeat"] == 1
    with pytest.raises(ValueError):
        validate_gen({"mode": "custom", "samples": [1], "rate": 1e6, "high_v": 5})
    with pytest.raises(ValueError):
        validate_gen({"mode": "bogus"})


def test_load_example_plan():
    p = load_plan(Path(__file__).resolve().parents[1] / "plans" / "example_pulse_response.json")
    assert p.shunt == 0.5 and p.hi_channel == 1
    assert p.capture.samples == 16384 and p.capture.trigger == "generator" and p.capture.digital
    labels = [s.label for s in p.steps if s.op == "capture"]
    assert labels == ["M5_10us_2k", "M5_50us_2k", "M5_200us_2k", "M5_1ms_2k", "M5_1ms_hold_2k"]
    for s in p.steps:
        if s.op == "capture":
            validate_gen(s.gen)
