# Continuous Calibration

## Workflow

1. **Bootstrap:** Run an initial corpus through drift's training pipeline (`drift distill`) to produce a first set of classifier weights. Export to denoise.
2. **Runtime collection:** During inference, denoise logs every classifier prediction (verdict, confidence, feature hash) to a rolling buffer.
3. **Drift detection:** Periodically compute Jensen-Shannon divergence between the training-time confidence distribution and the runtime buffer. If JSD exceeds threshold, the model needs updating.
4. **Recalibration:** Either retrain automatically or flag for review, controlled by a configuration flag.

## Architecture

### `src/denoise/monitor.py` (new)

Collects runtime prediction data and detects distribution drift.

```python
monitor = ClassifierMonitor(
    baseline_path="weights/baseline_distribution.json",  # from training
    window_size=1000,         # min predictions before computing JSD
    warn_threshold=0.1,       # log warning
    retrain_threshold=0.2,    # trigger recalibration
    auto_retrain=False,       # True = retrain without asking
)

# Called on every classification
monitor.record(verdict="safe", confidence=0.94, features={...})

# Periodic check (called by application or on schedule)
status = monitor.check()
# status.drifted: bool
# status.jsd: float
# status.recommendation: "ok" | "warn" | "retrain"
```

### Recalibration modes

- `auto_retrain=False` (default) — returns `"retrain"` recommendation, application decides what to do
- `auto_retrain=True` — triggers retraining pipeline automatically via callback

### Integration with drift

The retraining pipeline lives in drift. Denoise only detects drift and collects data. The handoff:

1. denoise detects JSD > threshold
2. denoise exports collected runtime samples to a directory
3. drift's `distill` command runs on the expanded corpus
4. New weights are written back to denoise's weights path
5. denoise hot-reloads weights on next classification call

### Thresholds

Starting points — these are empirically tuned per domain:

| Threshold | Value | Meaning |
|---|---|---|
| `warn_threshold` | 0.1 | Log drift, no action |
| `retrain_threshold` | 0.2 | Trigger recalibration |
| `window_size` | 1000 | Min samples before JSD is meaningful |

## What denoise owns vs. what drift owns

| Concern | Owner |
|---|---|
| Runtime prediction logging | denoise |
| JSD computation | denoise |
| Drift detection | denoise |
| Sample export for retraining | denoise |
| Classifier training / retraining | drift |
| Baseline distribution generation | drift |
| Weight serialization format | shared contract |

## Implementation Status

- `src/denoise/monitor.py` — `ClassifierMonitor` with `record()`, `check()`, `export_samples()`, `reset()`
- `tests/test_monitor.py` — 15 tests covering JSD, thresholds, callbacks, export, reset
- Drift side: `src/drift/hybrid/calibration.py` computes baselines; `GeneratedRule.baseline_distribution` carries them through training
