"""Tests for classifier drift monitor."""

import json
import pytest

from denoise.monitor import ClassifierMonitor, DriftStatus, compute_jsd


def _make_baseline(n_bins=20, peak_bin=18):
    """Create a baseline with most confidence mass in a high bin."""
    histogram = [0.0] * n_bins
    histogram[peak_bin] = 0.8
    histogram[peak_bin - 1] = 0.15
    histogram[peak_bin - 2] = 0.05
    return {
        "histogram": histogram,
        "n_bins": n_bins,
        "n_samples": 1000,
        "class_distribution": {"safe": 800, "unsafe": 200},
        "mean_confidence": 0.92,
        "classes": ["safe", "unsafe"],
    }


def test_monitor_initial_state():
    baseline = _make_baseline()
    monitor = ClassifierMonitor(baseline, window_size=100)
    status = monitor.check()
    assert status.recommendation == "ok"
    assert status.jsd == 0.0
    assert status.n_samples == 0
    assert not status.drifted


def test_monitor_below_window_returns_ok():
    baseline = _make_baseline()
    monitor = ClassifierMonitor(baseline, window_size=100)
    for i in range(50):
        monitor.record("safe", 0.95)
    status = monitor.check()
    assert status.recommendation == "ok"
    assert status.n_samples == 50


def test_monitor_no_drift_with_similar_distribution():
    baseline = _make_baseline()
    monitor = ClassifierMonitor(baseline, window_size=100)
    # Feed predictions matching the baseline distribution:
    # 80% in bin 18 (0.90-0.95), 15% in bin 17 (0.85-0.90), 5% in bin 16 (0.80-0.85)
    for _ in range(80):
        monitor.record("safe", 0.92)  # bin 18
    for _ in range(15):
        monitor.record("safe", 0.87)  # bin 17
    for _ in range(5):
        monitor.record("safe", 0.82)  # bin 16
    status = monitor.check()
    assert status.recommendation == "ok"
    assert status.jsd < 0.1


def test_monitor_detects_drift_warn():
    baseline = _make_baseline()
    monitor = ClassifierMonitor(
        baseline, window_size=100,
        warn_threshold=0.1, retrain_threshold=0.3,
    )
    # Feed predictions with medium confidence (shifted distribution)
    for _ in range(100):
        monitor.record("safe", 0.55)
    status = monitor.check()
    assert status.drifted
    assert status.jsd >= 0.1
    assert status.recommendation in ("warn", "retrain")


def test_monitor_detects_drift_retrain():
    baseline = _make_baseline()
    monitor = ClassifierMonitor(
        baseline, window_size=100,
        warn_threshold=0.05, retrain_threshold=0.1,
    )
    # Feed predictions with very low confidence (strong drift)
    for _ in range(100):
        monitor.record("unsafe", 0.15)
    status = monitor.check()
    assert status.drifted
    assert status.recommendation == "retrain"
    assert status.jsd >= 0.1


def test_monitor_auto_retrain_callback():
    baseline = _make_baseline()
    callback_log = []

    def on_retrain(monitor, status):
        callback_log.append(status)

    monitor = ClassifierMonitor(
        baseline, window_size=50,
        warn_threshold=0.05, retrain_threshold=0.1,
        auto_retrain=True, on_retrain=on_retrain,
    )
    for _ in range(50):
        monitor.record("unsafe", 0.15)
    monitor.check()
    assert len(callback_log) == 1
    assert callback_log[0].recommendation == "retrain"


def test_monitor_no_callback_without_auto_retrain():
    baseline = _make_baseline()
    callback_log = []

    def on_retrain(monitor, status):
        callback_log.append(status)

    monitor = ClassifierMonitor(
        baseline, window_size=50,
        warn_threshold=0.05, retrain_threshold=0.1,
        auto_retrain=False, on_retrain=on_retrain,
    )
    for _ in range(50):
        monitor.record("unsafe", 0.15)
    monitor.check()
    assert len(callback_log) == 0


def test_monitor_export_samples(tmp_path):
    baseline = _make_baseline()
    monitor = ClassifierMonitor(baseline, window_size=10)
    for i in range(5):
        monitor.record("safe", 0.9 + i * 0.01, features={"input_hash": f"h{i}"})

    path = tmp_path / "samples.json"
    count = monitor.export_samples(path)
    assert count == 5

    samples = json.loads(path.read_text())
    assert len(samples) == 5
    assert samples[0]["verdict"] == "safe"
    assert samples[0]["features"]["input_hash"] == "h0"


def test_monitor_reset():
    baseline = _make_baseline()
    monitor = ClassifierMonitor(baseline, window_size=10)
    for _ in range(10):
        monitor.record("safe", 0.95)
    monitor.check()
    assert monitor.last_status is not None

    monitor.reset()
    assert monitor.last_status is None
    status = monitor.check()
    assert status.n_samples == 0


def test_monitor_reset_with_new_baseline():
    baseline = _make_baseline(peak_bin=18)
    monitor = ClassifierMonitor(baseline, window_size=10)

    new_baseline = _make_baseline(peak_bin=10)
    monitor.reset(new_baseline=new_baseline)
    assert monitor.baseline_histogram == new_baseline["histogram"]


def test_monitor_last_status():
    baseline = _make_baseline()
    monitor = ClassifierMonitor(baseline, window_size=10)
    assert monitor.last_status is None
    monitor.check()
    assert monitor.last_status is not None
    assert isinstance(monitor.last_status, DriftStatus)


def test_compute_jsd_identical():
    p = [0.2, 0.3, 0.5]
    assert compute_jsd(p, p) == 0.0


def test_compute_jsd_maximally_different():
    p = [1.0, 0.0]
    q = [0.0, 1.0]
    assert compute_jsd(p, q) == 1.0


def test_compute_jsd_symmetric():
    p = [0.3, 0.7]
    q = [0.6, 0.4]
    assert compute_jsd(p, q) == compute_jsd(q, p)


def test_compute_jsd_bounded():
    p = [0.1, 0.2, 0.3, 0.4]
    q = [0.4, 0.3, 0.2, 0.1]
    jsd = compute_jsd(p, q)
    assert 0 <= jsd <= 1.0
