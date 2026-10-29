import numpy as np
import pytest

from currentdongle.processing import (Block, Calibration, ChannelCal, Processor, fit_channel, minmax_decimate,
                                      parse_current)

RATE = 100e3
R = 0.5
V_SAT = 3.15


def volts(i):
    """Ideal channel outputs for current i [A], A5 clipped at 3.28 V like the real INA."""
    i = np.asarray(i, float)
    return np.minimum(i * R * 500, 3.28), np.minimum(i * R * 50, 3.28)


def blocks(i, size):
    hi, lo = volts(i)
    for s in range(0, len(i), size):
        yield Block(s, hi[s:s + size], lo[s:s + size])


def proc(holdoff_s=0.5e-3):
    return Processor(Calibration.nominal(R, "2k"), RATE, V_SAT, holdoff_s)


def run(p, i_or_volts, size):
    """Feed blocks, return the concatenated emitted samples (the last hold-off is still held)."""
    hi, lo = i_or_volts if isinstance(i_or_volts, tuple) else volts(i_or_volts)
    res = [r for s in range(0, len(hi), size) for r in p.process(Block(s, hi[s:s + size], lo[s:s + size]))]
    return np.concatenate([r.current for r in res]), np.concatenate([r.use_lo for r in res])


def rc(x, tau):
    """One-pole low-pass at RATE (output RC filter); keeps the area of x."""
    a = 1 - np.exp(-1 / (tau * RATE))
    y, acc = np.empty_like(x), 0.0
    for k, v in enumerate(x):
        acc += a * (v - acc)
        y[k] = acc
    return y


HOLD = int(0.5e-3 * RATE)


def test_constant_current_charge():
    p = proc()
    i = np.full(int(RATE), 1e-3)  # 1 mA for 1 s
    current, _ = run(p, i, 997)
    assert len(current) == len(i) - HOLD
    assert p.charge == pytest.approx(1e-3 * len(current) / RATE, rel=1e-9)
    assert p.mean_current == pytest.approx(1e-3)
    assert p.n_lo == 0
    assert p.summary()["valid"]


def test_pulse_uses_low_channel_with_holdoff_on_both_sides_across_blocks():
    p = proc()
    i = np.full(10_000, 1e-3)
    i[2000:2100] = 50e-3  # 1 ms pulse, saturates A5
    current, use_lo = run(p, i, 2050)  # block edge inside the pulse
    assert not use_lo[:2000 - HOLD].any()
    assert use_lo[2000 - HOLD:2100 + HOLD].all()
    assert not use_lo[2100 + HOLD:].any()
    np.testing.assert_allclose(current, i[:len(current)])
    assert p.charge == pytest.approx(i[:len(current)].sum() / RATE)


@pytest.mark.parametrize("width, period, tau", [(100e-6, 10e-3, 72.6e-6), (10e-6, 10e-3, 6.6e-6),
                                                (10e-6, 1e-3, 72.6e-6), (10e-6, 100e-6, 72.6e-6),
                                                (1e-3, 10e-3, 72.6e-6)])
def test_short_pulse_through_output_filter(width, period, tau):
    """Bench case (M6): A5 clips before the RC filter, so its filtered output stays below
    v_sat during short pulses. Switching where A5 reads below A2, with look-back, keeps the charge."""
    i = np.full(20_000, 1e-3)
    n, per = int(round(width * RATE)), int(round(period * RATE))
    for s in range(5000, 15_000, per):
        i[s:s + n] = 48e-3
    hi = rc(np.minimum(i * R * 500, 3.28), tau)
    lo = rc(i * R * 50, tau)
    assert hi.max() < V_SAT or width > 500e-6  # the old rule would not have switched
    p = proc()
    current, use_lo = run(p, (hi, lo), 1000)
    q_true = i[:len(current)].sum() / RATE
    assert p.charge == pytest.approx(q_true, rel=2e-3)
    assert use_lo[5000 - 20:5000 + n].all()


def test_a5_only_would_underread():
    i = np.full(10_000, 1e-3)
    i[2000:2100] = 50e-3
    hi, _ = volts(i)
    q_a5_only = (hi / (500 * R)).sum() / RATE
    # pulse clipped at 3.28 V / 250 = 13.1 mA instead of 50 mA -> ~25 % of the charge missing
    assert q_a5_only < 0.8 * i.sum() / RATE


def test_gap_is_counted_and_forces_low_channel():
    p = proc()
    hi, lo = volts(np.full(3000, 1e-3))
    first = p.process(Block(0, hi[:1000], lo[:1000]))
    assert len(first) == 1 and len(first[0].current) == 1000 - HOLD
    held, after = p.process(Block(1500, hi[1500:], lo[1500:]))
    assert held.start == 1000 - HOLD and held.gap == 0 and held.use_lo.all()  # before the gap
    assert after.start == 1500 and after.gap == 500
    assert p.gap_samples == 500
    assert after.use_lo[:HOLD].all() and not after.use_lo[HOLD:].any()
    assert not p.summary()["valid"]


def test_overrange_flag():
    p = proc()
    run(p, np.full(100 + HOLD, 0.2), 1000)  # 200 mA > A2 full scale
    assert p.n_over == 100
    assert not p.summary()["valid"]


def test_calibration_applied():
    cal = Calibration(R, "2k", ChannelCal(505.0, 0.004), ChannelCal(49.5, -0.001))
    p = Processor(cal, RATE, V_SAT, 0.0)
    i = 2e-3
    (r,) = p.process(Block(0, np.array([i * R * 505 + 0.004]), np.array([i * R * 49.5 - 0.001])))
    assert r.current[0] == pytest.approx(i)


def test_fit_channel():
    i_ref = np.array([0.2e-3, 1e-3, 8e-3])
    v = i_ref * R * 497.0 + 0.0021
    cal, resid = fit_channel(i_ref, v, R)
    assert cal.gain == pytest.approx(497.0)
    assert cal.offset == pytest.approx(0.0021)
    assert np.abs(resid).max() < 1e-12


def test_minmax_decimate():
    mn, mean, mx = minmax_decimate(np.arange(10.0), 4)
    np.testing.assert_array_equal(mn, [0, 4])
    np.testing.assert_array_equal(mx, [3, 7])
    np.testing.assert_array_equal(mean, [1.5, 5.5])


@pytest.mark.parametrize("text, amps", [
    ("12.5u", 12.5e-6), ("3.3m", 3.3e-3), ("0", 0.0), ("1.2uA", 1.2e-6), ("2e-3", 2e-3),
    ("v=3.3 r=330k", 3.3 / 330e3 + 3.3 / 10e6),
    ("v=3.3 r=3.3M", 3.3 / 3.3e6 + 3.3 / 10e6),
    ("v=3.3 r=3.3M rdmm=1G", 3.3 / 3.3e6 + 3.3 / 1e9),
    ("v=3.3 r=10M rdmm=inf", 3.3 / 10e6),
])
def test_parse_current(text, amps):
    assert parse_current(text) == pytest.approx(amps)
