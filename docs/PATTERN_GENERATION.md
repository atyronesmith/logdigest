# Automated Pattern Generation

## Status: Plan

The current NOISE_PATTERNS and SIGNAL_PATTERNS in each domain module are hand-authored by domain experts examining log samples. This document describes the pipeline to automate pattern discovery, version-aware routing, and live updates.

## Problem

Static regex lists have three weaknesses:

1. **Provenance** — no automated trail from corpus to patterns. Patterns appear hand-rolled to outside readers.
2. **Staleness** — log formats change across versions (Ansible 2.14 vs 2.16, RHEL 8 vs 9 systemd). Static patterns drift silently.
3. **Scope** — adding a new domain requires a human to read logs and write regex. Doesn't scale.

## Architecture

Three components, built incrementally:

```
┌─────────────────────────────────────────────────────────┐
│  Phase 1: Pattern Discovery                             │
│  corpus → entropy analysis → candidate regex → validate │
└──────────────────────┬──────────────────────────────────┘
                       │ patterns.json
┌──────────────────────▼──────────────────────────────────┐
│  Phase 2: Domain/Version Classifier                     │
│  log sample → domain + version → pattern set selection  │
└──────────────────────┬──────────────────────────────────┘
                       │ routed patterns
┌──────────────────────▼──────────────────────────────────┐
│  Phase 3: Live Drift Detection                          │
│  runtime stats → logprob monitoring → re-derive trigger │
└─────────────────────────────────────────────────────────┘
```

## Phase 1: Pattern Discovery Pipeline

**Goal:** Given a corpus of logs, automatically discover noise/signal patterns and emit a validated pattern set.

### Step 1: Corpus Entropy Analysis

Use `corpus_entropy()` to identify high-redundancy corpora worth processing. For corpora that pass the threshold, compute per-line entropy:

```python
from denoise.entropy import line_entropy

# Per-line frequency analysis across corpus
line_counts = Counter(line.strip() for doc in corpus for line in doc.splitlines())
```

Lines that appear in >5% of documents with low per-line entropy are noise candidates. Lines that appear rarely and contain known error tokens (fatal, error, failed, panic, traceback) are signal candidates.

### Step 2: Candidate Regex Generation

For each cluster of similar noise lines, generate a regex that captures the pattern:

1. **Exact match lines** — lines identical across documents become literal patterns
2. **Prefix clusters** — lines sharing a common prefix with variable suffixes (e.g., `ok: [hostname]`) become prefix regex (`^ok: \[`)
3. **Structural patterns** — lines matching a structural template (timestamp + level + message) get templated regex

The generation follows drift's `autogen._generate_regex()` approach: find the invariant prefix/structure, parameterize the variable parts, validate against the source corpus.

### Step 3: Validation

Each candidate pattern is tested against a held-out split of the corpus:

- **Precision**: does the pattern match only noise? (false positive = signal line matched as noise)
- **Recall**: what fraction of noise lines does it catch?
- **Safety check**: run pattern against known signal lines — any match disqualifies the candidate

Patterns that pass validation are emitted with a confidence score.

### Step 4: Export

Output is a JSON pattern set, not Python source:

```json
{
  "domain": "ansible",
  "version": "2.16",
  "generated": "2026-04-21T12:00:00Z",
  "corpus_size": 500,
  "noise_patterns": [
    {"regex": "^ok: \\[", "label": "ok", "confidence": 0.99, "corpus_frequency": 0.82},
    {"regex": "^skipping: \\[", "label": "skipping", "confidence": 0.98, "corpus_frequency": 0.71}
  ],
  "signal_patterns": [
    {"regex": "^fatal: \\[", "label": "fatal", "confidence": 0.99}
  ]
}
```

Domain modules load patterns from JSON at import time, falling back to the current compiled defaults if no pattern file is present.

### Implementation

New in drift:

```
drift discover <corpus_dir> --domain ansible -o patterns.json
```

Pipeline: `corpus_entropy` → line frequency analysis → prefix clustering → regex generation → validation → export.

New in denoise:

- `PatternSet` class in `domain_base.py` that loads from JSON or falls back to compiled defaults
- Each domain module switches from `NOISE_PATTERNS = [...]` to `NOISE_PATTERNS = PatternSet.load("ansible", version="default")`

## Phase 2: Domain/Version Classifier

**Goal:** Automatically detect what kind of log and what version, then select the right pattern set.

### Current State

`_detect_domain()` in `mcp.py` uses keyword scoring on the first 5000 chars. It detects domain (ansible, mustgather, cicd, journald) but not version.

### Extension

Two additions:

**Version detection** — extract version markers from log content:

| Domain | Version signal |
|--------|---------------|
| Ansible | `ansible-playbook [core 2.16.x]` in header, `ansible.builtin.` vs bare module names |
| OpenShift | `openshift-apiserver v4.14.x`, API resource versions |
| CI/CD | `Runner v2.x`, `Jenkins 2.x`, `GitLab Runner x.y` in preamble |
| systemd | `systemd[1]: systemd 252` or similar in early boot lines |

**Multi-domain support** — a single log stream may contain multiple domains (Ansible calling `oc logs`, CI/CD running Ansible playbooks). The classifier should return a ranked list of detected domains with byte ranges:

