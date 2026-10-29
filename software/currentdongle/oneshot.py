"""Single-shot triggered capture driven by a pre-planned sequence (protocol M5, and the
groundwork for defined rail/supply sequences on a future board revision).

Unlike the streaming acquisition process, a single-shot capture is one armed buffer: the PC
arms the scope (and optionally the logic analyzer) with their trigger source set to W1, starts
the generator, and reads the buffer back once it is full. The AD2 synchronises the capture to
the generator start on the device - the PC is not in the timing path, so the window is
deterministic relative to the applied sequence and can run far faster than USB streaming
(up to the scope's buffer, 16k samples in device config 1).

A plan is a small JSON file: global capture settings plus an ordered list of steps
(`supply` / `settle` / `gen` / `capture`). See `plans/m5_pulse_response.json`.
"""

import json
import time
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

import numpy as np

from . import dwf
from .processing import ChannelCal

TRIGGER_SOURCES = {
    "generator": dwf.TRIGSRC_ANALOG_OUT1,        # fire when W1 starts (window synced to the sequence)
    "edge": dwf.TRIGSRC_DETECTOR_ANALOG_IN,      # fire on the measured pulse crossing a level
    "none": dwf.TRIGSRC_NONE,                    # free-run, capture immediately
}


@dataclass
class CaptureCfg:
    rate: float = 10e6
    samples: int = 16384
    volts_range: float = 5.0
    filt: str = "decimate"            # raw samples; no hardware averaging for a fast single shot
    trigger: str = "generator"
    position_s: float = 0.0           # trigger at sample N/2 - position_s*rate (positive = earlier)
    timeout_s: float = 0.0            # scope auto-trigger (0 = off: a missed trigger times out
                                      # instead of returning a capture without the event)
    level_v: float = 0.5              # edge-trigger level on the hi channel (post-gain volts)
    rising: bool = True
    digital: bool = False             # also capture DIO (clip flags) on the same trigger
    digital_pins: dict = field(default_factory=lambda: {"a_clip": 7, "b_clip": 15})
    wall_timeout_s: float = 10.0      # safety: give up waiting for DONE after this
    settle_s: float = 0.5             # after (re)configuring the scope: offset DAC settling
    retries: int = 3                  # re-arm attempts when the trigger is missed


@dataclass
class Step:
    op: str
    label: str = ""
    seconds: float = 0.0
    volts: float | None = None
    enable: bool = True
    gen: dict = field(default_factory=lambda: {"mode": "off"})
    note: str = ""
    capture: dict = field(default_factory=dict)  # per-capture overrides of CaptureCfg fields


@dataclass
class Plan:
    name: str
    vdc: float = 3.3
    shunt: float = 0.5
    sw3: str = "2k"
    hi_channel: int = 1               # 0-indexed scope channel carrying A5 (x500); CH2 -> 1
    capture: CaptureCfg = field(default_factory=CaptureCfg)
    steps: list[Step] = field(default_factory=list)


@dataclass
class Capture:
    label: str
    rate: float
    t: np.ndarray
    v_hi: np.ndarray
    v_lo: np.ndarray
    i_hi: np.ndarray
    i_lo: np.ndarray
    gen: dict
    triggered: bool
    note: str = ""
    digital: np.ndarray | None = None
    digital_rate: float | None = None
    retries: int = 0


def load_plan(path: Path) -> Plan:
    d = json.loads(Path(path).read_text())
    cap = CaptureCfg(**d.pop("capture", {}))
    steps = [Step(**s) for s in d.pop("steps", [])]
    return Plan(capture=cap, steps=steps, **d)


def validate_gen(g: dict) -> dict:
    mode = g.get("mode")
    if mode in ("off", "dc", "pulses"):
        from .pulsetest import validate_gen as _v
        return _v(g)
    if mode == "custom":
        n = len(g["samples"])
        if n < 2:
            raise ValueError("custom needs >= 2 samples")
        return {"mode": "custom", "samples": list(map(float, g["samples"])),
                "rate": float(g["rate"]), "high_v": float(g["high_v"]), "repeat": int(g.get("repeat", 1))}
    raise ValueError(f"unknown generator mode {mode!r}")


