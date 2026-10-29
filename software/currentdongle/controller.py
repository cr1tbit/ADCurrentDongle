"""Single point of control for the GUI and the agent. Main (Qt) thread only."""

import copy
import logging
import time
from pathlib import Path

import numpy as np

from .pulsetest import PulseTest, snap_period, validate_gen
from .processing import Calibration, fit_channel, load_calibration, save_calibration, timestamp
from .session import Session, SessionConfig, si

log = logging.getLogger("currentdongle")

LINEAR_MIN_V = 0.03


class Controller:
    def __init__(self, template: SessionConfig, cal_path: Path):
        self.template = copy.deepcopy(template)
        self.cal_path = cal_path
        self.session: Session | None = None
        self.last_summary: dict | None = None
        self.cal_points: dict[str, list[dict]] = {}
        self.history: list[dict] = []  # measurement results of finished sessions
        self.listeners = []  # callables(str) - activity feed for the GUI
        self.pulse_test: PulseTest | None = None

    # --- state ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self.session is not None and self.session.running

    @property
    def profile(self) -> tuple[float, str]:
        return self.template.cal.shunt, self.template.cal.filter

    @property
    def vdc(self) -> float:
        return self.session.vdc if self.running else self.template.acq.vdc

    def notify(self, text: str):
        log.info(text)
        for fn in self.listeners:
            fn(text)

    def status(self) -> dict:
        s = self.session
        shunt, filt = self.profile
        st = {
            "running": self.running,
            "vdc": self.vdc,
            "supply_controlled": self.template.acq.supply,
            "shunt_ohm": shunt,
            "filter": filt,
            "calibration": self.template.cal.source,
            "full_scale_A": {"a5_x500": round((self.vdc - 0.04) / (500 * shunt), 9),
                             "a2_x50": round((self.vdc - 0.04) / (50 * shunt), 9)},
        }
        if self.running:
            p = s.proc.summary()
            d = s.last_diag
            st.update({
                "session_dir": str(s.dir),
                "connected": s.connected,
                "disconnected_s": (time.time() - s.disconnected_since) if s.disconnected_since else 0,
                "session_duration_s": p["duration_s"],
                "session_mean_current_A": p["mean_current_A"],
                "session_charge_mAh": p["charge_mAh"],
                "session_valid": p["valid"],
                "gap_samples": p["gap_samples"],
                "corrupt_samples": p["corrupt_samples"],
                "overrange_fraction": p["overrange_fraction"],
                "open_measurements": [w.id for w in s.windows.values()],
                "reconnects": d.get("reconnects", 0),
                "buffer_fill_peak": d.get("buffer_fill_peak"),
                "usb_v": d.get("usb_v"),
            })
            st["generator"] = s.gen
            if self.pulse_test is not None:
                st["pulse_test"] = self.pulse_test.progress()
            recent = s.trace(1.0, 1)
            st["last_1s_mean_A"] = float(recent["mean_A"][0]) if len(recent["mean_A"]) else None
        elif self.last_summary:
            st["last_session"] = self.last_summary
        return st

    # --- lifecycle ----------------------------------------------------------------

    def start(self, source: str = "operator"):
        if self.running:
            raise RuntimeError("already running")
        cfg = copy.deepcopy(self.template)
        cfg.cal = load_calibration(self.cal_path, cfg.cal.shunt, cfg.cal.filter)
        self.template.cal = cfg.cal
        self.session = Session(cfg)
        self.session.start()
        self.notify(f"[{source}] started {cfg.cal.key} @ {cfg.acq.vdc} V -> {self.session.dir.name}")

    def stop(self, source: str = "operator") -> dict:
        if self.session is None:
            raise RuntimeError("not running")
        s, self.session = self.session, None
        self.last_summary = s.stop()
        self.last_summary["session_dir"] = str(s.dir)
        self.history.extend(s.results)
        self.notify(f"[{source}] stopped, session {'valid' if self.last_summary.get('valid') else 'NOT VALID'}")
        return self.last_summary

    def pump(self, max_time: float) -> int:
        s = self.session
        if s is None:
            return 0
        n = s.pump(max_time)
        if self.pulse_test is not None and not self.pulse_test.done:
            self.pulse_test.step()
        if not s.running and s.error:
            self.notify(f"acquisition failed: {s.error.strip().splitlines()[-1]}")
            self.stop("system")
        return n

    def set_profile(self, shunt: float, filt: str, source: str = "operator"):
        if self.running:
            raise RuntimeError("stop acquisition before changing shunt/filter")
        if filt not in ("2k", "20k") or shunt <= 0:
            raise ValueError("invalid shunt/filter")
        self.template.cal = load_calibration(self.cal_path, shunt, filt)
        self.notify(f"[{source}] profile {self.template.cal.key} ({self.template.cal.source} calibration)")

    def set_vdc(self, volts: float, source: str = "operator"):
        if not 0.5 <= volts <= 5.0:
            raise ValueError("AD2 V+ range is 0.5 .. 5 V")
        if self.running:
            self.session.set_vdc(volts)
        self.template.acq.vdc = volts
        self.notify(f"[{source}] VDC {volts} V")

    def set_generator(self, cmd: dict, source: str = "operator") -> dict:
        if not self.running:
            raise RuntimeError("acquisition is not running")
        if self.pulse_test is not None and not self.pulse_test.done:
            raise RuntimeError("a pulse test is running")
        g = validate_gen(cmd)
        if g["mode"] == "pulses":
            g["requested_period_s"] = g["period_s"]
            g["period_s"] = snap_period(g["width_s"], g["period_s"], self.session.setup["gen_buffer"])
        self.session.set_gen(g)
        self.notify(f"[{source}] generator {gen_text(g)}")
        return g

    def start_pulse_test(self, source: str = "operator", **kw) -> PulseTest:
        if not self.running:
            raise RuntimeError("acquisition is not running")
        if self.pulse_test is not None and not self.pulse_test.done:
            raise RuntimeError("a pulse test is already running")
        self.pulse_test = PulseTest(self, source=source, **kw)
        self.notify(f"[{source}] pulse test started: {self.pulse_test.label}")
        return self.pulse_test

    def annotate(self, text: str, source: str = "operator"):
        if self.session:
            self.session.log_event({"type": "note", "source": source, "text": text})
        self.notify(f"[{source}] note: {text}")

    # --- measurements -------------------------------------------------------------

    def begin(self, label: str = "", source: str = "operator") -> int:
        wid = self.session.begin_window(label, source) if self.running else None
        if wid is None:
            raise RuntimeError("acquisition is not running")
        self.notify(f"[{source}] measurement #{wid} {label} started")
        return wid

    def end(self, wid: int) -> dict:
        res = self.session.end_window(wid)
        self.notify(f"[{res['source']}] measurement #{wid} {res['label']}: {si(res['mean_current_A'], 'A')}, "
                    f"{res['duration_s']:.2f} s, {'valid' if res['valid'] else 'NOT VALID'}")
        return res

    def results(self) -> list[dict]:
        return self.history + (list(self.session.results) if self.session else [])

    def result(self, wid: int) -> dict | None:
        return next((r for r in reversed(self.results()) if r["id"] == wid), None)

    # --- calibration ----------------------------------------------------------------

    def cal_add(self, i_ref: float, res: dict) -> dict:
        pt = {"i_ref": i_ref, "v_hi": res["mean_v_hi"], "v_lo": res["mean_v_lo"],
              "std_hi": res["std_v_hi"], "std_lo": res["std_v_lo"],
              "n": round(res["duration_s"] * self.session.proc.rate),
              "valid": res["valid"], "vdc": res["vdc"]}
        v_sat = self.session.proc.v_sat
        pt["window_hi"] = window_label(pt["v_hi"], v_sat)
        pt["window_lo"] = window_label(pt["v_lo"], v_sat)
        self.cal_points.setdefault(self.template.cal.key, []).append(pt)
        self.session.log_event({"type": "cal_point", **pt})
        return pt

    def cal_fit(self, save: bool = True, allow_partial: bool = False) -> dict:
        shunt, filt = self.profile
        key = self.template.cal.key
        points = [p for p in self.cal_points.get(key, []) if p["valid"]]
        v_sat = self.session.proc.v_sat if self.running else self.template.acq.vdc - 0.15
        nominal = Calibration.nominal(shunt, filt)
        cal = Calibration(shunt, filt, nominal.hi, nominal.lo, source="cal", created=timestamp(), points=points)
        report = {"profile": key, "points": len(points), "channels": {}}
        complete = True
        for ch in ("hi", "lo"):
            use = [p for p in points if LINEAR_MIN_V <= p[f"v_{ch}"] <= v_sat]
            if len(use) < 2:
                report["channels"][ch] = {"error": f"need >= 2 valid points in the linear window, have {len(use)}"}
                complete = False
                continue
            fit, resid = fit_channel([p["i_ref"] for p in use], [p[f"v_{ch}"] for p in use], shunt)
            setattr(cal, ch, fit)
            zeros = [p[f"v_{ch}"] for p in points if p["i_ref"] == 0]
            report["channels"][ch] = {
                "gain": fit.gain,
                "gain_vs_nominal": fit.gain / getattr(nominal, ch).gain - 1,
                "offset_V": fit.offset,
                "residuals": [{"i_ref": p["i_ref"], "residual_A": float(r),
                               "residual_rel": float(r / p["i_ref"]) if p["i_ref"] else None}
                              for p, r in zip(use, resid)],
                "zero_current_output_V": zeros,
            }
        report["complete"] = complete
        if save and (complete or allow_partial):
            save_calibration(self.cal_path, cal)
            self.template.cal = cal
            report["saved_to"] = str(self.cal_path)
            report["note"] = "applies from the next start"
            self.notify(f"calibration {key} saved")
        return report


def gen_text(g: dict) -> str:
    if g["mode"] == "dc":
        return f"DC {g['volts']:g} V"
    if g["mode"] == "pulses":
        return (f"{g['count']} pulses {g['high_v']:g} V, {g['width_s'] * 1e6:g} µs / "
                f"{g['period_s'] * 1e3:g} ms")
    return "off"


def window_label(v: float, v_sat: float) -> str:
    return "ok" if LINEAR_MIN_V <= v <= v_sat else ("low" if v < LINEAR_MIN_V else "SAT")


def jsonable(x):
    """numpy -> plain Python for JSON/MCP replies."""
    if isinstance(x, dict):
        return {k: jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    if isinstance(x, np.ndarray):
        return [jsonable(v) for v in x.tolist()]
    if isinstance(x, (np.floating, float)):
        return None if not np.isfinite(x) else float(x)
    if isinstance(x, np.integer):
        return int(x)
    return x
