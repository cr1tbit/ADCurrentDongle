"""Acquisition process: drains the AD2 record buffer and ships blocks to the main process.

Runs in its own process so that GUI/processing hiccups in the main process can't
delay polling the device (the AD2 buffer only covers a few tens of ms).

If the AD2 drops off USB (call errors, or no data for `stall_s`), the process keeps
retrying to open it, restores the supply and scope setup, and continues. The sample
index is advanced by the downtime, so the main process sees an exact gap.
"""

import gc
import queue
import time
import traceback
from dataclasses import dataclass

import numpy as np

from . import dwf
from .processing import Block

DIAG_INTERVAL_S = 1.0
CTRL_INTERVAL_S = 0.02


@dataclass
class AcqConfig:
    vdc: float
    rate: float = 200e3
    device_config: int = 1    # 16k-sample scope buffer
    filter: str = "average"
    block_s: float = 0.02
    settle_s: float = 1.0
    keep_supply: bool = False
    supply: bool = True       # False: leave V+ alone (bring-up / testing)
    reconnect: bool = True
    open_timeout_s: float = 5.0   # first open; after a drop we retry until stopped
    retry_s: float = 1.0
    stall_s: float = 2.0
    hi_channel: int = 1       # scope channel (0 = CH1, 1 = CH2) carrying the x500 INA186A5


class DeviceLost(Exception):
    pass


# Messages to the main process: (kind, payload)
#   ("setup", dict)     once, after the device is configured the first time
#   ("block", Block)
#   ("diag", dict)      every DIAG_INTERVAL_S while connected
#   ("event", dict)     lost/corrupted samples, dropped blocks, supply trip,
#                       disconnected/reconnected, vdc_set
#   ("error", str)      fatal, process exits
#   ("closed", None)
#
# Control messages from the main process:
#   ("set_vdc", volts)
#   ("gen", {"mode": "off"} | {"mode": "dc", "volts"} | {"mode": "pulses", "high_v", "width_s", "period_s", "count"})


class _State:
    def __init__(self, cfg: AcqConfig):
        self.cfg = cfg
        self.index = 0             # absolute index of the next sample
        self.rate = None
        self.totals = {"lost": 0, "corrupt": 0, "dropped_blocks": 0}
        self.reconnects = 0
        self.gen = {"mode": "off"}  # restored after a reconnect (DC only)


def run(cfg: AcqConfig, out, stop, ctrl=None, device_cls=None) -> None:
    device_cls = device_cls or dwf.AD2
    st = _State(cfg)
    dev = None
    lost_at = None  # monotonic time of the last disconnect
    try:
        while not stop.is_set():
            try:
                dev = _open(device_cls, cfg, stop, first=st.rate is None)
                if dev is None:
                    break
                setup = _configure(dev, cfg)
                _restore_gen(dev, st, out)
                if st.rate is None:
                    st.rate = setup["rate"]
                    out.put(("setup", setup))
                else:
                    if abs(setup["rate"] - st.rate) > 1e-6 * st.rate:
                        raise RuntimeError(f"rate changed after reconnect: {setup['rate']} != {st.rate}")
                    down = time.monotonic() - lost_at
                    st.index += int(round(down * st.rate))
                    st.reconnects += 1
                    out.put(("event", {"type": "reconnected", "index": st.index, "down_s": down,
                                       "reconnects": st.reconnects, "supply_restored": cfg.supply,
                                       "monitor": setup["monitor"]}))
                _loop(dev, st, setup, out, stop, ctrl)
                break  # stopped normally
            except (dwf.DwfError, DeviceLost) as e:
                if dev is not None:
                    dev.abandon()
                    dev = None
                if st.rate is None or not cfg.reconnect:
                    raise
                lost_at = time.monotonic()
                out.put(("event", {"type": "disconnected", "index": st.index, "reason": str(e)}))
                _sleep(stop, cfg.retry_s)
    except Exception:
        out.put(("error", traceback.format_exc()))
    finally:
        if dev is not None:
            try:
                dev.close(keep_supply=cfg.keep_supply)
            except Exception:
                out.put(("error", traceback.format_exc()))
        out.put(("closed", None))


