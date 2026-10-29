"""Measurement session: acquisition process + processing + on-disk record.

One session = one continuous acquisition (it survives USB reconnects). Measurements
are windows on that stream, opened and closed by the operator, the agent or `cal`.

Session folder contents:
    meta.json          configuration, calibration, device setup, versions
    app.log            human-readable log (also on the console)
    events.jsonl       lost/corrupted samples, gaps, dropped blocks, reconnects, supply trips, errors
    measurements.jsonl one line per measurement window
    diag.csv           acquisition health, once per second
    stats.csv          current statistics per interval
    raw.f32            optional: interleaved float32 [V_500, V_50] per sample (with --raw)
    summary.json       final totals, written on stop
"""

import copy
import csv
import itertools
import json
import logging
import multiprocessing as mp
import platform
import queue
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from . import __version__
from .acquisition import AcqConfig, run
from .processing import HOLDOFF_S, Calibration, Processor, Result, minmax_decimate, timestamp

log = logging.getLogger("currentdongle")

QUEUE_BLOCKS = 500       # ~10 s of 20 ms blocks
DISPLAY_KEEP_S = 120     # display history kept in memory


@dataclass
class SessionConfig:
    acq: AcqConfig
    cal: Calibration
    out_dir: Path
    label: str = ""
    raw: bool = False
    stats_interval_s: float = 1.0
    display_bin_s: float = 1e-3
    v_sat_margin: float = 0.15  # INA186 gain is specified up to VS - 0.1 V


class Ring:
    """Append-only history of display bins (t, min, mean, max), fixed memory."""

    def __init__(self, capacity: int):
        self.cap = capacity
        self.buf = np.empty((4, 2 * capacity))
        self.n = 0

    def append(self, cols):
        m = len(cols[0])
        if m == 0:
            return
        if m >= self.cap:
            cols = [c[-self.cap:] for c in cols]
            m = self.cap
        if self.n + m > self.buf.shape[1]:
            keep = self.cap - m
            self.buf[:, :keep] = self.buf[:, self.n - keep:self.n]
            self.n = keep
        for i, c in enumerate(cols):
            self.buf[i, self.n:self.n + m] = c
        self.n += m

    def last(self, seconds: float):
        if self.n == 0:
            return np.empty((4, 0))
        t = self.buf[0, :self.n]
        i = np.searchsorted(t, t[-1] - seconds)
        return self.buf[:, i:self.n]


class Binner:
    """Decimates (V_500, V_50, I) into fixed bins of (mean, min, max) for CSV export."""

    def __init__(self, n: int, start_index: int):
        self.n = n
        self.t0 = start_index
        self.carry = None
        self.carry_start = 0
        self.rows = []

    def add(self, start: int, hi: np.ndarray, lo: np.ndarray, cur: np.ndarray):
        x = np.vstack((hi, lo, cur)).astype(np.float64)
        if self.carry is not None and self.carry.shape[1] and start == self.carry_start + self.carry.shape[1]:
            x = np.hstack((self.carry, x))
            start = self.carry_start
        m = x.shape[1] // self.n
        if m:
            v = x[:, :m * self.n].reshape(3, m, self.n)
            t = start - self.t0 + (np.arange(m) + 0.5) * self.n
            cols = [t]
            for k in range(3):
                cols += [v[k].mean(axis=1), v[k].min(axis=1), v[k].max(axis=1)]
            self.rows.append(np.column_stack(cols))
        self.carry = x[:, m * self.n:]
        self.carry_start = start + m * self.n

    def array(self, rate: float) -> np.ndarray:
        a = np.vstack(self.rows) if self.rows else np.empty((0, 10))
        a[:, 0] /= rate
        return a


