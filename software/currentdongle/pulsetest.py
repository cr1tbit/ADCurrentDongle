"""Pulse charge test (protocol M6), driven step by step from the GUI timer.

    1. on:       gate held high (DC), measure I_on - the app's own steady-state reading
    2. pre:      generator off, baseline right before the train
    3. train:    one window around exactly N pulses (+ tail for recovery/filter)
    4. post:     baseline right after the train
    5. I_base(t) interpolated between pre and post (cancels linear offset drift)
       Q_meas = Q_window - I_base(train) * T_window,  Q_expected = (I_on - I_pre) * width * N

The baseline is taken per channel and weighted by how much of a window ran on each: at low
duty cycles the train window is mostly on A2 (look-back hold-off), while the baseline windows
are on A5, and the two channels' zero points differ by tens of uA on 0.5 ohm.

At low duty cycles the baseline term is larger than the signal, so drift matters:
0.1 uA over a 3 s window is 0.3 uC.

Q_meas vs. Q_expected is a relative test of the dynamic chain (range switching,
hold-off, filtering, sampling): both sides come from the same calibration.
"""

import time

GEN_LIMITS = {"volts": (0.0, 5.0), "width_s": (1e-6, 10.0), "period_s": (2e-6, 100.0), "count": (1, 1_000_000)}


def snap_period(width_s: float, period_s: float, gen_buffer: int) -> float:
    """Nudge the period so the pulse width is a whole number of generator samples.

    The generator renders a period from `gen_buffer` samples, so an arbitrary width can be
    rounded by up to half a sample (period / 2 / gen_buffer). With the snapped period the
    width is exact; the period itself doesn't enter the expected charge.
    """
    k = round(width_s / period_s * gen_buffer)
    k = min(max(k, 1), gen_buffer - 1)
    return width_s * gen_buffer / k


def validate_gen(cmd: dict) -> dict:
    mode = cmd.get("mode")
    if mode == "off":
        return {"mode": "off"}
    if mode == "dc":
        v = float(cmd["volts"])
        _check("volts", v)
        return {"mode": "dc", "volts": v}
    if mode == "pulses":
        g = {"mode": "pulses", "high_v": float(cmd["high_v"]), "width_s": float(cmd["width_s"]),
             "period_s": float(cmd["period_s"]), "count": int(cmd["count"])}
        _check("volts", g["high_v"])
        _check("width_s", g["width_s"])
        _check("period_s", g["period_s"])
        _check("count", g["count"])
        if g["width_s"] >= g["period_s"]:
            raise ValueError("width must be shorter than the period")
        return g
    raise ValueError(f"unknown generator mode {mode!r}")


def _check(key, v):
    lo, hi = GEN_LIMITS[key]
    if not lo <= v <= hi:
        raise ValueError(f"{key} must be within {lo} .. {hi}")