def _open(device_cls, cfg: AcqConfig, stop, first: bool):
    """Open the device. First time: give up after open_timeout_s. After a drop: retry until stopped."""
    t0 = time.monotonic()
    while not stop.is_set():
        try:
            return device_cls(config=cfg.device_config)
        except dwf.DwfError:
            if first and time.monotonic() - t0 > cfg.open_timeout_s:
                raise
        _sleep(stop, cfg.retry_s)
    return None


def _configure(dev, cfg: AcqConfig) -> dict:
    mon = dev.monitor()
    if cfg.supply:
        dev.set_supply(cfg.vdc, True)
    setup = dev.configure_record(cfg.rate, (cfg.vdc / 2, cfg.vdc / 2), filt=cfg.filter)
    time.sleep(cfg.settle_s)  # supply + scope offset settling
    return {
        "dwf_version": dwf.version(),
        "rate": setup.rate,
        "ranges": setup.ranges,
        "offsets": setup.offsets,
        "buffer_size": setup.buffer_size,
        "gen_buffer": dev.gen_buffer_size(),
        "device_config": cfg.device_config,
        "filter": cfg.filter,
        "vdc": cfg.vdc,
        "supply_enabled_by_app": cfg.supply,
        "monitor_before_supply": mon,
        "monitor": dev.monitor(),
    }


def _apply_gen(dev, g: dict):
    if g["mode"] == "dc":
        dev.gen_dc(g["volts"])
    elif g["mode"] == "pulses":
        dev.gen_pulses(g["high_v"], g["width_s"], g["period_s"], int(g["count"]))
    else:
        dev.gen_off()


def _restore_gen(dev, st: "_State", out):
    if st.gen["mode"] == "dc":
        dev.gen_dc(st.gen["volts"])
        out.put(("event", {"type": "gen_restored", "index": st.index, **st.gen}))
    else:
        if st.gen["mode"] == "pulses":  # an interrupted train is not restarted
            out.put(("event", {"type": "gen_aborted", "index": st.index}))
        st.gen = {"mode": "off"}
        dev.gen_off()


def _check_train(dev, st: "_State", out):
    """A train counts as done only after it was seen running (or ran its full length)."""
    g = st.gen
    state = dev.gen_state()
    if state == dwf.STATE_RUNNING:
        g["seen_running"] = True
    elapsed = time.monotonic() - g["t_start"]
    if state == dwf.STATE_DONE and (g.get("seen_running") or elapsed > g["count"] * g["period_s"]):
        out.put(("event", {"type": "gen_done", "index": st.index, "elapsed_s": elapsed}))
        st.gen = {"mode": "off"}


def _sleep(stop, seconds):
    stop.wait(seconds)


def _send(out, msg, stats) -> bool:
    try:
        out.put_nowait(msg)
        return True
    except queue.Full:
        stats["dropped_blocks"] += 1
        return False