class Window:
    """Running statistics over a measurement window."""

    _ids = itertools.count(1)

    def __init__(self, label: str, source: str, start_index: int, vdc: float, profile: str,
                 trace_bin: int | None = None):
        self.id = next(self._ids)
        self.trace = Binner(trace_bin, start_index) if trace_bin else None
        self.label, self.source, self.vdc, self.profile = label, source, vdc, profile
        self.start_index = start_index
        self.end_index = start_index
        self.started = timestamp()
        self.n = self.n_lo = self.n_over = self.gap = self.corrupt = 0
        self.sum_i = 0.0
        self.min_i, self.max_i = np.inf, -np.inf
        self.sum_v = np.zeros(2)
        self.sumsq_v = np.zeros(2)
        self.events: list[str] = []

    def add(self, r: Result, hi: np.ndarray, lo: np.ndarray, corrupt: int):
        n = len(r.current)
        self.gap += r.gap
        self.corrupt += corrupt
        self.end_index = r.start + n
        if n == 0:
            return
        if self.trace is not None:
            self.trace.add(r.start, hi, lo, r.current)
        self.n += n
        self.sum_i += float(r.current.sum())
        self.min_i = min(self.min_i, float(r.current.min()))
        self.max_i = max(self.max_i, float(r.current.max()))
        self.n_lo += int(r.use_lo.sum())
        self.n_over += int(r.over.sum())
        for k, v in enumerate((hi, lo)):
            v = v.astype(np.float64)
            self.sum_v[k] += v.sum()
            self.sumsq_v[k] += (v * v).sum()

    def result(self, rate: float) -> dict:
        n = max(self.n, 1)
        mean_v = self.sum_v / n
        std_v = np.sqrt(np.maximum(self.sumsq_v / n - mean_v ** 2, 0))
        charge = self.sum_i / rate
        return {
            "id": self.id,
            "label": self.label,
            "source": self.source,
            "profile": self.profile,
            "vdc": self.vdc,
            "started": self.started,
            "start_s": self.start_index / rate,
            "duration_s": self.n / rate,
            "mean_current_A": self.sum_i / n if self.n else None,
            "min_current_A": self.min_i if self.n else None,
            "max_current_A": self.max_i if self.n else None,
            "charge_C": charge,
            "charge_mAh": charge / 3.6,
            "lo_channel_fraction": self.n_lo / n,
            "overrange_fraction": self.n_over / n,
            "gap_samples": self.gap,
            "corrupt_samples": self.corrupt,
            "mean_v_hi": float(mean_v[0]), "std_v_hi": float(std_v[0]),
            "mean_v_lo": float(mean_v[1]), "std_v_lo": float(std_v[1]),
            "events": self.events,
            "valid": self.n > 0 and self.gap == 0 and self.corrupt == 0 and self.n_over == 0,
        }


