# CurrentDongle app

Live current measurement with the CurrentDongle + Analog Discovery 2, replacing the WaveForms math-channel scripts.

- Streams both INA186 channels (A5 ×500 → CH1, A2 ×50 → CH2) at 200 kS/s with the AD2's hardware averaging, so short pulses aren't skipped.
- Switches to the low-gain channel while A5 is saturated, with a hold-off after each overload (INA186 recovery ~80 µs + output filter tail).
- Integrates charge, and tracks every way the result can go wrong: lost/corrupted samples, dropped blocks, A2 over-range, supply trips.
- The acquisition runs in its own process, so the GUI can't stall the device polling.
- Survives USB drops: it reconnects automatically, restores the supply, and marks the gap (see below).
- Optional agent connector (MCP), so an AI agent can run measurements in the operator's GUI.

## Setup

Requires the WaveForms runtime + SDK (`libdwf`).

```
cd software
uv venv --system-site-packages .venv      # reuses system PySide6/numpy if present
uv pip install --python .venv/bin/python -e '.[test]'
.venv/bin/python -m pytest -q tests
```

## Usage

```
.venv/bin/currentdongle info                                         # devices, buffers, USB/temperature
.venv/bin/currentdongle cal --vdc 3.3 --shunt 0.5 --filter 2k         # two-point calibration (M2)
.venv/bin/currentdongle run --vdc 3.3 --shunt 0.5 --filter 2k         # live GUI
.venv/bin/currentdongle run --vdc 3.3 --shunt 0.5 --filter 2k --agent # GUI + agent connector
.venv/bin/currentdongle run --vdc 3.3 --shunt 100 --filter 2k --no-gui --duration 600 --label sleep
```

### GUI

The GUI opens idle. The operator presses **Start** to switch V+ on and start streaming (`--autostart` starts right away).
- **Stop** ends the session (one session folder per Start/Stop).
- Shunt and SW3 can be changed while stopped. VDC can also be changed while running, within the scope window set at start.
- **Begin/End measurement** marks a window on the live stream. Its result goes to the *Measurements* table and `measurements.jsonl`.
- The *Activity* tab logs operator and agent actions, and the agent's questions.
- The plot shows a min/max envelope plus the mean per bin, re-binned to ~1500 points whatever the time window.

### Waveform generator (W1 = WAV_GEN1)

Drives the gate of a MOSFET-switched test load (*Generator* tab, or agent `set_generator`):
- **Off** (0 V), **DC**, or a **pulse train** of exactly N pulses (0 → high), after which the output returns to 0 V.
- The generator is off at start, at stop and on close. After a USB reconnect, DC is restored, but an interrupted train is not restarted (logged as `gen_aborted`).
- The pulse period is nudged so the width is a whole number of generator samples: the AD2 renders a period from 1024 samples in device config 1. Otherwise the width is off by up to half a sample (−0.4 % for 1 ms in 10 ms).

**Pulse charge test (M6):**
1. Gate held on → I_on.
2. Baseline before → N pulses in one window → baseline after.
3. Q_meas (baseline-corrected, drift interpolated) is compared with I_on · width · N.

### Channel mapping

`--hi-channel 2` (default) means the x500 INA186A5 output reaches scope CH2 and the x50 A2 output reaches CH1, as measured on this board. Use `--hi-channel 1` for a board wired the other way.

### USB drops

If the AD2 drops off USB, the app keeps retrying every second (call errors, or 2 s without data, count as a drop):
- The GUI shows *DISCONNECTED – reconnecting*.
- On reconnect it restores the scope setup and the supply (at the current VDC), and continues the same session.
- The downtime becomes an exact gap in the sample index. It is logged in `events.jsonl`, and any session or measurement spanning it is marked **not valid**.
- The DUT loses power while the AD2 is gone, and is powered again on reconnect.

`--no-reconnect` fails instead. The very first open gives up after 5 s if no device is found.

### Agent connector

With `--agent`, the GUI serves an MCP server on `http://127.0.0.1:8765/mcp` (localhost only, bearer token). The token is kept in `~/.config/currentdongle/agent_token`, so the command to register it only needs running once. It is printed at startup, e.g. for Claude Code:

