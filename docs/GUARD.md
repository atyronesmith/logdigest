# Guard Module: Safety Pre-digestion and Tiered Classification

## Problem

LLM safety guards (Llama Guard, NeMo Guardrails) add 200-400ms per check. In multi-step pipelines, guard overhead compounds — a 3-step pipeline with full safety checks at every trust boundary can require 15-90+ guard checks per pipeline run.

Empirical testing revealed the problem is worse than the latency alone:

- **47.8% false positive rate** on legitimate software engineering outputs (Llama Guard 3-1B, 224 outputs)
- **95% rejection rate** on code generation steps specifically
- **Guard confidence is near-random** — mean entropy 0.94 bits (94% of maximum binary uncertainty), 0% of outputs above 0.90 confidence
- A 3-step pipeline has a **96.4% chance** of at least one step being blocked, requiring 21.4 expected calls (614% overhead)

The guard isn't making confident wrong decisions — it's making essentially random decisions that happen to land on "unsafe" roughly half the time on SE content.

## Solution: Two-Part Approach

### 1. Input Pre-digestion (`guard.predigest`)

Strips structural noise from text before feeding to a safety guard, reducing input tokens by 40-60% without affecting safety classification:

- Skips JSON structural-only lines (braces, brackets)
- Collapses blank line runs
- Handles code fence blocks: keeps first 5 lines for semantic content, summarizes rest as `[CODE: N lines]`
- Limits repetitive import statements (keeps first 3)

```python
from denoise.guard import predigest

cleaned = predigest(llm_output)
# Feed cleaned text to your guard model instead of raw output
```

### 2. Tiered Classification (`guard.classify`)

Three-tier safe/unsafe classification that eliminates most LLM guard calls:

**Tier 1 (keyword scan, <0.01ms):** Searches for unsafe vs. safe keyword patterns. Returns high confidence (0.95) if strong signal (5+ hits of one type, ≤1 of the other).

**Tier 2 (logistic regression, <0.01ms):** Bag-of-words + 3 structural features (code fence presence, JSON structure, `<think>` tags). Sparse dot product + softmax. Returns verdict if confidence ≥ threshold (default 0.90).

**Tier 3 (escalate to LLM guard):** When Tier 2 confidence is too low, returns `("escalate", confidence)` to trigger full LLM guard.

```python
from denoise.guard import classify, load_weights

load_weights("weights/guard_classifier_weights.json",
             "weights/guard_tier1_keywords.json")

verdict, confidence = classify(text)
# ("safe", 0.96)     → skip LLM guard
# ("unsafe", 0.93)   → skip LLM guard
# ("escalate", 0.72) → call LLM guard
```

### Empirical Results

Trained on 224 LLM outputs from drift's experiment corpus:

| Tier | Latency | CV Accuracy | Notes |
|------|---------|-------------|-------|
| Tier 1 (keywords) | <0.01ms | — | Catches obvious patterns |
| Tier 2 (logistic regression) | <0.01ms | 81.5% | 203 features (200 BoW + 3 structural) |
| Tier 3 (LLM guard) | ~290ms | ~50% effective | Near-random on SE content |

The classifier's 81.5% accuracy is not a limitation — the LLM guard itself achieves ~50% effective accuracy (coin-flip confidence). The classifier's value is that **it knows when it doesn't know**. When it reports 0.95 confidence, that confidence is calibrated. The LLM guard reports categorical assertions backed by coin-flip decisions.

### Category Overrides

The false positive problem is largely a category configuration issue:

| Configuration | Rejection rate | Pipeline overhead |
|--------------|---------------|-------------------|
| Default (all categories) | 47.8% | 614% (21.4 calls vs 3) |
| Exclude S6+S8 | 1.3% | 1% (3.04 calls vs 3) |

S6 (Specialized Advice) and S8 (Intellectual Property) trigger on all code review and code generation outputs. These categories make Llama Guard unsuitable for SE workloads without overrides.

## Training Weights

Weights are trained using drift's pipeline:

```bash
# In the drift repo:
python scripts/train_guard_classifier.py
# Outputs: results/guard_classifier/guard_classifier_weights.json
#          results/guard_classifier/guard_tier1_keywords.json
```

The classifier is exported as weight matrices — denoise needs only stdlib Python at runtime (no sklearn, no numpy). The compiled code does sparse dot product + softmax with numerical stability.

## What's Next

See [CONTINUOUS_CALIBRATION.md](CONTINUOUS_CALIBRATION.md) for the runtime monitoring loop that detects when classifier accuracy has drifted and triggers retraining.

For the full empirical analysis, see the drift repo's `docs/ai-safety-overhead.md`.