def _loop(dev, st: _State, setup: dict, out, stop, ctrl):
    cfg = st.cfg
    rate, buf = setup["rate"], setup["buffer_size"]
    block_n = max(1, int(rate * cfg.block_s))
    tmp = [np.empty(buf, np.float64) for _ in range(2)]
    pend = [np.empty(block_n + buf, np.float32) for _ in range(2)]
    pend_n = 0
    pend_start = 0
    pend_corrupt = 0
    started = False
    supply_on = cfg.supply  # report a trip once, only for a supply we enabled

    stats = _new_stats()
    t_diag = t_prev = t_ctrl = t_data = time.perf_counter()

    def flush():
        nonlocal pend_n, pend_corrupt
        if pend_n:
            hi, lo = cfg.hi_channel, 1 - cfg.hi_channel
            block = Block(pend_start, pend[hi][:pend_n].copy(), pend[lo][:pend_n].copy(), pend_corrupt)
            _send(out, ("block", block), stats)
        pend_n, pend_corrupt = 0, 0

    dev.start()
    gc.disable()  # no collector pauses while draining; the loop's arrays are freed by refcounting
    try:
        while not stop.is_set():
            state, avail, lost, corrupt = dev.poll()
            now = time.perf_counter()
            if not started:
                if state in (dwf.STATE_CONFIG, dwf.STATE_PREFILL, dwf.STATE_ARMED):
                    if now - t_data > cfg.stall_s:
                        raise DeviceLost(f"acquisition did not start within {cfg.stall_s} s")
                    continue
                started = True
                t_prev = t_data = now
            stats["polls"] += 1
            stats["max_poll_gap"] = max(stats["max_poll_gap"], now - t_prev)
            stats["max_avail"] = max(stats["max_avail"], avail)
            t_prev = now

            if lost or corrupt:
                out.put(("event", {"type": "lost" if lost else "corrupt", "index": st.index,
                                   "lost": lost, "corrupt": corrupt, "avail": avail}))
                stats["lost"] += lost
                stats["corrupt"] += corrupt
            if lost:
                flush()  # the next block starts after the hole
                st.index += lost

            if avail:
                t_data = now
                for ch in (0, 1):
                    dev.read(ch, tmp[ch], avail)
                if pend_n == 0:
                    pend_start = st.index
                pend[0][pend_n:pend_n + avail] = tmp[0][:avail]
                pend[1][pend_n:pend_n + avail] = tmp[1][:avail]
                pend_n += avail
                pend_corrupt += corrupt
                st.index += avail
                stats["samples"] += avail
                if pend_n >= block_n:
                    flush()
            elif now - t_data > cfg.stall_s:
                raise DeviceLost(f"no data for {now - t_data:.1f} s")

            if now - t_ctrl >= CTRL_INTERVAL_S:
                t_ctrl = now
                if ctrl is not None:
                    _handle_ctrl(dev, cfg, ctrl, out, st)
                if st.gen["mode"] == "pulses":
                    _check_train(dev, st, out)

            if now - t_diag >= DIAG_INTERVAL_S:
                mon = dev.monitor()
                if supply_on and not mon["supply_enabled"]:
                    out.put(("event", {"type": "supply_tripped", "index": st.index, "monitor": mon}))
                    supply_on = False
                for k in st.totals:
                    st.totals[k] += stats[k]
                dt = now - t_diag
                out.put(("diag", {
                    "index": st.index,
                    "rate_actual": stats["samples"] / dt,
                    "polls_per_s": stats["polls"] / dt,
                    "max_poll_gap_ms": stats["max_poll_gap"] * 1e3,
                    "max_avail": stats["max_avail"],
                    "buffer_fill_peak": stats["max_avail"] / buf,
                    "lost": stats["lost"],
                    "corrupt": stats["corrupt"],
                    "dropped_blocks": stats["dropped_blocks"],
                    "total_lost": st.totals["lost"],
                    "total_corrupt": st.totals["corrupt"],
                    "total_dropped_blocks": st.totals["dropped_blocks"],
                    "reconnects": st.reconnects,
                    "vdc": cfg.vdc,
                    **mon,
                }))
                stats = _new_stats()
                t_diag = time.perf_counter()
    finally:
        gc.enable()
        flush()


def _handle_ctrl(dev, cfg: AcqConfig, ctrl, out, st: _State):
    while True:
        try:
            cmd, arg = ctrl.get_nowait()
        except queue.Empty:
            return
        if cmd == "set_vdc":
            cfg.vdc = float(arg)  # also used when restoring after a reconnect
            if cfg.supply:
                dev.set_supply(cfg.vdc, True)
            out.put(("event", {"type": "vdc_set", "index": st.index, "vdc": cfg.vdc}))
        elif cmd == "gen":
            _apply_gen(dev, arg)
            st.gen = {**arg, "t_start": time.monotonic()}
            out.put(("event", {"type": "gen_set", "index": st.index, **arg}))


def _new_stats() -> dict:
    return {"polls": 0, "samples": 0, "max_poll_gap": 0.0, "max_avail": 0,
            "lost": 0, "corrupt": 0, "dropped_blocks": 0}
