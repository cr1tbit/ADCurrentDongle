"""Run a single-shot capture plan (protocol M5) and export each capture.

    python scripts/sequence.py --plan plans/example_pulse_response.json           # on hardware
    python scripts/sequence.py --plan plans/example_pulse_response.json --dry      # validate only, no device

Each capture -> <data>/<test>/<label>.csv (raw samples; see currentdongle.export) and a
row in <test>_summary.csv. V+ and the generator are forced off at the end.
"""

import argparse
import logging
from pathlib import Path

import numpy as np

from currentdongle import dwf
from currentdongle.export import append_summary, write_oneshot_csv
from currentdongle.oneshot import Runner, load_plan, validate_gen
from currentdongle.processing import load_calibration

ROOT = Path(__file__).resolve().parents[2]
CAL = ROOT / "software" / "calibration.json"


def gen_label(g: dict) -> str:
    g = validate_gen(g)
    if g["mode"] == "pulses":
        return f"{g['count']}x {g['width_s'] * 1e6:g}us/{g['period_s'] * 1e3:g}ms {g['high_v']:g}V"
    if g["mode"] == "custom":
        return f"custom {len(g['samples'])} samp @ {g['rate'] / 1e6:g}MS/s {g['high_v']:g}V"
    return g["mode"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", type=Path, required=True)
    ap.add_argument("--test", default="M5", help="data folder under --data")
    ap.add_argument("--nominal", action="store_true", help="nominal gains, ignore calibration.json")
    ap.add_argument("--dry", action="store_true", help="load and validate the plan, don't touch the device")
    ap.add_argument("--data", type=Path, default=Path("data"), help="data root (default: ./data)")
    ap.add_argument("--sessions", type=Path, default=Path("sessions"), help="session folder (default: ./sessions)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    plan = load_plan(args.plan)
    print(f"plan: {plan.name}  VDC {plan.vdc} V, shunt {plan.shunt} Ω, SW3 {plan.sw3}, "
          f"CH{plan.hi_channel + 1}=A5")
    print(f"capture: {plan.capture.samples} samples @ {plan.capture.rate / 1e6:g} MS/s, "
          f"trigger={plan.capture.trigger}, digital={plan.capture.digital}")
    caps = [s for s in plan.steps if s.op == "capture"]
    for s in caps:
        validate_gen(s.gen)
        print(f"  capture {s.label!r}: {gen_label(s.gen)}")
    if args.dry:
        print("dry run OK")
        return

    cal = None if args.nominal else load_calibration(CAL, plan.shunt, plan.sw3)
    data = args.data / args.test
    dev = dwf.AD2(config=1)
    print(f"device open, dwf {dwf.version()}")
    try:
        runner = Runner(dev, plan, cal)
        for step in plan.steps:
            cap = runner.step(step)
            if cap is None:
                continue
            write_oneshot_csv(data / f"{cap.label}.csv", cap, plan.capture.digital_pins)
            append_summary(data / f"{args.test}_summary.csv", {
                "label": cap.label, "vdc_V": plan.vdc, "shunt_ohm": plan.shunt, "sw3": plan.sw3,
                "calibration": "nominal" if cal is None else cal.source,
                "rate_Sps": cap.rate, "n_samples": len(cap.t), "gen": gen_label(cap.gen),
                "triggered": cap.triggered, "retries": cap.retries,
                "v500_max_mV": round(float(cap.v_hi.max()) * 1e3, 3),
                "i500_max_mA": round(float(cap.i_hi.max()) * 1e3, 4),
                "i50_max_mA": round(float(cap.i_lo.max()) * 1e3, 4),
                "note": cap.note,
            })
    finally:
        try:
            dev.gen_off()
            dev.set_supply(0.0, False)
        finally:
            dev.close()
        print("V+ off, generator off")


if __name__ == "__main__":
    main()
