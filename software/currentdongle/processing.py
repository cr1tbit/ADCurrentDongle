"""Signal processing: calibration, range switching, charge integral. Pure numpy, no hardware."""

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

NOMINAL_GAIN = {"hi": 500.0, "lo": 50.0}  # INA186A5, INA186A2

# Range-switch hold-off, applied before and after each overload: INA186 overload recovery
# (~80 us) + ~5 tau of the output RC filter (2 kHz: 3k3 / 22 nF, tau 73 us; 20 kHz: 300 R, 6.6 us).
# The SW3 labels were found swapped once on the bench, so both use the value for the slower filter.
HOLDOFF_S = {"2k": 0.5e-3, "20k": 0.5e-3}

# A5 counts as clipped where it reads below A2 by more than this (A2 output volts, ~50 x its
# noise, above the 13 kHz A5 oscillation) plus a relative margin for calibration differences
DISAGREE_V = 15e-3
DISAGREE_REL = 0.03

@dataclass
class ChannelCal:
    gain: float    # effective V/V, includes shunt/jumper/loading errors
    offset: float  # output voltage at zero current [V]

    def current(self, v: np.ndarray, shunt: float) -> np.ndarray:
        return (v - self.offset) / (self.gain * shunt)


@dataclass
class Calibration:
    shunt: float
    filter: str
    hi: ChannelCal
    lo: ChannelCal
    source: str = "nominal"
    created: str = ""
    points: list = field(default_factory=list)

    @property
    def key(self) -> str:
        return profile_key(self.shunt, self.filter)

    @classmethod
    def nominal(cls, shunt: float, filt: str) -> "Calibration":
        return cls(shunt, filt, ChannelCal(NOMINAL_GAIN["hi"], 0.0), ChannelCal(NOMINAL_GAIN["lo"], 0.0))

    @classmethod
    def from_dict(cls, d: dict) -> "Calibration":
        d = dict(d)
        d["hi"], d["lo"] = ChannelCal(**d["hi"]), ChannelCal(**d["lo"])
        return cls(**d)


def profile_key(shunt: float, filt: str) -> str:
    return f"{shunt:g}ohm-{filt}"


def load_calibration(path: Path, shunt: float, filt: str) -> Calibration:
    if path.exists():
        profiles = json.loads(path.read_text()).get("profiles", {})
        if (d := profiles.get(profile_key(shunt, filt))) is not None:
            return Calibration.from_dict(d)
    return Calibration.nominal(shunt, filt)


def save_calibration(path: Path, cal: Calibration):
    data = json.loads(path.read_text()) if path.exists() else {"profiles": {}}
    data["profiles"][cal.key] = asdict(cal)
    path.write_text(json.dumps(data, indent=2))


def fit_channel(i_ref: np.ndarray, v: np.ndarray, shunt: float) -> tuple[ChannelCal, np.ndarray]:
    """Least-squares fit of V = gain * I * R + offset. Returns the cal and residuals in amps."""
    x = np.asarray(i_ref, float) * shunt
    v = np.asarray(v, float)
    gain, offset = np.polyfit(x, v, 1)
    cal = ChannelCal(float(gain), float(offset))
    return cal, cal.current(v, shunt) - i_ref


@dataclass
class Block:
    """Samples from the acquisition process. `start` is the absolute sample index."""
    start: int
    hi: np.ndarray
    lo: np.ndarray
    corrupt: int = 0


@dataclass
class Result:
    start: int
    current: np.ndarray
    use_lo: np.ndarray
    over: np.ndarray
    gap: int  # samples missing right before this result
    hi: np.ndarray
    lo: np.ndarray


