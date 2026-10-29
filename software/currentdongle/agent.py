"""MCP server that lets an external agent drive the running GUI.

Streamable HTTP on 127.0.0.1, bearer-token auth, served from a background thread.
Every tool body that touches the controller runs on the Qt main thread (via `Invoker`),
so the agent and the operator never race. Long operations (measure, ask_operator) wait
in the server thread and don't block the GUI.
"""

import asyncio
import concurrent.futures
import logging
import os
import secrets
import threading
import time
from pathlib import Path

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from PySide6 import QtCore

from .controller import Controller, jsonable
from .processing import parse_current

log = logging.getLogger("currentdongle")

TOKEN_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "currentdongle" / "agent_token"
MAX_MEASURE_S = 900  # longer runs: begin_measurement / end_measurement

INSTRUCTIONS = """\
Controls the CurrentDongle + Analog Discovery 2 current measurement setup (operator's GUI).

Hardware: the AD2 V+ supply (VDC) powers the dongle and the DUT through a shunt. Two INA186
channels (x500 and x50) are combined automatically. The active shunt jumper and the SW3 output
filter (2k / 20k) are physical - you cannot change them. Ask the operator with `ask_operator`
to change jumpers, switches, loads or connections, then call `set_profile` to tell the app which
shunt/filter is now fitted (the operator confirms it).

Typical flow: status -> (ask_operator + set_profile) -> start_acquisition -> measure -> ... ->
stop_acquisition. Results include `valid`: false means gaps, corrupted samples, a USB
reconnect or the x50 channel over-range during the window - don't trust such numbers.
Full-scale currents per channel are in `status`. All currents are in amperes, charge in mAh.
Starting the supply needs operator approval in the GUI and may wait for a click.
The waveform generator W1 drives the gate of a test-load MOSFET; `pulse_charge_test` runs protocol M6.
"""


class Invoker(QtCore.QObject):
    """Runs callables on the Qt main thread and hands back a concurrent Future."""

    _sig = QtCore.Signal(object)

    def __init__(self):
        super().__init__()
        self._sig.connect(self._run, QtCore.Qt.QueuedConnection)

    def call(self, fn) -> concurrent.futures.Future:
        fut = concurrent.futures.Future()
        self._sig.emit((fn, fut))
        return fut

    @QtCore.Slot(object)
    def _run(self, job):
        fn, fut = job
        if not fut.set_running_or_notify_cancel():
            return
        try:
            fut.set_result(fn())
        except Exception as e:  # reported to the agent as a tool error
            fut.set_exception(e)


def load_token() -> str:
    try:
        tok = TOKEN_FILE.read_text().strip()
        if tok:
            return tok
    except FileNotFoundError:
        pass
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(tok + "\n")
    TOKEN_FILE.chmod(0o600)
    return tok


