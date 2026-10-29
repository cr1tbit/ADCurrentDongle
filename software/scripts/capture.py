"""Capture one measurement window for a protocol test and export it.

    <data>/<test>/<label>.csv          binned trace (1 ms), see currentdongle.export
    <data>/<test>/<test>_summary.csv   one row per capture

V+ is switched on for the capture and off afterwards (the D5 LED shows it), so loads
can be changed safely between captures. Switch states are passed in, the app can't see them.

    python scripts/capture.py --test M2 --label M2_0.5R_20k_16k5 --shunt 0.5 --sw3 20k --seconds 20
"""

import argparse
import logging
import time
from pathlib import Path

from currentdongle.acquisition import AcqConfig
from currentdongle.controller import Controller
from currentdongle.export import append_summary, window_meta, write_trace_csv
from currentdongle.processing import Calibration, load_calibration
from currentdongle.session import SessionConfig

ROOT = Path(__file__).resolve().parents[2]
CAL = ROOT / "software" / "calibration.json"


def run_for(ctl, seconds):
    t = time.monotonic()
    while time.monotonic() - t < seconds:
        ctl.pump(0.05)
        time.sleep(0.005)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", required=True, help="protocol test, e.g. M2 (data folder name)")
    ap.add_argument("--label", required=True)
    ap.add_argument("--vdc", type=float, default=3.3)
    ap.add_argument("--shunt", type=float, required=True)
    ap.add_argument("--sw3", choices=["2k", "20k"], required=True)
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--settle", type=float, default=3.0)
    ap.add_argument("--nominal", action="store_true", help="ignore calibration.json (raw/nominal readings)")
    ap.add_argument("--note", default="", help="free text stored in the CSV header and summary")
    ap.add_argument("--raw", action="store_true", help="also keep every sample (session raw.f32) for spectra")
    ap.add_argument("--gen-dc", type=float, help="hold W1 at this DC level during the capture (e.g. BSS138 gate)")
    ap.add_argument("--data", type=Path, default=Path("data"), help="data root (default: ./data)")
    ap.add_argument("--sessions", type=Path, default=Path("sessions"), help="session folder (default: ./sessions)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    cal = Calibration.nominal(args.shunt, args.sw3) if args.nominal else load_calibration(CAL, args.shunt, args.sw3)
    data = args.data / args.test
    cfg = SessionConfig(AcqConfig(vdc=args.vdc), cal, args.sessions, label=args.label,
                        raw=args.raw)
    ctl = Controller(cfg, CAL)
    ctl.start(args.test)
    try:
        ctl.template.cal = cal  # keep --nominal even if a calibration file exists
        ctl.session.cfg.cal = cal
        ctl.session.proc.cal = cal
        if args.gen_dc is not None:
            ctl.set_generator({"mode": "dc", "volts": args.gen_dc}, "script")
        print(f"V+ on, settling {args.settle:g} s, measuring {args.seconds:g} s", flush=True)
        run_for(ctl, args.settle)
        wid = ctl.session.begin_window(args.label, "script", trace_bin_s=1e-3)
        run_for(ctl, args.seconds)
        res = ctl.session.end_window(wid)
        s = ctl.session
        meta = window_meta(s, res) | {"sw3": args.sw3, "shunt_ohm": args.shunt, "note": args.note}
        write_trace_csv(data / f"{args.label}.csv", s.traces[wid], meta)
        row = {
            "label": args.label, "vdc_V": args.vdc, "shunt_ohm": args.shunt, "sw3": args.sw3,
            "calibration": cal.source,
            "v500_mean_mV": round(res["mean_v_hi"] * 1e3, 4), "v500_rms_mV": round(res["std_v_hi"] * 1e3, 4),
            "v50_mean_mV": round(res["mean_v_lo"] * 1e3, 4), "v50_rms_mV": round(res["std_v_lo"] * 1e3, 4),
            "i_mean_A": res["mean_current_A"], "x50_fraction": round(res["lo_channel_fraction"], 4),
            "duration_s": round(res["duration_s"], 4), "valid": res["valid"],
            "temp_C": s.last_diag.get("temp_c"), "started": res["started"], "session": s.dir.name,
            "gen_dc_V": args.gen_dc,
            "note": args.note,
        }
        append_summary(data / f"{args.test}_summary.csv", row)
        print(f"{args.label}: V500 {row['v500_mean_mV']:+.3f} mV (rms {row['v500_rms_mV']:.3f})  "
              f"V50 {row['v50_mean_mV']:+.3f} mV (rms {row['v50_rms_mV']:.3f})  "
              f"I {res['mean_current_A'] * 1e3:.4f} mA ({cal.source})  valid {res['valid']}", flush=True)
    finally:
        if ctl.running and args.gen_dc is not None:
            ctl.set_generator({"mode": "off"}, "script")
        ctl.stop(args.test)
        print("V+ off", flush=True)


if __name__ == "__main__":
    main()