class Processor:
    """Turns raw channel voltages into one current trace, keeping running totals.

    A5 clips *before* the output filter, so its filtered output can stay below `v_sat` during
    a short pulse while already being wrong (a 100 us pulse on the 2 kHz filter peaks at ~2.3 V).
    The low-gain channel is therefore used within `holdoff_s` before and after any sample where
    A5 is above `v_sat` or reads clearly less current than A2 (only clipping does that).
    A2 stays linear, and a linear filter keeps the pulse area, so the charge is right.

    The look-back delays the output by `holdoff_s`: the newest samples are held until the
    next block. Results start at `out_index`, which lags `next_index` by up to that much.
    """

    def __init__(self, cal: Calibration, rate: float, v_sat: float, holdoff_s: float):
        self.cal = cal
        self.rate = rate
        self.v_sat = v_sat
        self.holdoff = int(round(holdoff_s * rate))
        self.reset()

    def reset(self):
        self.next_index = None  # next expected input sample
        self.out_index = None   # next sample to be emitted
        self.last_trig = -(1 << 62)
        self._hold_hi = np.empty(0)
        self._hold_lo = np.empty(0)
        self.charge = 0.0  # [C]
        self.n = 0
        self.n_lo = 0
        self.n_over = 0
        self.gap_samples = 0
        self.corrupt_samples = 0
        self.i_min = np.inf
        self.i_max = -np.inf

    def process(self, b: Block) -> list[Result]:
        """Returns the samples that are now decided: usually one Result, two around a gap
        (the held samples before it, then the new ones; `gap` is set on the latter)."""
        shunt = self.cal.shunt
        gap = 0
        out = []
        if self.next_index is None:
            self.out_index = b.start
        elif b.start > self.next_index:
            gap = b.start - self.next_index
            self.gap_samples += gap
            # unknown what happened in the gap - treat it as an overload on both sides
            if len(self._hold_hi):
                out.append(self._emit(self._hold_hi, self._hold_lo, np.ones(len(self._hold_hi), bool)))
            self._hold_hi = self._hold_lo = np.empty(0)
            self.out_index = b.start
            self.last_trig = b.start - 1
        self.next_index = b.start + len(b.hi)
        self.corrupt_samples += b.corrupt

        hi = np.concatenate((self._hold_hi, b.hi.astype(np.float64)))
        lo = np.concatenate((self._hold_lo, b.lo.astype(np.float64)))
        n = len(hi)
        idx = self.out_index + np.arange(n, dtype=np.int64)
        i_lo, i_hi = self.cal.lo.current(lo, shunt), self.cal.hi.current(hi, shunt)
        margin = DISAGREE_V / (self.cal.lo.gain * shunt) + DISAGREE_REL * np.abs(i_lo)
        trig = (hi > self.v_sat) | (i_lo - i_hi > margin)
        far = 1 << 62
        last = np.maximum(np.maximum.accumulate(np.where(trig, idx, -far)), self.last_trig)
        nxt = np.minimum.accumulate(np.where(trig, idx, far)[::-1])[::-1]
        use_lo = ((idx - last) <= self.holdoff) | ((nxt - idx) <= self.holdoff)

        m = max(0, n - self.holdoff)  # the rest still needs look-ahead
        if m:
            self.last_trig = int(last[m - 1])
        self._hold_hi, self._hold_lo = hi[m:], lo[m:]
        r = self._emit(hi[:m], lo[:m], use_lo[:m])
        r.gap = gap
        out.append(r)
        return out

    def _emit(self, hi: np.ndarray, lo: np.ndarray, use_lo: np.ndarray) -> Result:
        start = self.out_index
        shunt = self.cal.shunt
        current = np.where(use_lo, self.cal.lo.current(lo, shunt), self.cal.hi.current(hi, shunt))
        over = lo > self.v_sat
        n = len(current)
        self.out_index += n
        self.charge += current.sum() / self.rate
        self.n += n
        self.n_lo += int(use_lo.sum())
        self.n_over += int(over.sum())
        if n:
            self.i_min = min(self.i_min, float(current.min()))
            self.i_max = max(self.i_max, float(current.max()))
        return Result(start, current, use_lo, over, 0, hi, lo)

    @property
    def duration(self) -> float:
        return self.n / self.rate

    @property
    def mean_current(self) -> float:
        return self.charge / self.duration if self.n else float("nan")

    def summary(self) -> dict:
        return {
            "duration_s": self.duration,
            "charge_C": self.charge,
            "charge_mAh": self.charge / 3.6,
            "mean_current_A": self.mean_current,
            "min_current_A": self.i_min if self.n else None,
            "max_current_A": self.i_max if self.n else None,
            "lo_channel_fraction": self.n_lo / self.n if self.n else 0.0,
            "overrange_fraction": self.n_over / self.n if self.n else 0.0,
            "gap_samples": self.gap_samples,
            "gap_s": self.gap_samples / self.rate,
            "corrupt_samples": self.corrupt_samples,
            "valid": self.gap_samples == 0 and self.corrupt_samples == 0 and self.n_over == 0,
        }


def minmax_decimate(x: np.ndarray, factor: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per-bin (min, mean, max), dropping a trailing partial bin."""
    m = len(x) // factor
    if m == 0:
        e = np.empty(0)
        return e, e, e
    v = x[: m * factor].reshape(m, factor)
    return v.min(axis=1), v.mean(axis=1), v.max(axis=1)


def timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


SI = {"": 1, "m": 1e-3, "u": 1e-6, "n": 1e-9, "k": 1e3, "M": 1e6, "G": 1e9}


def parse_current(text: str) -> float:
    """'12.5u' / '3.3m' / '0' -> A;  'v=3.21 r=330k [rdmm=10M]' -> V/R, corrected for DMM loading.

    rdmm=inf: the DMM was disconnected during the reading, no correction.
    """
    text = text.strip().replace("µ", "u")
    if "=" in text:
        kv = {k.lower(): v for k, v in re.findall(r"(\w+)\s*=\s*(\S+)", text)}
        v, r = _si_number(kv["v"]), _si_number(kv["r"])
        rdmm = kv.get("rdmm", "10M")
        if rdmm.lower() in ("inf", "off", "none"):
            return v / r
        return v * (1 / r + 1 / _si_number(rdmm))
    return _si_number(text)


def _si_number(t: str) -> float:
    # case matters: m = milli, M = mega
    m = re.fullmatch(r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*([munkMG]?)[aAvV]?", t.strip())
    if not m:
        raise ValueError(f"can't parse {t!r}")
    return float(m.group(1)) * SI[m.group(2)]
