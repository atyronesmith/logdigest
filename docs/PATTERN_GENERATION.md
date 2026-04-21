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

Three distinct entropy operations, each answering a different question:

**Operation 1: Corpus-level line-frequency entropy.** Treat each unique stripped line as a symbol. Compute Shannon entropy of the frequency distribution across the entire corpus. Low entropy means a few line patterns dominate the corpus (e.g., `ok: [host]` appears 40,000 times across 500 logs). This is the pre-flight gate — if line-frequency entropy is high (many unique lines, flat distribution), the corpus has little redundancy and pattern discovery won't help. This is what `line_entropy()` in `entropy.py` already computes.

```python
# Corpus-level: how concentrated is the line distribution?
line_counts = Counter(line.strip() for doc in corpus for line in doc.splitlines())
# Shannon entropy of this distribution → low = dominated by few patterns = good candidate
```

**Operation 2: Prefix-cluster compactness.** Group lines by shared prefix (longest common prefix ≥ 8 chars). For each cluster, measure within-cluster variance on the suffix. Tight clusters (low suffix variance, high member count) indicate an invariant template with a variable tail — exactly what becomes a regex. This is the operation that discovers patterns.

```python
# Prefix clustering: group "ok: [host1]", "ok: [host2]", ... → prefix "ok: ["
# Low suffix variance + high frequency → noise candidate
# High suffix variance or low frequency → not a template, skip
```

**Operation 3: Character-level entropy per line.** Shannon entropy of the character distribution within a single line. Only useful as a filter for two edge cases: separator lines (`==========`, entropy ≈ 0) and base64/hex blobs (entropy ≈ 5.5+). Not a primary signal for pattern discovery.

Lines are classified as noise candidates when they belong to high-frequency, low-variance prefix clusters (Operation 2) within a corpus that has low line-frequency entropy (Operation 1). Lines are classified as signal candidates when they appear rarely and contain known error tokens (fatal, error, failed, panic, traceback).

### Step 2: Candidate Regex Generation

For each prefix cluster identified in Step 1, generate a regex:

1. **Exact match clusters** — all members identical → literal pattern (e.g., `^TASKS RECAP$`)
2. **Prefix-variable clusters** — shared prefix with variable suffix → prefix regex (e.g., `^ok: \[` captures `ok: [host1]`, `ok: [host2]`, ...)
3. **Structural template clusters** — members share a structural template (e.g., timestamp + level + message) but vary in multiple positions → templated regex with capture groups for the invariant structure

The generation follows drift's `autogen._generate_regex()` approach: extract the invariant prefix/structure from the cluster, parameterize variable positions, validate the generated regex against all cluster members and against the full corpus.

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

Extends drift's entropy probing to the pattern generation problem. The specific operation:

1. **At pattern generation time**, run `drift probe` on a sample of pre-digested logs. For each log, the LLM generates a summary; `drift probe` records per-token logprobs. Aggregate into a baseline distribution: the histogram of mean-token-entropy values across the sample. This captures how "surprised" the LLM is by pre-digested content when patterns are fresh.

2. **At runtime** (configurable interval, e.g., every 1000 logs), sample a batch of recently pre-digested logs and run the same probe. Compute the same mean-token-entropy histogram.

3. **Compare the two histograms via JSD.** Jensen-Shannon divergence measures how much the runtime entropy distribution has shifted from baseline. High JSD means the LLM is seeing systematically different content after pre-digestion than it saw when patterns were generated.

4. **If JSD exceeds threshold** → the pre-digested output has changed character, even if classification rates look stable. Trigger re-derivation.

This catches a failure mode that `unknown_pct` misses: patterns still match syntactically, but the semantic content they're stripping has changed. For example, Ansible 2.17 might use the same `ok: [host]` format but restructure task output so that stripping `ok:` lines now removes context the LLM needs. The LLM's rising surprise (higher token entropy on the pre-digested output) surfaces this before downstream quality degrades.

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