class AgentServer:
    def __init__(self, ctl: Controller, ui, port: int = 8765, max_vdc: float = 3.6, confirm_start: bool = True):
        """`ui` provides ask(text, choices) -> concurrent Future[str] and close_ask(future); main thread."""
        self.ctl = ctl
        self.ui = ui
        self.port = port
        self.max_vdc = max_vdc
        self.confirm_start = confirm_start
        self.token = load_token()
        self.enabled = threading.Event()
        self.enabled.set()
        self.inv = Invoker()
        self.mcp = MCPServer("currentdongle", instructions=INSTRUCTIONS)
        self._register()
        self._server = None
        self._thread = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def add_command(self) -> str:
        return (f"claude mcp add --transport http currentdongle {self.url} "
                f"--header \"Authorization: Bearer {self.token}\"")

    # --- server lifecycle -----------------------------------------------------------

    def start(self):
        inner = self.mcp.streamable_http_app()
        expected = f"Bearer {self.token}".encode()

        async def app(scope, receive, send):
            if scope["type"] == "http" and dict(scope["headers"]).get(b"authorization") != expected:
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
            await inner(scope, receive, send)

        cfg = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="on")
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self._server.run, name="agent-mcp", daemon=True)
        self._thread.start()
        t0 = time.monotonic()
        while not self._server.started:
            if not self._thread.is_alive() or time.monotonic() - t0 > 5:
                raise RuntimeError(f"agent server failed to start on port {self.port}")
            time.sleep(0.02)
        log.info("agent server on %s", self.url)

    def stop(self):
        if self._server:
            self._server.should_exit = True
            self._thread.join(timeout=3)

    # --- helpers --------------------------------------------------------------------

    async def _main(self, fn):
        """Run fn on the Qt main thread. Failures go back to the agent as readable tool errors."""
        if not self.enabled.is_set():
            raise ToolError("agent control is disabled by the operator")
        try:
            return await asyncio.wrap_future(self.inv.call(fn))
        except Exception as e:
            raise ToolError(f"{type(e).__name__}: {e}") from None

    async def _ask(self, text: str, choices: list[str], timeout_s: float) -> str:
        fut = await self._main(lambda: self.ui.ask(text, choices))
        try:
            return await asyncio.wait_for(asyncio.wrap_future(fut), timeout_s)
        except asyncio.TimeoutError:
            self.inv.call(lambda: self.ui.close_ask(fut))
            return "timeout"

    # --- tools ----------------------------------------------------------------------

    def _register(self):
        mcp, ctl = self.mcp, self.ctl

        @mcp.tool()
        async def status() -> dict:
            """Current state: running, VDC, shunt/filter profile, calibration, live current, health."""
            return jsonable(await self._main(ctl.status))

        @mcp.tool()
        async def start_acquisition(vdc: float | None = None, reason: str = "") -> dict:
            """Turn on the V+ supply (at `vdc` volts, or the current setting) and start streaming.
            Requires operator approval in the GUI."""
            v = await self._main(lambda: ctl.vdc) if vdc is None else vdc
            if not 0.5 <= v <= self.max_vdc:
                raise ToolError(f"vdc must be within 0.5 .. {self.max_vdc} V (limit set by the operator)")
            if self.confirm_start:
                key = (await self._main(ctl.status))
                text = (f"Agent requests START\n\nVDC {v} V, shunt {key['shunt_ohm']} Ω, filter {key['filter']}"
                        + (f"\n\nReason: {reason}" if reason else ""))
                if await self._ask(text, ["Allow", "Deny"], 300) != "Allow":
                    raise ToolError("operator denied the start")

            def go():
                ctl.set_vdc(v, "agent")
                ctl.start("agent")
                return ctl.status()
            return jsonable(await self._main(go))

        @mcp.tool()
        async def stop_acquisition() -> dict:
            """Stop streaming and switch the supply off. Returns the session summary."""
            return jsonable(await self._main(lambda: ctl.stop("agent")))

        @mcp.tool()
        async def set_vdc(volts: float) -> dict:
            """Change the supply voltage (live if running)."""
            if not 0.5 <= volts <= self.max_vdc:
                raise ToolError(f"volts must be within 0.5 .. {self.max_vdc} V (limit set by the operator)")
            await self._main(lambda: ctl.set_vdc(volts, "agent"))
            return jsonable(await self._main(ctl.status))

        @mcp.tool()
        async def set_profile(shunt_ohm: float, filter: str, reason: str = "") -> dict:
            """Tell the app which shunt jumper and SW3 filter ('2k' or '20k') are fitted. Only while stopped.
            The operator must confirm the hardware matches."""
            text = (f"Agent says the hardware is now:\n\nshunt {shunt_ohm:g} Ω\nSW3 filter {filter}\n\n"
                    "Does the hardware match?" + (f"\n\n{reason}" if reason else ""))
            if await self._ask(text, ["Confirm", "No"], 600) != "Confirm":
                raise ToolError("operator did not confirm the hardware profile")
            await self._main(lambda: ctl.set_profile(shunt_ohm, filter, "agent"))
            return jsonable(await self._main(ctl.status))

        @mcp.tool()
        async def measure(seconds: float, label: str = "") -> dict:
            """Measure for `seconds` (max 900) on the live stream and return mean/min/max current, charge,
            channel usage and validity."""
            if not 0 < seconds <= MAX_MEASURE_S:
                raise ToolError(f"seconds must be within 0 .. {MAX_MEASURE_S}; use begin/end_measurement")
            wid = await self._main(lambda: ctl.begin(label, "agent"))
            return jsonable(await self._wait_window(wid, seconds))

        @mcp.tool()
        async def begin_measurement(label: str = "") -> dict:
            """Open a measurement window for long runs. Close it with end_measurement(id)."""
            wid = await self._main(lambda: ctl.begin(label, "agent"))
            return {"id": wid}

        @mcp.tool()
        async def end_measurement(id: int) -> dict:
            """Close a window opened with begin_measurement and return its result."""
            def end():
                s = ctl.session
                if s is not None and id in s.windows:
                    return ctl.end(id)
                r = ctl.result(id)
                if r is None:
                    raise KeyError(f"no measurement {id}")
                return r
            return jsonable(await self._main(end))

        @mcp.tool()
        async def list_measurements() -> dict:
            """All measurement results since the GUI started (operator's and agent's)."""
            res = await self._main(ctl.results)
            keep = ("id", "label", "source", "profile", "vdc", "duration_s", "mean_current_A", "min_current_A",
                    "max_current_A", "charge_mAh", "lo_channel_fraction", "overrange_fraction", "valid", "events")
            return jsonable({"measurements": [{k: r[k] for k in keep} for r in res]})

        @mcp.tool()
        async def get_trace(seconds: float = 10.0, bins: int = 200) -> dict:
            """Recent current history, re-binned: time [s], min/mean/max current [A] per bin. bins <= 2000."""
            bins = max(1, min(int(bins), 2000))

            def tr():
                if not ctl.running:
                    raise ToolError("acquisition is not running")
                return ctl.session.trace(seconds, bins)
            d = await self._main(tr)
            return jsonable({k: [round(float(x), 12) for x in v] for k, v in d.items()})

        @mcp.tool()
        async def ask_operator(message: str, choices: list[str] | None = None, timeout_s: float = 900) -> dict:
            """Show a message in the GUI and wait for the operator's answer (e.g. to change a jumper or load).
            Returns the chosen option or 'timeout'."""
            choices = choices or ["Done", "Cancel"]
            return {"answer": await self._ask(message, choices, timeout_s)}

        @mcp.tool()
        async def annotate(text: str) -> dict:
            """Add a note to the session log."""
            await self._main(lambda: ctl.annotate(text, "agent"))
            return {"ok": True}

        @mcp.tool()
        async def set_generator(mode: str, volts: float = 0.0, high_v: float = 5.0, width_s: float = 1e-3,
                                period_s: float = 10e-3, count: int = 1) -> dict:
            """Waveform generator W1 (WAV_GEN1, drives the test load's MOSFET gate).
            mode 'off' (0 V), 'dc' (`volts`), or 'pulses': exactly `count` 0 -> high_v pulses of `width_s`
            every `period_s`, then 0 V. The period is nudged so the width is exact (reported back)."""
            cmd = {"mode": mode, "volts": volts, "high_v": high_v, "width_s": width_s,
                   "period_s": period_s, "count": count}
            return jsonable(await self._main(lambda: ctl.set_generator(cmd, "agent")))

        @mcp.tool()
        async def pulse_charge_test(width_s: float, period_s: float, count: int, high_v: float = 5.0,
                                    label: str = "") -> dict:
            """Protocol M6: measure I_on with the gate held high, then the charge of exactly `count` pulses
            (baseline before/after subtracted) and compare with I_on * width * count. Returns error_rel."""
            await self._main(lambda: ctl.start_pulse_test(source="agent", high_v=high_v, width_s=width_s,
                                                          period_s=period_s, count=count, label=label))
            deadline = time.monotonic() + count * period_s * 1.1 + 30
            while time.monotonic() < deadline:
                await asyncio.sleep(0.2)
                p = await self._main(lambda: ctl.pulse_test.progress())
                if p["state"] == "done":
                    return jsonable(p["result"])
                if p["state"] == "failed":
                    raise ToolError(f"pulse test failed: {p['error']}")
            await self._main(lambda: ctl.pulse_test.abort("timeout"))
            raise ToolError("pulse test timed out")

        @mcp.tool()
        async def cal_point(reference: str, seconds: float = 5.0) -> dict:
            """Record a calibration point for the active profile. `reference` is the true current:
            '12.5u', '3.3m', '0', or 'v=3.205 r=330k [rdmm=10M]' (DMM voltage across a measured load)."""
            i_ref = parse_current(reference)
            wid = await self._main(lambda: ctl.begin(f"cal {reference}", "agent"))
            res = await self._wait_window(wid, seconds)
            return jsonable(await self._main(lambda: ctl.cal_add(i_ref, res)))

        @mcp.tool()
        async def cal_fit(save: bool = True) -> dict:
            """Fit gain/offset per channel from the recorded points of the active profile and save them
            (used from the next start)."""
            return jsonable(await self._main(lambda: ctl.cal_fit(save)))

    async def _wait_window(self, wid: int, seconds: float) -> dict:
        deadline = time.monotonic() + seconds + 30  # allow for a reconnect
        try:
            while True:
                await asyncio.sleep(0.1)

                def progress():
                    s = self.ctl.session
                    if s is None or wid not in s.windows:
                        return None
                    return s.window_progress(wid)
                p = await self._main(progress)
                if p is None:  # session stopped underneath us
                    r = await self._main(lambda: self.ctl.result(wid))
                    if r is None:
                        raise ToolError("measurement aborted")
                    return r
                if p["duration_s"] >= seconds or time.monotonic() > deadline:
                    break
        except BaseException:
            # agent cancelled or failed - still close the window so it isn't left open
            self.inv.call(lambda: self.ctl.end(wid) if self.ctl.session and wid in self.ctl.session.windows
                          else None)
            raise
        return await self._main(lambda: self.ctl.end(wid))
