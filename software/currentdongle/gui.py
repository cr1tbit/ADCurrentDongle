"""Live view and operator controls. Optionally hosts the agent (MCP) server."""

import concurrent.futures
import logging
import time

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from .controller import Controller, gen_text
from .session import si

log = logging.getLogger("currentdongle")

WINDOWS_S = [1, 2, 5, 10, 30, 60, 120]
PLOT_POINTS = 1500
TICK_MS = 50
LOG_FLOOR = 1e-10
SHUNTS = ["0.5", "1", "100"]


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctl: Controller, duration: float = 0):
        super().__init__()
        self.ctl = ctl
        self.duration = duration
        self.agent = None
        self.window_s = 10.0
        self.paused = False
        self.op_window: int | None = None
        self._t_labels = 0.0
        self._n_results = -1
        self._asks: dict[concurrent.futures.Future, QtWidgets.QMessageBox] = {}

        self.setWindowTitle("CurrentDongle")
        pg.setConfigOptions(antialias=False)
        self._build()
        ctl.listeners.append(self._activity_line)
        self._sync_controls()

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(TICK_MS)

    # --- layout -----------------------------------------------------------------------

    def _build(self):
        bar = QtWidgets.QHBoxLayout()
        self.btn_run = QtWidgets.QPushButton("Start")
        self.btn_run.setMinimumWidth(90)
        self.btn_run.clicked.connect(self._toggle_run)
        self.vdc = QtWidgets.QDoubleSpinBox()
        self.vdc.setRange(0.5, 5.0)
        self.vdc.setSingleStep(0.1)
        self.vdc.setDecimals(2)
        self.vdc.setSuffix(" V")
        self.vdc.setValue(self.ctl.vdc)
        self.vdc.setKeyboardTracking(False)
        self.vdc.valueChanged.connect(self._set_vdc)
        self.shunt = QtWidgets.QComboBox()
        self.shunt.setEditable(True)
        self.shunt.addItems(SHUNTS)
        self.shunt.setCurrentText(f"{self.ctl.profile[0]:g}")
        self.shunt.lineEdit().editingFinished.connect(self._set_profile)
        self.shunt.activated.connect(self._set_profile)
        self.filt = QtWidgets.QComboBox()
        self.filt.addItems(["2k", "20k"])
        self.filt.setCurrentText(self.ctl.profile[1])
        self.filt.activated.connect(self._set_profile)
        self.btn_meas = QtWidgets.QPushButton("Begin measurement")
        self.btn_meas.clicked.connect(self._toggle_measurement)
        self.meas_label = QtWidgets.QLineEdit()
        self.meas_label.setPlaceholderText("label")
        self.meas_label.setMaximumWidth(160)
        for w in (self.btn_run, QtWidgets.QLabel("VDC"), self.vdc, QtWidgets.QLabel("shunt Ω"), self.shunt,
                  QtWidgets.QLabel("SW3"), self.filt, self.btn_meas, self.meas_label):
            bar.addWidget(w)
        bar.addStretch()
        self.agent_box = QtWidgets.QCheckBox("agent control")
        self.agent_box.setChecked(True)
        self.agent_box.setVisible(False)
        self.agent_box.toggled.connect(self._toggle_agent)
        bar.addWidget(self.agent_box)

        self.plot = pg.PlotWidget()
        self.plot.setLabel("left", "current", units="A")
        self.plot.setLabel("bottom", "time", units="s")
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        self.c_env = pg.PlotCurveItem(pen=pg.mkPen((80, 140, 220, 150)))
        self.c_mean = pg.PlotCurveItem(pen=pg.mkPen((240, 240, 240), width=1), connect="finite")
        self.plot.addItem(self.c_env)
        self.plot.addItem(self.c_mean)

        view = QtWidgets.QHBoxLayout()
        self.win_box = QtWidgets.QComboBox()
        self.win_box.addItems([f"{w} s" for w in WINDOWS_S])
        self.win_box.setCurrentIndex(WINDOWS_S.index(10))
        self.win_box.currentIndexChanged.connect(lambda i: setattr(self, "window_s", WINDOWS_S[i]))
        self.log_y = QtWidgets.QCheckBox("log")
        self.log_y.toggled.connect(lambda on: self.plot.setLogMode(y=on))
        pause = QtWidgets.QPushButton("Pause")
        pause.setCheckable(True)
        pause.toggled.connect(lambda on: setattr(self, "paused", on))
        reset = QtWidgets.QPushButton("Reset integral")
        reset.clicked.connect(lambda: self.ctl.session and self.ctl.session.reset_integral())
        for w in (self.win_box, self.log_y, pause, reset):
            view.addWidget(w)

        side = QtWidgets.QVBoxLayout()
        self.lbl = {}
        for group, keys in (("Totals", ["duration", "mean", "charge", "window", "minmax"]),
                            ("Health", ["state", "valid", "lo", "over", "gaps", "corrupt", "buffer", "poll",
                                        "dropped", "reconnects", "usb", "temp"])):
            box = QtWidgets.QGroupBox(group)
            form = QtWidgets.QFormLayout(box)
            for k in keys:
                self.lbl[k] = QtWidgets.QLabel("--")
                self.lbl[k].setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
                form.addRow(k, self.lbl[k])
            side.addWidget(box)
        big = self.lbl["mean"].font()
        big.setPointSize(big.pointSize() + 4)
        big.setBold(True)
        for k in ("mean", "charge"):
            self.lbl[k].setFont(big)
        side.addLayout(view)
        self.dir_lbl = QtWidgets.QLabel("")
        self.dir_lbl.setWordWrap(True)
        self.dir_lbl.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        side.addWidget(self.dir_lbl)
        side.addStretch()
        panel = QtWidgets.QWidget()
        panel.setLayout(side)
        panel.setFixedWidth(340)

        self.table = QtWidgets.QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(["#", "label", "source", "duration", "mean", "min", "max",
                                              "charge", "x50 %", "valid"])
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.activity = QtWidgets.QPlainTextEdit()
        self.activity.setReadOnly(True)
        self.activity.setMaximumBlockCount(2000)
        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self.table, "Measurements")
        tabs.addTab(self.activity, "Activity")
        tabs.addTab(self._build_generator(), "Generator")

        upper = QtWidgets.QWidget()
        ul = QtWidgets.QHBoxLayout(upper)
        ul.setContentsMargins(0, 0, 0, 0)
        ul.addWidget(self.plot, 1)
        ul.addWidget(panel)
        split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        split.addWidget(upper)
        split.addWidget(tabs)
        split.setSizes([520, 200])

        root = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(root)
        lay.addLayout(bar)
        lay.addWidget(split, 1)
        self.setCentralWidget(root)
        self.resize(1350, 780)

    def _build_generator(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QGridLayout(w)

        def spin(lo, hi, val, dec, suffix, step=None):
            b = QtWidgets.QDoubleSpinBox()
            b.setRange(lo, hi)
            b.setDecimals(dec)
            b.setValue(val)
            b.setSuffix(suffix)
            if step:
                b.setSingleStep(step)
            return b

        self.g_dc = spin(0, 5, 5, 2, " V", 0.1)
        self.g_high = spin(0, 5, 5, 2, " V", 0.1)
        self.g_width = spin(1, 10_000_000, 1000, 1, " µs")
        self.g_period = spin(0.002, 100_000, 10, 3, " ms")
        self.g_count = QtWidgets.QSpinBox()
        self.g_count.setRange(1, 1_000_000)
        self.g_count.setValue(200)
        b_off = QtWidgets.QPushButton("Off (0 V)")
        b_off.clicked.connect(lambda: self._gen({"mode": "off"}))
        b_dc = QtWidgets.QPushButton("DC")
        b_dc.clicked.connect(lambda: self._gen({"mode": "dc", "volts": self.g_dc.value()}))
        b_pulses = QtWidgets.QPushButton("Pulse train")
        b_pulses.clicked.connect(lambda: self._gen(self._train_params(mode="pulses")))
        self.b_ptest = QtWidgets.QPushButton("Run pulse charge test (M6)")
        self.b_ptest.clicked.connect(self._pulse_test)
        self.g_status = QtWidgets.QLabel("generator off")
        self.g_status.setWordWrap(True)
        self.g_status.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)

        lay.addWidget(b_off, 0, 0)
        lay.addWidget(b_dc, 0, 1)
        lay.addWidget(self.g_dc, 0, 2)
        for col, (name, box) in enumerate((("high", self.g_high), ("width", self.g_width),
                                           ("period", self.g_period), ("count", self.g_count))):
            lay.addWidget(QtWidgets.QLabel(name), 1, 2 * col)
            lay.addWidget(box, 1, 2 * col + 1)
        lay.addWidget(b_pulses, 2, 0)
        lay.addWidget(self.b_ptest, 2, 1, 1, 3)
        lay.addWidget(self.g_status, 3, 0, 1, 8)
        lay.setRowStretch(4, 1)
        return w

    def _train_params(self, **extra) -> dict:
        return {"high_v": self.g_high.value(), "width_s": self.g_width.value() * 1e-6,
                "period_s": self.g_period.value() * 1e-3, "count": self.g_count.value(), **extra}

    def _gen(self, cmd: dict):
        try:
            self.ctl.set_generator(cmd)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "CurrentDongle", str(e))

    def _pulse_test(self):
        try:
            self.ctl.start_pulse_test(label=self.meas_label.text().strip(), **self._train_params())
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "CurrentDongle", str(e))

    def _gen_status(self):
        s = self.ctl.session
        gen = s.gen if s else {"mode": "off"}
        txt = "generator " + gen_text(gen) if gen.get("mode") else "generator off"
        t = self.ctl.pulse_test
        if t is not None:
            if t.state == "done":
                r = t.result
                txt += (f"\nlast pulse test {r['label']}: Q {r['q_measured_uAh']:.5g} µAh vs expected "
                        f"{r['q_expected_uAh']:.5g} µAh, error {r['error_rel']:+.2%}, I_on {si(r['i_on_A'], 'A')}, "
                        f"period {r['train']['period_s'] * 1e3:.4f} ms" + ("" if r["valid"] else "  NOT VALID"))
            elif t.state == "failed":
                txt += f"\nlast pulse test failed: {t.error}"
            else:
                txt += f"\npulse test running: {t.state}"
        self.g_status.setText(txt)
        self.b_ptest.setEnabled(self.ctl.running and (t is None or t.done))

    # --- agent hookup -------------------------------------------------------------------

    def attach_agent(self, server):
        self.agent = server
        self.agent_box.setVisible(True)
        self._activity_line(f"agent server: {server.url}")
        self._activity_line(f"connect with: {server.add_command()}")

    def _toggle_agent(self, on: bool):
        if self.agent:
            (self.agent.enabled.set if on else self.agent.enabled.clear)()
            self.ctl.notify(f"agent control {'enabled' if on else 'DISABLED'} by operator")

    def ask(self, text: str, choices: list[str]) -> concurrent.futures.Future:
        """Non-modal question from the agent. Main thread."""
        fut = concurrent.futures.Future()
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Agent request")
        box.setIcon(QtWidgets.QMessageBox.Question)
        box.setText(text)
        for c in choices:
            box.addButton(c, QtWidgets.QMessageBox.ActionRole)
        box.setWindowModality(QtCore.Qt.NonModal)

        def answered(btn):
            if not fut.done():
                fut.set_result(btn.text())
            self._asks.pop(fut, None)

        def dismissed(_):
            if not fut.done():
                fut.set_result("dismissed")
            self._asks.pop(fut, None)
        box.buttonClicked.connect(answered)
        box.finished.connect(dismissed)
        self._asks[fut] = box
        box.show()
        box.raise_()
        QtWidgets.QApplication.alert(self)
        self._activity_line(f"[agent asks] {text.replace(chr(10), ' ')}")
        return fut

    def close_ask(self, fut: concurrent.futures.Future):
        box = self._asks.pop(fut, None)
        if box:
            box.close()

    # --- operator actions ---------------------------------------------------------------

    def _toggle_run(self):
        try:
            if self.ctl.running:
                self._close_op_window()
                self.ctl.stop()
            else:
                QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
                try:
                    self.ctl.start()
                finally:
                    QtWidgets.QApplication.restoreOverrideCursor()
        except Exception as e:
            log.exception("start/stop failed")
            QtWidgets.QMessageBox.warning(self, "CurrentDongle", str(e))
        self._sync_controls()

    def _set_vdc(self, v: float):
        if abs(v - self.ctl.vdc) < 1e-6:
            return
        try:
            self.ctl.set_vdc(round(v, 3))
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "CurrentDongle", str(e))
            self.vdc.blockSignals(True)
            self.vdc.setValue(self.ctl.vdc)
            self.vdc.blockSignals(False)

    def _set_profile(self, *_):
        try:
            shunt = float(self.shunt.currentText().replace(",", "."))
            if (shunt, self.filt.currentText()) != self.ctl.profile:
                self.ctl.set_profile(shunt, self.filt.currentText())
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "CurrentDongle", str(e))
        self._sync_controls()

    def _toggle_measurement(self):
        if self.op_window is None:
            try:
                self.op_window = self.ctl.begin(self.meas_label.text().strip(), "operator")
            except Exception as e:
                QtWidgets.QMessageBox.warning(self, "CurrentDongle", str(e))
        else:
            self._close_op_window()
        self._sync_controls()

    def _close_op_window(self):
        if self.op_window is not None and self.ctl.session and self.op_window in self.ctl.session.windows:
            self.ctl.end(self.op_window)
        self.op_window = None

    def _sync_controls(self):
        running = self.ctl.running
        self.btn_run.setText("Stop" if running else "Start")
        self.btn_run.setStyleSheet("background:#a33; color:white; font-weight:bold" if running else
                                   "background:#2a6; color:white; font-weight:bold")
        self.shunt.setEnabled(not running)
        self.filt.setEnabled(not running)
        self.shunt.setCurrentText(f"{self.ctl.profile[0]:g}")
        self.filt.setCurrentText(self.ctl.profile[1])
        self.vdc.blockSignals(True)
        self.vdc.setValue(self.ctl.vdc)
        self.vdc.blockSignals(False)
        self.btn_meas.setEnabled(running)
        self.btn_meas.setText("End measurement" if self.op_window is not None else "Begin measurement")
        s = self.ctl.session
        self.dir_lbl.setText(str(s.dir) if s else "")
        cal = self.ctl.template.cal
        self.setWindowTitle(f"CurrentDongle – {cal.key} ({cal.source} cal) @ {self.ctl.vdc:g} V"
                            + (" – RUNNING" if running else ""))

    def _activity_line(self, text: str):
        self.activity.appendPlainText(f"{time.strftime('%H:%M:%S')}  {text}")

    # --- periodic update ------------------------------------------------------------------

    def tick(self):
        was_running = self.ctl.running
        self.ctl.pump(0.03)
        if was_running != self.ctl.running:  # stopped by error or agent
            self.op_window = None
            self._sync_controls()

        s = self.ctl.session
        if s is not None and s.running and not self.paused:
            self._redraw(s)
        now = time.monotonic()
        if now - self._t_labels > 0.25:
            self._t_labels = now
            self._labels()
            self._sync_controls()
            self._gen_status()
            results = self.ctl.results()
            if len(results) != self._n_results:
                self._n_results = len(results)
                self._fill_table(results)

        if self.duration and s is not None and s.proc and s.proc.duration >= self.duration:
            self.close()

    def _redraw(self, s):
        d = s.trace(self.window_s, PLOT_POINTS)
        t, mn, mean, mx = d["t_s"], d["min_A"], d["mean_A"], d["max_A"]
        if len(t) == 0:
            return
        ok = np.isfinite(mn) & np.isfinite(mx)
        te, mne, mxe = t[ok], mn[ok], mx[ok]
        if self.log_y.isChecked():
            mne, mxe = np.maximum(mne, LOG_FLOOR), np.maximum(mxe, LOG_FLOOR)
            mean = np.where(np.isfinite(mean), np.maximum(mean, LOG_FLOOR), np.nan)
        # envelope: one vertical segment per bin
        self.c_env.setData(np.repeat(te, 2), np.column_stack((mne, mxe)).ravel(), connect="pairs")
        self.c_mean.setData(t, mean, connect="finite")

    def _labels(self):
        s, L = self.ctl.session, self.lbl
        if s is None or s.proc is None:
            L["state"].setText("stopped")
            L["state"].setStyleSheet("")
            return
        p, d = s.proc, s.last_diag
        sm = p.summary()
        L["duration"].setText(f"{p.duration:.1f} s")
        L["mean"].setText(si(p.mean_current, "A"))
        L["charge"].setText(si(p.charge / 3600, "Ah"))
        w = s.trace(self.window_s, 1)
        L["window"].setText(si(float(w["mean_A"][0]), "A") if len(w["mean_A"]) else "--")
        L["minmax"].setText(f"{si(p.i_min, 'A')} / {si(p.i_max, 'A')}" if p.n else "--")
        if not s.connected:
            down = time.time() - (s.disconnected_since or time.time())
            L["state"].setText(f"DISCONNECTED {down:.0f} s – reconnecting")
            L["state"].setStyleSheet("color:#e33; font-weight:bold")
        else:
            L["state"].setText("streaming" + (f", measurement #{self.op_window}" if self.op_window else ""))
            L["state"].setStyleSheet("color:#3c3")
        L["lo"].setText(f"{sm['lo_channel_fraction']:.1%}")
        L["over"].setText(f"{sm['overrange_fraction']:.3%}")
        L["gaps"].setText(f"{p.gap_samples} samples ({p.gap_samples / p.rate * 1e3:.1f} ms)")
        L["corrupt"].setText(str(p.corrupt_samples))
        L["buffer"].setText(f"{d.get('buffer_fill_peak', 0):.0%} peak/s, backlog {s.backlog()}")
        L["poll"].setText(f"{d.get('max_poll_gap_ms', 0):.1f} ms max, {d.get('polls_per_s', 0):.0f}/s")
        L["dropped"].setText(str(d.get("total_dropped_blocks", 0)))
        L["reconnects"].setText(str(d.get("reconnects", 0)))
        L["usb"].setText(f"{d.get('usb_v', 0):.3f} V  {d.get('usb_a', 0) * 1e3:.0f} mA")
        L["temp"].setText(f"{d.get('temp_c', 0):.1f} °C")
        if s.error:
            L["valid"].setText("ERROR – see log")
        else:
            L["valid"].setText("VALID" if sm["valid"] else "NOT VALID")
        L["valid"].setStyleSheet(f"color: {'#3c3' if sm['valid'] and not s.error else '#e33'}; font-weight: bold")

    def _fill_table(self, results):
        self.table.setRowCount(len(results))
        for row, r in enumerate(results):
            vals = [r["id"], r["label"], r["source"], f"{r['duration_s']:.2f} s", si(r["mean_current_A"], "A"),
                    si(r["min_current_A"], "A"), si(r["max_current_A"], "A"), si(r["charge_mAh"] * 1e-3, "Ah"),
                    f"{r['lo_channel_fraction']:.1%}", "yes" if r["valid"] else "NO"]
            for col, v in enumerate(vals):
                item = QtWidgets.QTableWidgetItem(str(v))
                if col == 9 and not r["valid"]:
                    item.setForeground(QtGui.QColor("#e33"))
                self.table.setItem(row, col, item)
        self.table.scrollToBottom()

    def closeEvent(self, ev):
        self.timer.stop()
        for fut in list(self._asks):
            self.close_ask(fut)
        if self.ctl.running:
            self._close_op_window()
            summary = self.ctl.stop()
            print(f"session: {summary['session_dir']}\nvalid: {summary.get('valid')}")
        if self.agent:
            self.agent.stop()
        ev.accept()


def run_gui(ctl: Controller, autostart: bool = False, duration: float = 0, agent: dict | None = None) -> int:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w = MainWindow(ctl, duration)
    if agent is not None:
        from .agent import AgentServer
        server = AgentServer(ctl, w, **agent)
        server.start()
        w.attach_agent(server)
        print(f"agent: {server.add_command()}", flush=True)
    w.show()
    if autostart:
        w._toggle_run()
    app.exec()
    return 0
