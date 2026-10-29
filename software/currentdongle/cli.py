"""Command line entry point: `currentdongle {info,run,cal}`."""

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path

from .acquisition import AcqConfig
from .controller import LINEAR_MIN_V, Controller
from .processing import Calibration, load_calibration, parse_current, profile_key
from .session import Session, SessionConfig, si

log = logging.getLogger("currentdongle")

HERE = Path(__file__).resolve().parent.parent
DEFAULT_CAL = HERE / "calibration.json"
DEFAULT_OUT = HERE / "sessions"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="currentdongle")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="list devices, configurations and USB/temperature monitor")

    for name, hlp in (("run", "measure current"), ("cal", "two-point calibration (M2)")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--vdc", type=float, required=True, help="AD2 V+ supply = DUT voltage [V]")
        p.add_argument("--shunt", type=float, required=True, help="active shunt [ohm]")
        p.add_argument("--filter", choices=["2k", "20k"], required=True, help="SW3 position")
        p.add_argument("--rate", type=float, default=200e3, help="sample rate per channel [S/s]")
        p.add_argument("--cal", type=Path, default=DEFAULT_CAL, help="calibration file")
        p.add_argument("--out", type=Path, default=DEFAULT_OUT, help="sessions folder")
        p.add_argument("--label", default="", help="appended to the session folder name")
        p.add_argument("--raw", action="store_true", help="store raw channel voltages (1.6 MB/s @ 200 kS/s)")
        p.add_argument("--keep-supply", action="store_true", help="leave V+ on after exit")
        p.add_argument("--no-supply", action="store_true", help="don't touch V+ (testing without power)")
        p.add_argument("--device-config", type=int, default=1, help="AD2 configuration (1 = 16k scope buffer)")
        p.add_argument("--no-reconnect", action="store_true", help="fail instead of waiting for a dropped AD2")
        p.add_argument("--hi-channel", type=int, choices=[1, 2], default=2,
                       help="scope channel carrying the x500 (A5) INA output (measured: CH2)")
        if name == "run":
            p.add_argument("--duration", type=float, default=0, help="stop after N seconds (0 = until Ctrl-C)")
            p.add_argument("--no-gui", action="store_true", help="headless, status on the console")
            p.add_argument("--stats-interval", type=float, default=1.0, help="stats.csv interval [s]")
            p.add_argument("--autostart", action="store_true", help="GUI: start right away (default: operator)")
            p.add_argument("--agent", action="store_true", help="GUI: serve the agent connector (MCP)")
            p.add_argument("--agent-port", type=int, default=8765)
            p.add_argument("--agent-max-vdc", type=float, default=3.6, help="highest VDC the agent may set")
            p.add_argument("--agent-no-confirm", action="store_true",
                           help="agent may start the supply without operator approval")
        else:
            p.add_argument("--seconds", type=float, default=5.0, help="averaging time per point")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    if args.cmd == "info":
        return cmd_info()
    if args.cmd == "run":
        return cmd_run(args)
    return cmd_cal(args)


def _session_config(args, cal: Calibration, **kw) -> SessionConfig:
    acq = AcqConfig(vdc=args.vdc, rate=args.rate, device_config=args.device_config,
                    keep_supply=args.keep_supply or args.no_supply, supply=not args.no_supply,
                    reconnect=not args.no_reconnect, hi_channel=args.hi_channel - 1)
    return SessionConfig(acq=acq, cal=cal, out_dir=args.out, label=args.label, raw=args.raw, **kw)


def cmd_info():
    from . import dwf
    print(f"WaveForms SDK {dwf.version()}")
    devices = dwf.enumerate_devices()
    if not devices:
        print("no devices found")
        return 1
    for d in devices:
        print(f"#{d['index']} {d['name']} SN {d['serial']}")
        for i, b in enumerate(d["ain_buffer_per_config"]):
            print(f"   config {i}: scope buffer {b}")
    dev = dwf.AD2(config=1)
    try:
        m = dev.monitor()
        print(f"USB {m['usb_v']:.3f} V {m['usb_a']:.3f} A, AUX {m['aux_v']:.2f} V, "
              f"temperature {m['temp_c']:.1f} C, supplies {'on' if m['supply_enabled'] else 'off'}")
    finally:
        dev.close(keep_supply=True)
    return 0


def cmd_run(args):
    cal = load_calibration(args.cal, args.shunt, args.filter)
    cfg = _session_config(args, cal, stats_interval_s=args.stats_interval)
    if args.no_gui:
        return _run_headless(Session(cfg), args.duration)
    from .gui import run_gui
    agent = None
    if args.agent:
        agent = {"port": args.agent_port, "max_vdc": args.agent_max_vdc, "confirm_start": not args.agent_no_confirm}
    return run_gui(Controller(cfg, args.cal), autostart=args.autostart, duration=args.duration, agent=agent)


def _run_headless(s: Session, duration: float):
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    s.start()
    t_print = time.monotonic()
    try:
        while not stop.is_set() and s.running:
            s.pump(0.05)
            if not s.backlog():
                time.sleep(0.005)
            if time.monotonic() - t_print >= 1.0:
                t_print = time.monotonic()
                print(status_line(s), flush=True)
            if duration and s.proc and s.proc.duration >= duration:
                break
    finally:
        summary = s.stop()
    print(f"session: {s.dir}")
    return 0 if summary.get("valid") else 2


def status_line(s: Session) -> str:
    p, d = s.proc, s.last_diag
    if p is None:
        return "waiting for device..."
    return (f"{p.duration:8.1f} s  mean {si(p.mean_current, 'A'):>10}  "
            f"Q {si(p.charge / 3600, 'Ah'):>10}  lo {p.n_lo / max(p.n, 1):6.1%}  "
            f"over {p.n_over / max(p.n, 1):6.2%}  gaps {p.gap_samples}  corrupt {p.corrupt_samples}  "
            f"buf {d.get('buffer_fill_peak', 0):4.0%}  USB {d.get('usb_v', 0):.2f} V")


# --- calibration --------------------------------------------------------------

def cmd_cal(args):
    ctl = Controller(_session_config(args, Calibration.nominal(args.shunt, args.filter),
                                     label=args.label or f"cal-{profile_key(args.shunt, args.filter)}"), args.cal)
    ctl.start("cal")
    lock = threading.Lock()  # the controller isn't thread-safe; the pump thread shares it
    pumping = threading.Event()
    pumping.set()

    def pump():
        while pumping.is_set() and ctl.running:
            with lock:
                ctl.pump(0.05)
            time.sleep(0.005)

    th = threading.Thread(target=pump, daemon=True)
    th.start()
    v_sat = ctl.session.proc.v_sat

    print(f"\nCalibrating {ctl.template.cal.key} at VDC {args.vdc} V. "
          f"Linear output window: {LINEAR_MIN_V * 1e3:.0f} mV .. {v_sat:.2f} V")
    print("Per point, connect a load and enter the reference current:")
    print("  '12.5u' / '3.3m' / '0'            current in A (SI prefixes)")
    print("  'v=3.205 r=330k [rdmm=10M]'       DMM voltage across a measured load resistor")
    print("  'done' to fit, 'quit' to abort\n")

    line = ""
    n = 0
    try:
        while True:
            line = input(f"point {n + 1}> ").strip()
            if line in ("done", "quit", "q"):
                break
            try:
                i_ref = parse_current(line)
            except (ValueError, KeyError) as e:
                print(f"  {e}")
                continue
            with lock:
                wid = ctl.begin(f"cal {line}", "operator")
            time.sleep(args.seconds)
            with lock:
                pt = ctl.cal_add(i_ref, ctl.end(wid))
            n += 1
            print(f"  I_ref {si(i_ref, 'A')}:  V_500 {pt['v_hi'] * 1e3:9.3f} mV (σ {pt['std_hi'] * 1e3:.3f})"
                  f"  V_50 {pt['v_lo'] * 1e3:9.3f} mV (σ {pt['std_lo'] * 1e3:.3f})"
                  f"  [{pt['window_hi']} / {pt['window_lo']}]" + ("" if pt["valid"] else "  NOT VALID - ignored"))
        if line in ("quit", "q"):
            print("aborted, nothing saved")
            return 1
        with lock:
            report = ctl.cal_fit(save=False)
        _print_fit(report)
        if report["complete"] or input("save partial calibration? [y/N] ").strip().lower() == "y":
            with lock:
                report = ctl.cal_fit(save=True, allow_partial=True)
            print(f"saved {report['profile']} to {args.cal}")
            return 0
        return 1
    finally:
        pumping.clear()
        th.join(timeout=2)
        if ctl.running:
            ctl.stop("cal")


def _print_fit(report: dict):
    for ch, r in report["channels"].items():
        if "error" in r:
            print(f"{ch}: {r['error']} - keeping nominal gain")
            continue
        print(f"{ch}: gain {r['gain']:.3f} V/V ({r['gain_vs_nominal']:+.2%} vs nominal), "
              f"offset {r['offset_V'] * 1e3:+.3f} mV")
        for p in r["residuals"]:
            rel = f" ({p['residual_rel']:+.3%})" if p["residual_rel"] is not None else ""
            print(f"     {si(p['i_ref'], 'A'):>10}: residual {si(p['residual_A'], 'A')}{rel}")
        for v in r["zero_current_output_V"]:
            print(f"     zero current: {v * 1e3:.3f} mV (fit predicts {r['offset_V'] * 1e3:+.3f} mV - "
                  "a gap means dead band / AD2 offset)")


if __name__ == "__main__":
    sys.exit(main())