```python
from denoise import classify_log

result = classify_log(text)
# [
#   {"domain": "cicd", "version": "gha-2.x", "range": (0, 5000)},
#   {"domain": "ansible", "version": "2.16", "range": (5001, 45000)},
#   {"domain": "mustgather", "version": "ocp-4.14", "range": (45001, 80000)},
# ]
```

Each segment gets its version-appropriate pattern set. Unknown segments fall through to the general-purpose `core.py` cleaner.

### Pattern Set Storage

```
denoise/
  patterns/
    ansible/
      default.json      # hand-authored baseline
      2.14.json          # discovered from 2.14 corpus
      2.16.json          # discovered from 2.16 corpus
    mustgather/
      default.json
      ocp-4.14.json
    cicd/
      gha.json
      jenkins.json
      gitlab.json
    journald/
      default.json
      rhel-9.json
```

## Phase 3: Live Drift Detection and Re-derivation

**Goal:** Detect when log formats have changed enough that patterns are stale, and trigger re-derivation automatically.

### Runtime Monitoring

During pre-digestion, track classification statistics per session:

```python
@dataclass
class DigestStats:
    total_lines: int = 0
    noise_lines: int = 0
    signal_lines: int = 0
    unknown_lines: int = 0
    unknown_pct: float = 0.0
```

The key metric is `unknown_pct` — the fraction of lines that match neither noise nor signal patterns. This is the canary:

- **Baseline** (healthy patterns): unknown_pct ~5-15% (some lines are genuinely ambiguous)
- **Stale patterns**: unknown_pct rises as new log format lines go unclassified
- **Major version change**: unknown_pct spikes — many new patterns the classifier hasn't seen

### Logprob-Based Drift Detection

Extends drift's entropy probing to the pattern generation problem:

1. **Periodically** (configurable interval, e.g., every 1000 logs processed), sample a batch of recent logs
2. **Run `drift probe`** on the batch — get per-token logprobs for the LLM's summary of these logs
3. **Compare logprob distribution** against the baseline (from when patterns were last generated)
4. **If JSD exceeds threshold** → patterns are stale, new log content is surprising the model

This catches a subtler failure than unknown_pct alone: patterns might still match syntactically, but the semantic content they're stripping may have changed (e.g., Ansible 2.17 uses the same `ok: [host]` format but the task structure is different, causing the LLM to miss context it needs).

### Re-derivation Trigger

When drift is detected:

```
┌─────────────────┐
│ unknown_pct > T │──or──▶ Collect unclassified lines
│ JSD > threshold │        from recent buffer
└─────────────────┘               │
                                  ▼
                    ┌──────────────────────────┐
                    │ drift discover <buffer>  │
                    │   --domain <detected>    │
                    │   --base <current.json>  │
                    └──────────┬───────────────┘
                               │
                    ┌──────────▼───────────────┐
                    │ Merge: new patterns +    │
                    │ existing validated ones   │
                    └──────────┬───────────────┘
                               │
                    ┌──────────▼───────────────┐
                    │ Validate merged set      │
                    │ against recent corpus     │
                    └──────────┬───────────────┘
                               │
                    ┌──────────▼───────────────┐
                    │ Hot-reload patterns.json  │
                    │ (no restart needed)       │
                    └──────────────────────────┘
```

### Configuration

```python
from denoise import PatternManager

manager = PatternManager(
    patterns_dir="patterns/",
    unknown_threshold=0.25,       # trigger if >25% lines unclassified
    jsd_threshold=0.15,           # trigger if logprob distribution shifted
    buffer_size=1000,             # lines to collect before checking
    auto_rediscover=False,        # True = run drift discover automatically
    drift_probe_interval=1000,    # logs between logprob checks
)
```

### What denoise owns vs. what drift owns

| Concern | Owner |
|---------|-------|
| Runtime line classification stats | denoise |
| Unknown line buffering | denoise |
| Drift detection (unknown_pct threshold) | denoise |
| Pattern set loading and hot-reload | denoise |
| Domain/version classification | denoise |
| `drift discover` (corpus → patterns) | drift |
| `drift probe` (logprob analysis) | drift |
| JSD comparison against baseline | drift |
| Pattern validation against held-out corpus | drift |

## Implementation Order

1. **Phase 1a** — JSON pattern format + `PatternSet` loader in denoise (patterns still hand-authored, but externalized from Python source)
2. **Phase 1b** — `drift discover` command (corpus → pattern JSON)
3. **Phase 1c** — Re-derive current hand-authored patterns via the pipeline, proving the loop works
4. **Phase 2a** — Version detection in `classify_log()`
5. **Phase 2b** — Version-specific pattern sets (start with Ansible 2.14 vs 2.16)
6. **Phase 3a** — Runtime `DigestStats` + unknown_pct monitoring
7. **Phase 3b** — Logprob-based drift detection via `drift probe`
8. **Phase 3c** — Auto re-derivation trigger

Each phase is independently useful. Phase 1 alone answers the reviewer's provenance question. Phase 2 handles the multi-version/multi-domain concern. Phase 3 makes the system self-maintaining.