class Session:
    def __init__(self, cfg: SessionConfig):
        self.cfg = copy.deepcopy(cfg)
        cfg = self.cfg
        name = time.strftime("%Y%m%d-%H%M%S") + (f"-{cfg.label}" if cfg.label else "")
        self.dir = cfg.out_dir / name
        for k in itertools.count(2):
            try:
                self.dir.mkdir(parents=True)
                break
            except FileExistsError:
                self.dir = cfg.out_dir / f"{name}-{k}"
        self._log_handler = logging.FileHandler(self.dir / "app.log")
        self._log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(self._log_handler)

        self.setup: dict | None = None
        self.proc: Processor | None = None
        self.last_diag: dict = {}
        self.running = False
        self.connected = False
        self.disconnected_since: float | None = None
        self.error: str | None = None
        self.windows: dict[int, Window] = {}
        self.traces: dict[int, np.ndarray] = {}  # binned traces of finished windows (trace_bin_s)
        self.results: list[dict] = []
        self.display = None
        self.started_wall = None
        self.gen: dict = {"mode": "off"}
        self.gen_trains_done = 0  # counts gen_done events

        self._ctx = mp.get_context("spawn")
        self._queue = self._ctx.Queue(maxsize=QUEUE_BLOCKS)
        self._ctrl = self._ctx.Queue()
        self._stop = self._ctx.Event()
        self._p = None
        self._raw = None
        self._events = open(self.dir / "events.jsonl", "w")
        self._measurements = open(self.dir / "measurements.jsonl", "w")
        self._diag_csv = None
        self._stats_csv = None
        self._stats_acc = None
        self._disp_carry = np.empty(0)
        self._disp_carry_start = 0
        self._closed = False

    @property
    def vdc(self) -> float:
        return self.cfg.acq.vdc

    # --- lifecycle ------------------------------------------------------------

    def start(self, timeout: float = 15.0):
        log.info("session %s", self.dir)
        self._p = self._ctx.Process(target=run, args=(self.cfg.acq, self._queue, self._stop, self._ctrl),
                                    daemon=True)
        self._p.start()
        t0 = time.monotonic()
        while self.setup is None:
            try:
                msg = self._queue.get(timeout=1.0)
            except queue.Empty:
                if time.monotonic() - t0 > timeout or not self._p.is_alive():
                    self.stop()
                    raise TimeoutError("acquisition process did not report setup")
                continue
            self._handle(msg)
            if self.error:
                err = self.error
                self.stop()
                raise RuntimeError(err)
        self.running = True
        self.connected = True
        self.started_wall = time.time()

    def stop(self) -> dict:
        if self._closed:
            return self.summary()
        self._stop.set()
        t0 = time.monotonic()
        closed = False
        while not closed and time.monotonic() - t0 < 10:
            try:
                msg = self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._p is not None and not self._p.is_alive():
                    break
                continue
            closed = msg[0] == "closed"
            self._handle(msg)
        if self._p is not None:
            self._p.join(timeout=2)
            if self._p.is_alive():
                log.warning("acquisition process did not exit, terminating")
                self._p.terminate()
        self.running = False
        self.connected = False
        for wid in list(self.windows):
            self.end_window(wid, note="session stopped")
        summary = self.summary()
        (self.dir / "summary.json").write_text(json.dumps(summary, indent=2))
        log.info("summary: %s", _fmt_summary(summary))
        for f in (self._raw, self._events, self._measurements, self._diag_csv and self._diag_csv[0],
                  self._stats_csv and self._stats_csv[0]):
            if f:
                f.close()
        log.removeHandler(self._log_handler)
        self._log_handler.close()
        self._closed = True
        return summary

    def summary(self) -> dict:
        s = self.proc.summary() if self.proc else {}
        s["acquisition"] = {k: self.last_diag.get(k) for k in
                            ("total_lost", "total_corrupt", "total_dropped_blocks", "reconnects")}
        s["measurements"] = len(self.results)
        s["error"] = self.error
        s["finished"] = timestamp()
        return s

    def reset_integral(self):
        if self.proc:
            self.proc.reset()
            self.log_event({"type": "reset"})

    # --- control --------------------------------------------------------------

    def set_vdc(self, volts: float):
        """Change V+ live. Refused if the scope range set at start can't cover the new output swing."""
        tops = [o + r / 2 for o, r in zip(self.setup["offsets"], self.setup["ranges"])]
        if volts > min(tops) - 0.05:
            raise ValueError(f"{volts} V exceeds the scope window set at start "
                             f"({min(tops):.2f} V) - stop and start again at the new voltage")
        self._ctrl.put(("set_vdc", float(volts)))
        self.cfg.acq.vdc = float(volts)
        self.proc.v_sat = self._v_sat(volts)
        for w in self.windows.values():
            w.events.append(f"vdc changed to {volts} V")
        self.log_event({"type": "set_vdc", "vdc": volts, "v_sat": self.proc.v_sat})

    def set_gen(self, cmd: dict):
        """Waveform generator W1: {"mode": "off" | "dc" | "pulses", ...} (validated by the controller)."""
        if not self.running:
            raise RuntimeError("acquisition is not running")
        self._ctrl.put(("gen", dict(cmd)))
        self.gen = dict(cmd, pending=True)
        self.log_event({"type": "gen_request", **cmd})

    def begin_window(self, label: str = "", source: str = "operator", trace_bin_s: float | None = None) -> int:
        """Open a measurement window. With trace_bin_s, also keep a binned trace for CSV export."""
        if not self.running:
            raise RuntimeError("acquisition is not running")
        start = self.proc.out_index if self.proc.out_index is not None else 0
        trace_bin = max(1, int(round(trace_bin_s * self.proc.rate))) if trace_bin_s else None
        w = Window(label, source, start, self.vdc, self.cfg.cal.key, trace_bin)
        if not self.connected:
            w.events.append("started while disconnected")
        self.windows[w.id] = w
        self.log_event({"type": "window_begin", "id": w.id, "label": label, "source": source})
        return w.id

    def end_window(self, wid: int, note: str | None = None) -> dict:
        w = self.windows.pop(wid)
        if note:
            w.events.append(note)
        res = w.result(self.proc.rate if self.proc else 1.0)
        res["ended"] = timestamp()
        self.results.append(res)
        if w.trace is not None:
            self.traces[wid] = w.trace.array(self.proc.rate)
        self._measurements.write(json.dumps(res) + "\n")
        self._measurements.flush()
        log.info("measurement #%d %s: %s mean, %s, %.2f s, %s", res["id"], res["label"] or "-",
                 si(res["mean_current_A"], "A"), si(res["charge_mAh"] * 1e-3, "Ah"), res["duration_s"],
                 "valid" if res["valid"] else "NOT VALID")
        return res

    def window_progress(self, wid: int) -> dict:
        w = self.windows[wid]
        return {"duration_s": w.n / self.proc.rate, "connected": self.connected}

    # --- message pump -----------------------------------------------------------

    def pump(self, max_time: float = 0.05) -> int:
        """Handle queued messages for up to max_time. Returns the number handled."""
        t0 = time.monotonic()
        n = 0
        while time.monotonic() - t0 < max_time:
            try:
                msg = self._queue.get_nowait()
            except queue.Empty:
                break
            self._handle(msg)
            n += 1
        return n

    def backlog(self) -> int:
        try:
            return self._queue.qsize()
        except NotImplementedError:
            return 0

    def _handle(self, msg):
        kind, payload = msg
        if kind == "block":
            self._on_block(payload)
        elif kind == "diag":
            self._on_diag(payload)
        elif kind == "event":
            self._on_event(payload)
        elif kind == "setup":
            self._on_setup(payload)
        elif kind == "error":
            self.error = payload
            log.error("acquisition error:\n%s", payload)
            self.log_event({"type": "error", "traceback": payload}, logging.ERROR)
        elif kind == "closed":
            self.running = False

    def _on_event(self, ev: dict):
        t = ev.get("type")
        if t == "disconnected":
            self.connected = False
            self.disconnected_since = time.time()
            log.error("AD2 disconnected (%s) - retrying", ev.get("reason"))
        elif t == "reconnected":
            self.connected = True
            self.disconnected_since = None
            log.warning("AD2 reconnected after %.1f s (reconnect #%d)", ev["down_s"], ev["reconnects"])
        elif t == "gen_set":
            self.gen = {k: v for k, v in ev.items() if k not in ("type", "index", "wall_time")}
        elif t in ("gen_done", "gen_aborted"):
            self.gen = {"mode": "off"}
            self.gen_trains_done += t == "gen_done"
        if t in ("disconnected", "reconnected", "supply_tripped", "lost", "corrupt", "gen_aborted"):
            for w in self.windows.values():
                w.events.append(t)
        level = logging.INFO if t in ("vdc_set", "gen_set", "gen_done", "gen_restored") else logging.WARNING
        self.log_event(ev, level)

    def _v_sat(self, vdc: float) -> float:
        tops = [o + r / 2 for o, r in zip(self.setup["offsets"], self.setup["ranges"])]
        return min(vdc - self.cfg.v_sat_margin, min(tops) - 0.05)

    def _on_setup(self, setup: dict):
        self.setup = setup
        c = self.cfg
        rate = setup["rate"]
        bottoms = [o - r / 2 for o, r in zip(setup["offsets"], setup["ranges"])]
        v_sat = self._v_sat(c.acq.vdc)
        if max(bottoms) > -0.02:
            log.warning("scope range does not reach below 0 V: %s", setup)
        holdoff = HOLDOFF_S.get(c.cal.filter, max(HOLDOFF_S.values()))
        self.proc = Processor(c.cal, rate, v_sat, holdoff)
        self._stats_n = max(1, int(round(c.stats_interval_s * rate)))
        self._disp_n = max(1, int(round(c.display_bin_s * rate)))
        self.display = Ring(int(DISPLAY_KEEP_S / c.display_bin_s))

        if c.cal.source == "nominal":
            log.warning("no calibration for %s - using nominal gains, zero offsets", c.cal.key)
        log.info("rate %.0f S/s, buffer %d, ranges %s, offsets %s, v_sat %.3f V, hold-off %.2f ms",
                 rate, setup["buffer_size"], _r(setup["ranges"]), _r(setup["offsets"]), v_sat, holdoff * 1e3)
        log.info("USB %.3f V %.3f A, device %.1f C", setup["monitor"]["usb_v"], setup["monitor"]["usb_a"],
                 setup["monitor"]["temp_c"])

        meta = {
            "app_version": __version__,
            "started": timestamp(),
            "host": platform.node(),
            "python": platform.python_version(),
            "label": c.label,
            "acquisition": asdict(c.acq),
            "calibration": asdict(c.cal),
            "device": setup,
            "v_sat": v_sat,
            "holdoff_s": holdoff,
            "raw_format": "float32 interleaved [V_500, V_50]; gaps listed in events.jsonl" if c.raw else None,
        }
        (self.dir / "meta.json").write_text(json.dumps(meta, indent=2))
        if c.raw:
            self._raw = open(self.dir / "raw.f32", "wb")

    def _on_block(self, b):
        if self.proc is None:
            return
        corrupt = b.corrupt
        for r in self.proc.process(b):
            if r.gap:
                self.log_event({"type": "gap", "index": r.start - r.gap, "samples": r.gap,
                                "seconds": r.gap / self.proc.rate}, logging.WARNING)
            for w in self.windows.values():
                w.add(r, r.hi, r.lo, corrupt)
            corrupt = 0
            self._accumulate_stats(r)
            self._display(r)
        if self._raw:
            np.column_stack((b.hi, b.lo)).astype(np.float32).tofile(self._raw)

    def _accumulate_stats(self, r: Result):
        if self._stats_csv is None:
            f = open(self.dir / "stats.csv", "w", newline="")
            w = csv.writer(f)
            w.writerow(["t_s", "mean_A", "min_A", "max_A", "charge_total_C", "lo_fraction",
                        "over_fraction", "n"])
            self._stats_csv = (f, w)
            self._stats_acc = _StatsAcc(r.start)
        acc, rate = self._stats_acc, self.proc.rate
        if r.gap:
            # close the interval at the hole so timestamps stay right
            if acc.n:
                self._stats_csv[1].writerow(acc.row(rate, self.proc.charge))
            self._stats_acc = acc = _StatsAcc(r.start)
        i = 0
        while i < len(r.current):
            take = min(self._stats_n - acc.n, len(r.current) - i)
            acc.add(r.current[i:i + take], r.use_lo[i:i + take], r.over[i:i + take])
            i += take
            if acc.n >= self._stats_n:
                self._stats_csv[1].writerow(acc.row(rate, self.proc.charge))
                self._stats_acc = acc = _StatsAcc(acc.start + acc.n)
        self._stats_csv[0].flush()

    def _display(self, r: Result):
        if r.gap or len(self._disp_carry) == 0:
            self._disp_carry_start = r.start
            self._disp_carry = r.current
        else:
            self._disp_carry = np.concatenate((self._disp_carry, r.current))
        mn, mean, mx = minmax_decimate(self._disp_carry, self._disp_n)
        used = len(mean) * self._disp_n
        t = (self._disp_carry_start + (np.arange(len(mean)) + 0.5) * self._disp_n) / self.proc.rate
        self._disp_carry_start += used
        self._disp_carry = self._disp_carry[used:]
        if r.gap and self.display.n:
            # NaN row breaks the plotted line across the hole
            nan = np.array([np.nan])
            self.display.append([np.array([t[0] if len(t) else r.start / self.proc.rate]), nan, nan, nan])
        self.display.append([t, mn, mean, mx])

    def trace(self, seconds: float, bins: int) -> dict:
        """Recent history re-binned to at most `bins` points."""
        d = self.display.last(seconds) if self.display else np.empty((4, 0))
        t, mn, mean, mx = rebin(d, bins)
        return {"t_s": t, "min_A": mn, "mean_A": mean, "max_A": mx}

    def _on_diag(self, d: dict):
        self.last_diag = d
        if self._diag_csv is None:
            f = open(self.dir / "diag.csv", "w", newline="")
            w = csv.DictWriter(f, fieldnames=["wall_time", *d.keys()], extrasaction="ignore")
            w.writeheader()
            self._diag_csv = (f, w)
        self._diag_csv[1].writerow({"wall_time": time.time(), **d})
        self._diag_csv[0].flush()
        if d["buffer_fill_peak"] > 0.5:
            log.warning("device buffer peak %.0f %% (poll gap %.1f ms)",
                        d["buffer_fill_peak"] * 100, d["max_poll_gap_ms"])
        if d["dropped_blocks"]:
            log.warning("%d blocks dropped - main process too slow", d["dropped_blocks"])

    def log_event(self, ev: dict, level=logging.INFO):
        ev = {"wall_time": time.time(), **ev}
        if not self._events.closed:
            self._events.write(json.dumps(ev) + "\n")
            self._events.flush()
        log.log(level, "event: %s", {k: v for k, v in ev.items() if k not in ("traceback", "wall_time")})


