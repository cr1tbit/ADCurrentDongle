"""Noise spectrum of a session recorded with --raw.

Reads raw.f32 (interleaved float32 [V_500, V_50]) and prints the RMS per frequency band and
the strongest lines, and writes the averaged spectrum as CSV next to the capture's data:

    python scripts/spectrum.py <session dir> [--skip 3] [--out data/M2/<name>_spectrum.csv]
"""

import argparse
import csv
import json
from pathlib import Path

import numpy as np

BANDS = [(0, 100), (100, 1e3), (1e3, 5e3), (5e3, 20e3), (20e3, 50e3), (50e3, 100e3)]


def welch(x: np.ndarray, rate: float, nfft: int = 8192):
    """One-sided PSD [V^2/Hz] with a Hann window, 50 % overlap."""
    win = np.hanning(nfft)
    scale = 1.0 / (rate * (win ** 2).sum())
    segs = range(0, len(x) - nfft + 1, nfft // 2)
    psd = np.zeros(nfft // 2 + 1)
    for i in segs:
        seg = x[i:i + nfft]
        psd += np.abs(np.fft.rfft((seg - seg.mean()) * win)) ** 2
    psd *= scale / max(len(segs), 1)
    psd[1:-1] *= 2
    return np.fft.rfftfreq(nfft, 1 / rate), psd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session", type=Path)
    ap.add_argument("--skip", type=float, default=3.0, help="seconds to skip (settling)")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    meta = json.loads((args.session / "meta.json").read_text())
    rate = meta["device"]["rate"]
    raw = np.fromfile(args.session / "raw.f32", dtype=np.float32).reshape(-1, 2).astype(np.float64)
    raw = raw[int(args.skip * rate):]
    print(f"{args.session.name}: {len(raw) / rate:.1f} s at {rate:.0f} S/s")

    spectra = {}
    for k, name in enumerate(("v500", "v50")):
        f, psd = welch(raw[:, k], rate)
        spectra[name] = psd
        df = f[1] - f[0]
        total = np.sqrt(psd.sum() * df)
        bands = "  ".join(f"{lo / 1e3:g}-{hi / 1e3:g}k: {np.sqrt(psd[(f >= lo) & (f < hi)].sum() * df) * 1e3:.2f}"
                          for lo, hi in BANDS)
        print(f"  {name}: total {total * 1e3:.2f} mV RMS | per band [mV RMS] {bands}")
        top = np.argsort(psd[5:])[::-1][:6] + 5
        print("        strongest lines: " + ", ".join(
            f"{f[i] / 1e3:.2f} kHz ({np.sqrt(psd[i] * df) * 1e3:.2f} mV)" for i in sorted(top)))

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w", newline="") as fh:
            fh.write(f"# session: {json.dumps(args.session.name)}\n# rate_Sps: {rate}\n")
            w = csv.writer(fh)
            w.writerow(["f_Hz", "v500_psd_V2_per_Hz", "v50_psd_V2_per_Hz"])
            for i in range(len(f)):
                w.writerow([f"{f[i]:.2f}", f"{spectra['v500'][i]:.4e}", f"{spectra['v50'][i]:.4e}"])
        print(f"  spectrum -> {args.out}")


if __name__ == "__main__":
    main()