```
claude mcp add --transport http currentdongle http://127.0.0.1:8765/mcp --header "Authorization: Bearer <token>"
```

| Tool | Does |
|---|---|
| `status` | running, VDC, shunt/filter, calibration, full-scale currents, live current, health |
| `start_acquisition(vdc, reason)` / `stop_acquisition` | start needs the operator's **Allow** in the GUI |
| `set_vdc(volts)` | live if running; capped by `--agent-max-vdc` (default 3.6 V) |
| `set_profile(shunt_ohm, filter)` | while stopped; the operator must **Confirm** the hardware matches |
| `measure(seconds, label)` | up to 900 s; longer runs: `begin_measurement` / `end_measurement(id)` |
| `get_trace(seconds, bins)` | recent min/mean/max history |
| `ask_operator(message, choices)` | dialog in the GUI, waits for the answer (jumpers, loads, wiring) |
| `cal_point(reference, seconds)`, `cal_fit(save)` | calibration (M2), same inputs as `cal` |
| `set_generator(mode, …)` | W1: `off`, `dc` (volts), `pulses` (high_v, width_s, period_s, count), max 5 V |
| `pulse_charge_test(width_s, period_s, count)` | protocol M6, returns measured vs. expected charge |
| `list_measurements`, `annotate(text)` | results so far, notes in the session log |

The operator stays in charge:
- The *agent control* checkbox cuts the agent off at any time.
- Every agent action is shown in the Activity tab and logged.
- Tool calls run on the GUI thread one at a time, so they can't race the operator.
- `--agent-no-confirm` lets the agent start the supply without asking.

- `--vdc` sets **and enables** the AD2 V+ supply (= DUT voltage). It is switched off on exit unless `--keep-supply` is set. `--no-supply` leaves V+ alone (bring-up without power).
- `--shunt` and `--filter` must match the fitted jumper and the SW3 position. They select the calibration profile in `calibration.json` (falls back to nominal gains, with a warning).
- `--raw` additionally stores the raw channel voltages (1.6 MB/s) for offline re-processing.

### Calibration

`cal` streams continuously. For each load you enter the reference current, and it averages both channels for `--seconds` (default 5). Inputs:

| Input | Meaning |
|---|---|
| `12.5u`, `3.3m`, `0` | current in A (case matters: `m` milli, `M` mega) |
| `v=3.205 r=330k` | DMM voltage across a measured load resistor, `I = V·(1/R + 1/R_dmm)`, R_dmm = 10 MΩ |
| `v=3.205 r=330k rdmm=11M` | same, with your DMM's input resistance |
| `v=3.300 r=10M rdmm=inf` | DMM disconnected during the reading (high-ohm loads), no correction |
| `done` / `quit` | fit and save / abort |

Each channel is fitted (`V = gain·I·R + offset`) from the points inside its linear window (30 mV … VDC − 0.15 V). Aim for ~50 mV and ~2 V per channel (see the ADCurrentDongle_LabSession repo, M2). A `0` point isn't used for the fit, but shows the gap between the true zero-current output and the fitted offset (dead band / AD2 offset).

## Session output

Each run creates `sessions/<date>-<time>[-label]/`:

| File | Content |
|---|---|
| `meta.json` | settings, calibration used, actual device rate/range/offset, versions |
| `app.log` | the console log |
| `events.jsonl` | lost/corrupted samples, gaps, dropped blocks, disconnects/reconnects, supply trips, VDC changes, notes, errors, cal points |
| `measurements.jsonl` | one line per measurement window (operator, agent, cal) |
| `diag.csv` | per second: sample rate, polls/s, longest poll gap, device buffer peak, USB V/A, temperature |
| `stats.csv` | per interval: mean/min/max current, running charge, low-channel and over-range fractions |
| `summary.json` | totals and `valid` (no gaps, no corruption, no over-range) |
| `raw.f32` | with `--raw`: interleaved float32 `[V_500, V_50]` |

`run --no-gui` exits with code 2 if the result isn't valid.

## Benchmark

`scripts/stream_benchmark.py` measures sustained two-channel streaming per sample rate (lost/corrupted samples, buffer peak).