def rebin(d: np.ndarray, bins: int):
    """Merge display bins (t, min, mean, max) down to <= bins points. NaN rows (gaps) survive."""
    n = d.shape[1]
    if n <= bins:
        return d[0], d[1], d[2], d[3]
    f = -(-n // bins)
    m = n // f
    v = d[:, n - m * f:].reshape(4, m, f)
    with np.errstate(all="ignore"):
        t = v[0].mean(axis=1)
        gap = np.isnan(v[2]).any(axis=1)
        mn = np.where(gap, np.nan, np.nanmin(v[1], axis=1))
        mean = np.where(gap, np.nan, np.nanmean(v[2], axis=1))
        mx = np.where(gap, np.nan, np.nanmax(v[3], axis=1))
    return t, mn, mean, mx


class _StatsAcc:
    def __init__(self, start: int):
        self.start, self.n, self.sum, self.n_lo, self.n_over = start, 0, 0.0, 0, 0
        self.mn, self.mx = np.inf, -np.inf

    def add(self, i, use_lo, over):
        if len(i) == 0:
            return
        self.n += len(i)
        self.sum += float(i.sum())
        self.mn = min(self.mn, float(i.min()))
        self.mx = max(self.mx, float(i.max()))
        self.n_lo += int(use_lo.sum())
        self.n_over += int(over.sum())

    def row(self, rate, charge_total):
        return [f"{(self.start + self.n) / rate:.6f}", f"{self.sum / self.n:.6e}", f"{self.mn:.6e}",
                f"{self.mx:.6e}", f"{charge_total:.6e}", f"{self.n_lo / self.n:.4f}",
                f"{self.n_over / self.n:.4f}", self.n]


def _r(t):
    return tuple(round(x, 4) for x in t)


def _fmt_summary(s: dict) -> str:
    if "duration_s" not in s:
        return str(s)
    return (f"{s['duration_s']:.1f} s, mean {si(s['mean_current_A'], 'A')}, "
            f"charge {si(s['charge_mAh'] * 1e-3, 'Ah')}, lo {s['lo_channel_fraction']:.1%}, "
            f"over {s['overrange_fraction']:.2%}, gaps {s['gap_samples']}, corrupt {s['corrupt_samples']}, "
            f"{'VALID' if s['valid'] else 'NOT VALID'}")


def si(x: float | None, unit: str) -> str:
    if x is None or not np.isfinite(x):
        return f"-- {unit}"
    for scale, prefix in ((1, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n")):
        if abs(x) >= scale or scale == 1e-9:
            return f"{x / scale:.4g} {prefix}{unit}"
    return f"{x:.4g} {unit}"
