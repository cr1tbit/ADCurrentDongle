"""Identify the SW3 output-filter position from a raw pulse capture (session recorded with --raw).

Fits the exponential decay of A2 after each pulse (gate off -> current stops, so the decay is the
output RC alone). 2 kHz: 3k3 / 22 nF, tau 72.6 us; 20 kHz: 300 R / 22 nF, tau 6.6 us.

    python scripts/filter_tau.py sessions/<session>
"""

import argparse
import json
from pathlib import Path

import numpy as np


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session", type=Path)
    args = ap.parse_args()
    rate = json.loads((args.session / "meta.json").read_text())["acquisition"]["rate"]
    raw = np.fromfile(args.session / "raw.f32", dtype=np.float32).reshape(-1, 2).astype(np.float64)
    lo = raw[:, 1]
    on = lo > 0.06
    falls = np.flatnonzero(np.diff(on.astype(int)) == -1)
    taus = []
    for f in falls:
        k = f - 20 + int(np.argmax(lo[f - 20:f + 5]))  # peak just before the fall
        base = np.median(lo[k + int(2e-3 * rate):k + int(3e-3 * rate)]) if k + int(3e-3 * rate) < len(lo) else None
        if base is None:
            continue
        seg = lo[k + 1:k + 1 + int(150e-6 * rate)] - base
        good = seg > 0.05 * (lo[k] - base)
        if good.sum() < 3:
            continue
        t = np.arange(len(seg))[good] / rate
        taus.append(-1 / np.polyfit(t, np.log(seg[good]), 1)[0])
    taus = np.array(taus)
    print(f"{args.session.name}: {len(taus)} pulses, tau {np.median(taus) * 1e6:.1f} us "
          f"(spread {np.percentile(taus, 10) * 1e6:.1f} .. {np.percentile(taus, 90) * 1e6:.1f})")
    print("-> 2 kHz position" if np.median(taus) > 30e-6 else "-> 20 kHz position")


if __name__ == "__main__":
    main()
