"""CSV export of measurement windows, for the article's data folder."""

import csv
import json
from pathlib import Path

import numpy as np

TRACE_COLUMNS = ["t_s",
                 "v500_mean_V", "v500_min_V", "v500_max_V",
                 "v50_mean_V", "v50_min_V", "v50_max_V",
                 "i_mean_A", "i_min_A", "i_max_A"]


def window_meta(session, res: dict) -> dict:
    setup = session.setup
    acq = session.cfg.acq
    return {
        "label": res["label"],
        "started": res["started"],
        "session": session.dir.name,
        "vdc_V": res["vdc"],
        "profile": res["profile"],
        "calibration": session.cfg.cal.source,
        "rate_Sps": setup["rate"],
        "scope_offsets_V": [round(o, 4) for o in setup["offsets"]],
        "scope_ranges_V": [round(r, 4) for r in setup["ranges"]],
        "v500_on": f"CH{acq.hi_channel + 1}",
        "v50_on": f"CH{2 - acq.hi_channel}",
        "duration_s": res["duration_s"],
        "valid": res["valid"],
        "events": res["events"],
    }


def write_trace_csv(path: Path, trace: np.ndarray, meta: dict):
    """Binned trace (see TRACE_COLUMNS) with the metadata as '# key: value' header lines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        for k, v in meta.items():
            f.write(f"# {k}: {json.dumps(v)}\n")
        w = csv.writer(f)
        w.writerow(TRACE_COLUMNS)
        for row in trace:
            w.writerow([f"{row[0]:.6f}"] + [f"{x:.6e}" for x in row[1:]])


def write_oneshot_csv(path: Path, cap, pins: dict | None = None):
    """Raw single-shot capture (M5): one row per sample, with the clip DIO bits if captured."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["t_s", "v500_V", "v50_V", "i500_A", "i50_A"]
    bits = {}
    if cap.digital is not None and pins:
        n = len(cap.t)
        dig = cap.digital[:n]
        if len(dig) < n:  # digital buffer shorter than the scope buffer: pad with 0
            dig = np.concatenate([dig, np.zeros(n - len(dig), dig.dtype)])
        for name, dio in pins.items():
            bits[name] = ((dig >> int(dio)) & 1).astype(int)
        cols += list(bits)
    meta = {
        "label": cap.label, "rate_Sps": cap.rate, "n_samples": len(cap.t),
        "triggered": cap.triggered, "gen": cap.gen, "note": cap.note,
        "digital_rate_Sps": cap.digital_rate,
    }
    with open(path, "w", newline="") as f:
        for k, v in meta.items():
            f.write(f"# {k}: {json.dumps(v)}\n")
        w = csv.writer(f)
        w.writerow(cols)
        for i in range(len(cap.t)):
            row = [f"{cap.t[i]:.9f}", f"{cap.v_hi[i]:.6e}", f"{cap.v_lo[i]:.6e}",
                   f"{cap.i_hi[i]:.6e}", f"{cap.i_lo[i]:.6e}"]
            row += [str(bits[name][i]) for name in bits]
            w.writerow(row)


def append_summary(path: Path, row: dict):
    """Append one row; the header is written when the file is new (column order from the first row).
    New columns are added at the end (the file is rewritten, older rows get them empty)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    if not new:
        with open(path, newline="") as f:
            fields = next(csv.reader(f))
        extra = [k for k in row if k not in fields]
        if extra:
            with open(path, newline="") as f:
                rows = list(csv.DictReader(f))
            fields += extra
            with open(path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                w.writerows(rows)
    else:
        fields = list(row)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


def read_trace_csv(path: Path) -> tuple[dict, dict[str, np.ndarray]]:
    """Inverse of write_trace_csv: (metadata, {column: array})."""
    meta, rows, header = {}, [], None
    with open(path, newline="") as f:
        for line in f:
            if line.startswith("# "):
                k, _, v = line[2:].partition(": ")
                meta[k] = json.loads(v)
            elif header is None:
                header = line.strip().split(",")
            else:
                rows.append(line)
    a = np.loadtxt(rows, delimiter=",", ndmin=2) if rows else np.empty((0, len(header)))
    return meta, {name: a[:, i] for i, name in enumerate(header)}
