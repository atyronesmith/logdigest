"""Runtime classifier drift detection via Jensen-Shannon divergence.

Monitors classifier confidence distributions at inference time and
detects when they diverge from training-time baselines. When drift
exceeds thresholds, recommends or triggers recalibration.

Usage:
    monitor = ClassifierMonitor(
        baseline={"histogram": [...], "n_bins": 20, ...},
        window_size=1000,
        warn_threshold=0.1,
        retrain_threshold=0.2,
    )

    # On every classification
    monitor.record(verdict="safe", confidence=0.94)

    # Periodic check
    status = monitor.check()
    # status.jsd, status.recommendation, status.drifted
"""

from __future__ import annotations

import json
import math
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DriftStatus:
    """Result of a drift check."""
    jsd: float
    recommendation: str  # "ok", "warn", "retrain"
    drifted: bool
    n_samples: int
    timestamp: float = field(default_factory=time.time)


class ClassifierMonitor:
    """Monitor classifier confidence distributions for drift.

    Args:
        baseline: Training-time baseline dict from drift's calibration.
            Must contain 'histogram' (normalized bin counts) and 'n_bins'.
        window_size: Minimum predictions before computing JSD.
        warn_threshold: JSD above this logs a warning.
        retrain_threshold: JSD above this triggers recalibration.
        auto_retrain: If True, triggers retraining automatically.
            If False, returns "retrain" recommendation for manual action.
        on_retrain: Optional callback invoked when retrain is triggered
            and auto_retrain is True. Called with (monitor, status).
    """

    def __init__(
        self,
        baseline: dict,
        window_size: int = 1000,
        warn_threshold: float = 0.1,
        retrain_threshold: float = 0.2,
        auto_retrain: bool = False,
        on_retrain=None,
    ):
        self.baseline_histogram = baseline["histogram"]
        self.n_bins = baseline.get("n_bins", len(self.baseline_histogram))
        self.baseline = baseline
        self.window_size = window_size
        self.warn_threshold = warn_threshold
        self.retrain_threshold = retrain_threshold
        self.auto_retrain = auto_retrain
        self.on_retrain = on_retrain

        self._buffer: deque[dict] = deque(maxlen=window_size * 2)
        self._last_status: DriftStatus | None = None

    def record(self, verdict: str, confidence: float, features: dict | None = None) -> None:
        """Record a classifier prediction.

        Args:
            verdict: The predicted class label.
            confidence: Top-class probability (0-1).
            features: Optional feature dict for export during retraining.
        """
        self._buffer.append({
            "verdict": verdict,
            "confidence": confidence,
            "features": features,
            "timestamp": time.time(),
        })

    def check(self) -> DriftStatus:
        """Check for distribution drift.

        Returns DriftStatus with recommendation. If fewer than window_size
        samples have been collected, returns "ok" with jsd=0.
        """
        n = len(self._buffer)
        if n < self.window_size:
            status = DriftStatus(
                jsd=0.0, recommendation="ok", drifted=False, n_samples=n,
            )
            self._last_status = status
            return status

        # Build runtime histogram from recent window_size predictions
        runtime_hist = self._build_histogram()
        jsd = compute_jsd(self.baseline_histogram, runtime_hist)

        if jsd >= self.retrain_threshold:
            recommendation = "retrain"
            drifted = True
        elif jsd >= self.warn_threshold:
            recommendation = "warn"
            drifted = True
        else:
            recommendation = "ok"
            drifted = False

        status = DriftStatus(
            jsd=round(jsd, 6), recommendation=recommendation,
            drifted=drifted, n_samples=n,
        )
        self._last_status = status

        if recommendation == "retrain" and self.auto_retrain and self.on_retrain:
            self.on_retrain(self, status)

        return status

    def export_samples(self, path: str | Path) -> int:
        """Export collected samples for retraining.

        Writes buffered predictions to a JSON file that drift's distill
        command can consume for recalibration.

        Returns:
            Number of samples exported.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        samples = list(self._buffer)
        path.write_text(json.dumps(samples, indent=2))
        return len(samples)

    def reset(self, new_baseline: dict | None = None) -> None:
        """Reset the monitor, optionally with a new baseline.

        Called after retraining to start fresh with updated weights.
        """
        self._buffer.clear()
        self._last_status = None
        if new_baseline is not None:
            self.baseline_histogram = new_baseline["histogram"]
            self.n_bins = new_baseline.get("n_bins", len(self.baseline_histogram))
            self.baseline = new_baseline

    @property
    def last_status(self) -> DriftStatus | None:
        return self._last_status

    def _build_histogram(self) -> list[float]:
        """Build normalized histogram from the most recent window_size entries."""
        recent = list(self._buffer)[-self.window_size:]
        histogram = [0] * self.n_bins
        for entry in recent:
            bin_idx = min(int(entry["confidence"] * self.n_bins), self.n_bins - 1)
            histogram[bin_idx] += 1
        total = len(recent)
        return [h / total for h in histogram] if total > 0 else histogram


def compute_jsd(p: list[float], q: list[float]) -> float:
    """Compute Jensen-Shannon divergence between two distributions.

    JSD is symmetric and bounded [0, 1] (using log base 2).
    """
    if len(p) != len(q):
        raise ValueError(f"Distribution lengths differ: {len(p)} vs {len(q)}")

    n = len(p)
    m = [(p[i] + q[i]) / 2 for i in range(n)]

    def _kl(a: list[float], b: list[float]) -> float:
        total = 0.0
        for i in range(n):
            if a[i] > 0 and b[i] > 0:
                total += a[i] * math.log2(a[i] / b[i])
        return total

    return (_kl(p, m) + _kl(q, m)) / 2