class PulseTest:
    def __init__(self, ctl, high_v: float, width_s: float, period_s: float, count: int, label: str = "",
                 baseline_s: float = 1.0, on_s: float = 1.0, settle_s: float = 0.3, tail_s: float = 0.02,
                 source: str = "operator"):
        self.ctl = ctl
        self.train = validate_gen({"mode": "pulses", "high_v": high_v, "width_s": width_s,
                                   "period_s": period_s, "count": count})
        self.train["requested_period_s"] = period_s
        self.train["period_s"] = snap_period(width_s, period_s, ctl.session.setup["gen_buffer"])
        self.label = label or f"pulses {width_s * 1e6:g}us/{period_s * 1e3:g}ms x{count}"
        self.baseline_s, self.on_s, self.settle_s, self.tail_s = baseline_s, on_s, settle_s, tail_s
        self.source = source
        self.state = "start"
        self.t = time.monotonic()
        self.wid = None
        self.windows = {}
        self.result: dict | None = None
        self.error: str | None = None
        self._trains_before = 0

    @property
    def done(self) -> bool:
        return self.state in ("done", "failed")

    def progress(self) -> dict:
        return {"state": self.state, "label": self.label, "error": self.error, "result": self.result}

    def abort(self, why: str):
        s = self.ctl.session
        if s is not None and s.running:
            if self.wid is not None and self.wid in s.windows:
                self.ctl.end(self.wid)
            s.set_gen({"mode": "off"})
        self.state, self.error = "failed", why
        self.ctl.notify(f"[{self.source}] pulse test failed: {why}")

    def step(self):
        """Advance the state machine; call periodically (main thread)."""
        if self.done:
            return
        s = self.ctl.session
        if s is None or not s.running:
            self.state, self.error = "failed", "acquisition stopped"
            return
        now = time.monotonic()
        el = now - self.t

        def go(state):
            self.state, self.t = state, now

        if self.state == "start":
            s.set_gen({"mode": "dc", "volts": self.train["high_v"]})
            go("on_settle")
        elif self.state == "on_settle" and el >= self.settle_s:
            self.wid = self.ctl.begin(f"{self.label} on", self.source)
            go("on")
        elif self.state == "on" and s.window_progress(self.wid)["duration_s"] >= self.on_s:
            self.windows["on"] = self.ctl.end(self.wid)
            s.set_gen({"mode": "off"})
            go("off_settle")
        elif self.state == "off_settle" and el >= self.settle_s:
            self.wid = self.ctl.begin(f"{self.label} pre", self.source)
            go("pre")
        elif self.state == "pre" and s.window_progress(self.wid)["duration_s"] >= self.baseline_s:
            self.windows["pre"] = self.ctl.end(self.wid)
            self.wid = self.ctl.begin(f"{self.label} train", self.source)
            self._trains_before = s.gen_trains_done
            s.set_gen(self.train)
            go("train")
        elif self.state == "train":
            if s.gen_trains_done > self._trains_before:
                go("tail")
            elif el > self.train["count"] * self.train["period_s"] + 5:
                self.abort("generator did not report the end of the train")
        elif self.state == "tail" and el >= self.tail_s:
            self.windows["train"] = self.ctl.end(self.wid)
            self.wid = self.ctl.begin(f"{self.label} post", self.source)
            go("post")
        elif self.state == "post" and s.window_progress(self.wid)["duration_s"] >= self.baseline_s:
            self.windows["post"] = self.ctl.end(self.wid)
            self.wid = None
            self._finish()

    def _finish(self):
        pre, post, on, tr = (self.windows[k] for k in ("pre", "post", "on", "train"))
        g = self.train

        def center(w):
            return w["start_s"] + w["duration_s"] / 2

        cal = self.ctl.session.proc.cal

        def a2(w):  # the window's mean current as A2 alone reads it
            return float(cal.lo.current(w["mean_v_lo"], cal.shunt))

        def base(w_frac, f_lo):  # baseline at a point between pre and post, as a mix of both channels
            hi = pre["mean_current_A"] + (post["mean_current_A"] - pre["mean_current_A"]) * w_frac
            lo = a2(pre) + (a2(post) - a2(pre)) * w_frac
            return (1 - f_lo) * hi + f_lo * lo

        frac = (center(tr) - center(pre)) / (center(post) - center(pre))
        i_base = base(frac, tr["lo_channel_fraction"])
        i_pre = pre["mean_current_A"]
        i_on = on["mean_current_A"] - base(0.0, on["lo_channel_fraction"])
        q_meas = tr["charge_C"] - i_base * tr["duration_s"]
        q_exp = i_on * g["width_s"] * g["count"]
        valid = all(w["valid"] for w in (pre, post, on, tr))
        self.result = {
            "label": self.label,
            "train": {k: g[k] for k in ("high_v", "width_s", "period_s", "requested_period_s", "count")},
            "i_base_A": i_base,
            "i_base_drift_A": post["mean_current_A"] - i_pre,
            "i_base_a2_minus_a5_A": a2(pre) - i_pre,
            "i_on_A": i_on,
            "i_on_lo_channel_fraction": on["lo_channel_fraction"],
            "q_measured_C": q_meas,
            "q_expected_C": q_exp,
            "q_measured_uAh": q_meas / 3.6e-3,
            "q_expected_uAh": q_exp / 3.6e-3,
            "error_rel": (q_meas / q_exp - 1) if q_exp else None,
            "train_window_s": tr["duration_s"],
            "train_lo_channel_fraction": tr["lo_channel_fraction"],
            "train_overrange_fraction": tr["overrange_fraction"],
            "train_max_current_A": tr["max_current_A"],
            "window_ids": {k: w["id"] for k, w in self.windows.items()},
            "valid": valid,
        }
        self.state = "done"
        err = self.result["error_rel"]
        err_txt = f"{err:+.2%}" if err is not None else "n/a"
        self.ctl.notify(f"[{self.source}] pulse test {self.label}: Q {self.result['q_measured_uAh']:.4g} µAh vs "
                        f"expected {self.result['q_expected_uAh']:.4g} µAh ({err_txt})"
                        + ("" if valid else " NOT VALID"))
        if self.ctl.session:
            self.ctl.session.log_event({"type": "pulse_test", **self.result})