def gen_duration(g: dict) -> float:
    g = validate_gen(g)
    if g["mode"] == "pulses":
        return g["count"] * g["period_s"]
    if g["mode"] == "custom":
        return g["repeat"] * len(g["samples"]) / g["rate"]
    return 0.0


def apply_gen(dev, g: dict, gen_buffer: int | None = None):
    """With gen_buffer, a pulse period is snapped so the width is a whole number of generator samples
    (a 10 us pulse in a 20 ms period would otherwise round to 0 samples)."""
    g = validate_gen(g)
    if g["mode"] == "pulses" and gen_buffer:
        from .pulsetest import snap_period
        g = dict(g, period_s=snap_period(g["width_s"], g["period_s"], gen_buffer))
    if g["mode"] == "dc":
        dev.gen_dc(g["volts"])
    elif g["mode"] == "pulses":
        dev.gen_pulses(g["high_v"], g["width_s"], g["period_s"], int(g["count"]))
    elif g["mode"] == "custom":
        dev.gen_custom(np.asarray(g["samples"], float), g["rate"], g["high_v"], g["repeat"])
    else:
        dev.gen_off()


class Runner:
    """Executes a plan against an open device (real or fake). Call `run`, or `step` for one step."""

    def __init__(self, dev, plan: Plan, cal=None, log=print, sleep=time.sleep):
        self.dev = dev
        self.plan = plan
        self.cal = cal
        self.log = log
        self.sleep = sleep
        hi = plan.hi_channel
        self.hi_cal, self.lo_cal = self._channel_cals(cal, hi)
        self._scope_key = None   # scope settings currently applied
        self._setup = None

    @staticmethod
    def _channel_cals(cal, hi):
        if cal is None:
            return ChannelCal(500.0, 0.0), ChannelCal(50.0, 0.0)
        return cal.hi, cal.lo

    def run(self) -> list[Capture]:
        caps = []
        for step in self.plan.steps:
            cap = self.step(step)
            if cap is not None:
                caps.append(cap)
        return caps

    def step(self, step: Step) -> Capture | None:
        if step.op == "supply":
            v = self.plan.vdc if step.volts is None else step.volts
            self.dev.set_supply(v, step.enable)
            self.log(f"supply {'on ' + format(v, 'g') + ' V' if step.enable else 'off'}")
        elif step.op == "settle":
            self.log(f"settle {step.seconds:g} s")
            self.sleep(step.seconds)
        elif step.op == "gen":
            apply_gen(self.dev, step.gen, self._gen_buffer())
            self.log(f"gen {step.gen.get('mode')}")
        elif step.op == "capture":
            return self._capture(step)
        else:
            raise ValueError(f"unknown step op {step.op!r}")
        return None

    def _capture(self, step: Step) -> Capture:
        plan, dev = self.plan, self.dev
        unknown = set(step.capture) - {f.name for f in fields(CaptureCfg)}
        if unknown:
            raise ValueError(f"unknown capture override(s) {sorted(unknown)}")
        cfg = replace(plan.capture, **step.capture)
        if cfg.trigger not in TRIGGER_SOURCES:
            raise ValueError(f"unknown trigger {cfg.trigger!r}")
        src = TRIGGER_SOURCES[cfg.trigger]
        offsets = (plan.vdc / 2, plan.vdc / 2)
        key = (cfg.rate, cfg.samples, offsets, cfg.volts_range, cfg.filt)
        if key != self._scope_key:
            # a reset moves the scope offset DAC; it takes a while to reach VDC/2 again
            self._setup = dev.configure_single(cfg.rate, cfg.samples, offsets, cfg.volts_range, cfg.filt)
            self._scope_key = key
            self.sleep(cfg.settle_s)
        setup = self._setup
        dev.set_scope_trigger(src, cfg.position_s, cfg.timeout_s, cfg.level_v, plan.hi_channel, cfg.rising)
        if cfg.digital:
            dig_rate, dig_n = dev.configure_digital_single(setup.buffer_size / setup.rate, src,
                                                           cfg.position_s)

        # Sometimes both instruments stay armed and miss W1's start. On the bench (2026-10-26) this
        # happened in ~half the captures with the scope at 5 MS/s + logic analyzer, never in 24 at
        # 10 MS/s or without the logic analyzer. Nothing was captured then: re-arm and restart W1.
        deadline = gen_duration(step.gen) + setup.buffer_size / setup.rate + 0.5
        retries = 0
        while True:
            dev.arm_scope()            # arm the slaves first ...
            if cfg.digital:
                dev.arm_digital()
            self._wait_armed(cfg)      # pre-trigger samples collected, else the trigger is missed
            apply_gen(dev, step.gen, self._gen_buffer())  # ... then start W1, which fires the trigger
            try:
                triggered = self._wait_done(cfg, min(cfg.wall_timeout_s, deadline))
                break
            except TimeoutError:
                missed = dev.scope_state() == dwf.STATE_ARMED or (
                    cfg.digital and dev.digital_state() == dwf.STATE_ARMED)
                if not missed or retries >= cfg.retries:
                    raise TimeoutError(
                        f"capture {step.label!r} did not complete: scope state {dev.scope_state()}, "
                        f"logic {dev.digital_state() if cfg.digital else '-'}, gen {dev.gen_state()}, "
                        f"retries {retries}")
                retries += 1
                self.log(f"capture {step.label!r}: trigger missed, retry {retries}")
                dev.gen_off()

        n = setup.buffer_size
        t = (np.arange(n) - (n / 2 - cfg.position_s * setup.rate)) / setup.rate  # 0 = trigger
        v0, v1 = dev.read_single(0, n), dev.read_single(1, n)
        dig, dig_rate_out = None, None
        if cfg.digital:
            # both triggers sit mid-buffer; map each scope sample to the logic sample at the same time
            trig_d = dig_n / 2 - cfg.position_s * dig_rate
            k = np.round(t * dig_rate + trig_d).astype(int)
            dig = dev.read_digital(dig_n)[np.clip(k, 0, dig_n - 1)]
            dig_rate_out = dig_rate
        dev.gen_off()

        v_hi, v_lo = (v0, v1) if plan.hi_channel == 0 else (v1, v0)
        self.log(f"capture {step.label!r}: {n} samples @ {setup.rate / 1e6:g} MS/s, "
                 f"triggered={triggered}, V500 max {v_hi.max():.3f} V")
        return Capture(
            label=step.label, rate=setup.rate, t=t, v_hi=v_hi, v_lo=v_lo,
            i_hi=self.hi_cal.current(v_hi, plan.shunt), i_lo=self.lo_cal.current(v_lo, plan.shunt),
            gen=validate_gen(step.gen), triggered=triggered, note=step.note,
            digital=dig, digital_rate=dig_rate_out, retries=retries)

    def _wait_armed(self, cfg: CaptureCfg):
        t0 = time.monotonic()
        while True:
            if self.dev.scope_state() == dwf.STATE_ARMED and (
                    not cfg.digital or self.dev.digital_state() == dwf.STATE_ARMED):
                return
            if time.monotonic() - t0 > cfg.wall_timeout_s:
                raise TimeoutError(f"scope/logic not armed within {cfg.wall_timeout_s:g} s")
            self.sleep(0.001)

    def _gen_buffer(self):
        f = getattr(self.dev, "gen_buffer_size", None)
        return f() if f else None

    def _wait_done(self, cfg: CaptureCfg, timeout_s: float) -> bool:
        t0 = time.monotonic()
        while True:
            scope_ok = self.dev.scope_state() == dwf.STATE_DONE
            dig_ok = (not cfg.digital) or self.dev.digital_state() == dwf.STATE_DONE
            if scope_ok and dig_ok:
                return True
            if time.monotonic() - t0 > timeout_s:
                raise TimeoutError(f"capture did not complete within {timeout_s:g} s")
            self.sleep(0.002)
