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

**Operation 2: Log template mining via Drain3.** Line-level template discovery is a solved problem. [Drain3](https://github.com/logpai/Drain3) (logpai) is the standard: it parses log lines into a fixed-depth tree by token position, clusters lines into templates, and extracts the invariant structure with `<*>` wildcards for variable parts. It's streaming, unsupervised, and production-deployed.

We use Drain3 as the Level 1 clustering engine rather than building our own prefix-clustering. Drain3 produces templates like `ok: [<*>]` and `FAILED - RETRYING: [<*>]: <*> (<*> retries left)`. These templates become the noise/signal pattern candidates — high-frequency templates are noise candidates, low-frequency templates containing error tokens are signal candidates.

```python
from drain3 import TemplateMiner

miner = TemplateMiner()
for doc in corpus:
    for line in doc.splitlines():
        miner.add_log_message(line.strip())

# High-frequency templates → noise candidates
# Low-frequency templates with error tokens → signal candidates
for cluster in miner.drain.clusters:
    template = cluster.get_template()
    frequency = cluster.size / total_lines
```

**What Drain3 doesn't do — and where logdigest adds value:**

- **Block patterns (Level 2):** Drain3 operates on individual lines. It cannot detect that 47 consecutive `FAILED - RETRYING:` lines form a retry block that should collapse into `[RETRY: task, 47 retries, then succeeded]`. Block detection requires sequence-level analysis across lines.
- **Blob patterns (Level 3):** Drain3 doesn't identify multi-kilobyte embedded structures (K8s API responses, base64 blobs, managedFields YAML) that should be truncated to one-line summaries.
- **Noise/signal classification:** Drain3 discovers templates but doesn't classify them. The classification step (is this template noise or signal?) uses corpus-level line-frequency entropy from Operation 1 plus error-token heuristics.
- **Domain-specific block/blob detectors:** Retry loop collapsing, item loop dedup, health-check block summarization, JSON blob truncation — these are logdigest's contribution on top of Drain3's line-level templates.

**Operation 3: Character-level entropy per line.** Shannon entropy of the character distribution within a single line. Only useful as a filter for two edge cases: separator lines (`==========`, entropy near 0) and base64/hex blobs (entropy near 5.5+). Not a primary signal for pattern discovery.

### Step 2: Template-to-Regex Conversion

Drain3 emits templates with `<*>` wildcards (e.g., `ok: [<*>]`). Convert these to compiled regex:

1. **Exact templates** (no wildcards) → literal pattern (e.g., `^TASKS RECAP$`)
2. **Prefix-wildcard templates** → prefix regex (e.g., `ok: [<*>]` → `^ok: \[`)
3. **Multi-wildcard templates** → structural regex with `.*?` or `\S+` at wildcard positions, with proper escaping and anchoring

The template-to-regex conversion is inspired by drift's `autogen._generate_regex()`, but that code targets JSON field extraction from LLM outputs — different input structure, different assumptions about what's variable vs. invariant. `drift discover` is a new pipeline, not a refactor of autogen.

Each generated regex is validated against all members of the Drain3 cluster it came from, plus cross-checked against signal clusters to prevent false matches.

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

Domain modules load patterns from JSON at import time. A missing pattern file raises `RuntimeError`.

### Schema Validation

The `PatternSet` loader validates every pattern file against a JSON Schema on load, before attempting regex compilation. A malformed file (missing `label`, invalid regex syntax, wrong type for `confidence`) produces a clear validation error at load time, not a `re.error` deep in `classify_line()`.

Schema ships as `src/logdigest/schemas/pattern_set.schema.json` (see that file for the full definition). Key structure:

```json
{
  "required": ["domain", "generated", "noise_patterns", "signal_patterns"],
  "properties": {
    "domain": "string",
    "generated": "date-time",
    "noise_patterns": [{ "regex": "string", "label": "string", ... }],
    "signal_patterns": [{ "regex": "string", "label": "string", ... }],
    "config": {
      "error_context": "integer",
      "max_line_len": "integer",
      "file_streaming_threshold_mb": "integer",
      "blob_markers": ["string"],
      "block_detectors": [{ "name", "regex", "min_count", "summary_template" }],
      "domain_keywords": ["string"]
    },
    "baseline_entropy": "object",
    "conflicts": [{ "pattern", "existing_class", "proposed_class", "action" }]
  }
}
```

Labels match `^[a-z][a-z0-9_]*$`. Pattern entries may also include `confidence`, `corpus_frequency`, `stale`, and `stale_cycles` fields for automated pattern generation.

The loader additionally validates that every `regex` field compiles successfully via `re.compile()` after schema validation passes. Schema validation uses stdlib `json` for parsing; the `jsonschema` package is an optional dependency (`pip install logdigest[dev]`). If `jsonschema` is not installed, the loader skips schema validation and falls back to regex-compile-time error checking only, with a warning.

### Implementation

New in drift:

```
drift discover <corpus_dir> --domain ansible -o patterns.json
```

Pipeline: `corpus_entropy` → line frequency analysis → prefix clustering → regex generation → validation → export.

New in logdigest:

- `PatternSet` class in `domain_base.py` that loads from JSON or falls back to compiled defaults
- Each domain module switches from `NOISE_PATTERNS = [...]` to `NOISE_PATTERNS = PatternSet.load("ansible")`

**Version resolution in `PatternSet.load(domain, version=None)`:**

| `version` argument | Behavior |
|---------------------|----------|
| `None` (default) | Load the hand-authored baseline (`ansible.json`). This is the current compiled pattern list, exported to JSON — known-good, manually validated. |
| `"latest"` | Load the most recently generated pattern file for this domain (by `generated` timestamp in the JSON). Falls back to baseline if no generated files exist. |
| `"2.16"` (explicit) | Load the named version file. Error if not found — no silent fallback when a specific version is requested. |

The baseline file is always the hand-authored patterns. Generated files from `drift discover` are stored alongside it but never replace it. `None` (baseline) is the safe default for any deployment that hasn't run the discovery pipeline.

## Phase 2: Domain/Version Classifier

**Goal:** Automatically detect what kind of log and what version, then select the right pattern set.

### Current State

`_detect_domain()` in `mcp.py` uses keyword scoring on the first 5000 chars. It detects domain (ansible, mustgather, cicd, journald) but not version.

### Extension

Two additions:

**Version detection — but only at the granularity that matters.** Log output format is typically stable within major versions and shifts at major boundaries. Before creating per-minor-version pattern files, run an empirical check: do the existing NOISE_PATTERNS produce different `unknown_pct` on Ansible 2.14 vs 2.16 logs? On OCP 4.14 vs 4.16 must-gather output? If `unknown_pct` is stable across minor versions (expected), one pattern file per major version is sufficient. Only split when observed divergence warrants it.

Version markers to extract (for metadata, not necessarily for pattern routing):

| Domain | Version signal |
|--------|---------------|
| Ansible | `ansible-playbook [core 2.x.y]` in header |
| OpenShift | `openshift-apiserver v4.x.y`, API resource versions |
| CI/CD | `Runner v2.x`, `Jenkins 2.x`, `GitLab Runner x.y` in preamble |
| systemd | `systemd[1]: systemd 252` or similar in early boot lines |

**Multi-domain support** — a single log stream often contains multiple domains. CI/CD output that invokes Ansible that calls kubectl is the common case, not an edge case. Domain boundaries are not clean: a single line can be GHA framing (`##[group]`) wrapping Ansible output that includes an embedded K8s API response.

Byte-range segmentation (assigning each range to one domain) is the wrong abstraction for this. Instead, apply all detected domain pattern sets to every line, with per-line priority ordering:

```python
from logdigest import classify_log

result = classify_log(text)
# {
#   "domains_detected": ["cicd", "ansible"],
#   "primary": "ansible",        # highest pattern match rate
#   "pattern_sets_applied": ["cicd", "ansible"],  # all sets run on every line
# }
```

Each line is tested against all detected domain pattern sets. If multiple patterns match, the most specific wins (e.g., an Ansible `ok:` pattern takes priority over a generic CI/CD "command output" pattern). Lines that match no domain pattern fall through to `core.py`.

This needs prototyping on real multi-domain logs (CI-running-Ansible is the test case) before committing to a final interface. The current single-domain `_detect_domain()` + dispatch in `mcp.py` works for clean single-domain inputs and remains the default path.

### Pattern Set Storage

Start with one pattern file per domain. Only create version-specific files when empirical divergence is observed (unknown_pct difference > 5% between versions on the same domain).

```
logdigest/
  patterns/
    ansible.json        # covers all 2.x — split only if versions diverge
    mustgather.json     # covers all OCP 4.x
    cicd/
      gha.json          # CI systems differ structurally, not by version
      jenkins.json
      gitlab.json
    journald.json       # covers all systemd versions
```

## Phase 3: Live Drift Detection and Re-derivation

**Goal:** Detect when log formats have changed enough that patterns are stale, and trigger re-derivation automatically.

### Continuous Monitoring: unknown_pct (free)

During pre-digestion, track classification statistics. This costs nothing — it's a counter increment on every `classify_line()` call that's already happening.

```python
@dataclass
class DigestStats:
    total_lines: int = 0
    noise_lines: int = 0
    signal_lines: int = 0
    unknown_lines: int = 0
    unknown_pct: float = 0.0
```

`unknown_pct` — the fraction of lines matching neither noise nor signal patterns — is the primary drift signal:

- **Healthy patterns**: unknown_pct ~5-15% (some lines are genuinely ambiguous)
- **Stale patterns**: unknown_pct rises as new log format lines go unclassified
- **Major version change**: unknown_pct spikes — many new patterns the classifier hasn't seen

Denoise buffers unclassified lines in a rolling window. When `unknown_pct` crosses the threshold (default 0.25), it triggers the escalation path.

### Escalation: Logprob Confirmation (expensive, on-demand only)

`unknown_pct` catches syntactic drift — new line formats the patterns don't match. But it misses semantic drift: patterns still match, but the content they're stripping has changed in ways that hurt downstream quality. For example, Ansible 2.17 might use the same `ok: [host]` format but restructure task output so that stripping `ok:` lines now removes context the LLM needs.

Logprob probing detects this — but it requires running an LLM over sampled logs and capturing per-token logprobs, which has real inference cost. Running it continuously as parallel monitoring is not worth the overhead. Instead, it runs only as a confirmation step after `unknown_pct` triggers.

**The escalation flow:**

```
unknown_pct crosses threshold
        │
        ▼
Collect buffered unclassified lines
        │
        ▼
Run drift probe on N sampled pre-digested logs (N=20-50)
        │
        ▼
Compare against baseline → confirm or dismiss
```

**What the baseline is, specifically:** At pattern generation time (`drift discover`), run `drift probe` on 50 pre-digested logs from the training corpus. For each log, the LLM generates a structured summary (the same task the patterns are optimizing for). `drift probe` records per-token logprobs on the LLM's output. Compute the mean token entropy per log (average of `-log2(p)` across all output tokens). The baseline is the distribution of these 50 mean-entropy values — stored as a histogram in the pattern set's JSON alongside the patterns themselves.

**What the confirmation probe measures:** Run the same `drift probe` on 20-50 recently pre-digested logs. Compute the same per-log mean token entropy. Compare the two distributions via JSD.

- **Low JSD** (< 0.10): the LLM is equally "surprised" by current pre-digested content as it was at training time. The `unknown_pct` spike is probably benign — new log lines that are genuinely ambiguous but not hurting quality. Buffer them for the next scheduled re-derivation but don't trigger an urgent one.
- **High JSD** (> 0.15): the LLM is systematically more surprised. Pre-digestion is stripping content the LLM now needs, or passing through noise it shouldn't. Trigger re-derivation.

This keeps continuous monitoring free (counters only) and pays the LLM inference cost only when there's already evidence of a problem.

### Re-derivation

When triggered (unknown_pct threshold crossed + logprob confirmation):

```
┌──────────────────────────────┐
│ Collect unclassified lines   │
│ from rolling buffer          │
└──────────┬───────────────────┘
           ▼
┌──────────────────────────────┐
│ drift discover <buffer>      │
│   --domain <detected>        │
│   --base <current.json>      │
└──────────┬───────────────────┘
           ▼
┌──────────────────────────────┐
│ Merge with conflict          │
│ resolution (see below)       │
└──────────┬───────────────────┘
           ▼
┌──────────────────────────────┐
│ Validate merged set against  │
│ recent corpus (held-out)     │
└──────────┬───────────────────┘
           ▼
┌──────────────────────────────┐
│ Hot-reload patterns.json     │
│ Reset unknown_pct baseline   │
│ Store new logprob baseline   │
└──────────────────────────────┘
```

### Pattern Merge Conflict Resolution

The dangerous scenario: Ansible 2.17 ships, the pipeline auto-re-derives, and a line pattern that was classified as signal (e.g., a specific fatal-error format) now appears in 6% of the new corpus and gets reclassified as noise. Silent replacement would strip errors from pre-digested output.

**Core rule: signal always wins.** A pattern classified as signal in the existing set cannot be reclassified as noise by re-derivation. This is asymmetric by design — false noise (stripping a real error) is catastrophic, false signal (keeping a noise line) is merely wasteful.

**Conflict categories and resolution:**

| Existing | New | Resolution |
|----------|-----|------------|
| signal | signal | Keep. Update confidence/frequency if changed. |
| signal | noise | **Block.** Flag for manual review. Never auto-demote. |
| signal | absent | Keep. Pattern may not appear in new corpus but is still valid. |
| noise | noise | Keep. Update confidence/frequency. |
| noise | signal | **Promote.** Auto-accept — promoting noise to signal is safe. |
| noise | absent | Keep, but mark `stale: true`. Review on next cycle. |
| absent | noise | Add as noise candidate, subject to validation (Step 3). |
| absent | signal | Add as signal. |

**The signal→noise block in detail:**

When re-derivation wants to reclassify a signal pattern as noise, this goes into a conflict report rather than the merged pattern set:

```json
{
  "conflicts": [
    {
      "pattern": "^fatal: \\[",
      "existing_class": "signal",
      "proposed_class": "noise",
      "reason": "appeared in 6.2% of new corpus (threshold 5%)",
      "existing_confidence": 0.99,
      "new_confidence": 0.87,
      "action": "blocked",
      "requires": "manual_review"
    }
  ]
}
```

The operator reviews the conflict report. The pattern stays signal until explicitly demoted. This is the one place the pipeline is deliberately not fully automated — the cost of a wrong demotion (stripping errors) outweighs the cost of a human review.

**Why not confidence-based tie-breaking:** Confidence scores are not comparable across derivation runs. A pattern trained on 500 2.16 logs and a pattern trained on 200 2.17 logs have different confidence scales. Comparing 0.99 vs 0.87 across runs is meaningless. The asymmetric rule (signal wins) avoids the need for cross-run confidence comparison entirely.

**Stale pattern handling:** Noise patterns marked `stale: true` (present in existing set, absent in new corpus) are kept for two re-derivation cycles. If still absent after two cycles, they're removed — the log format has moved on and the pattern is dead weight. Signal patterns are never auto-removed regardless of staleness.

### Configuration

```python
from logdigest import PatternManager

manager = PatternManager(
    patterns_dir="patterns/",
    unknown_threshold=0.25,       # unknown_pct trigger level
    buffer_size=5000,             # unclassified lines to retain
    auto_rediscover=False,        # True = run drift discover automatically
    # Logprob escalation (only runs after unknown_pct trigger)
    probe_sample_size=30,         # logs to probe for confirmation
    jsd_confirm_threshold=0.15,   # JSD above this = confirmed drift
    jsd_dismiss_threshold=0.10,   # JSD below this = benign, defer
)
```

### What logdigest owns vs. what drift owns

| Concern | Owner | Cost |
|---------|-------|------|
| Runtime classification counters | logdigest | Free (per-line increment) |
| unknown_pct threshold check | logdigest | Free (division on check interval) |
| Unclassified line buffering | logdigest | Memory only (rolling window) |
| Pattern set loading and hot-reload | logdigest | Startup + on trigger |
| Domain/version classification | logdigest | Free (keyword scoring) |
| `drift discover` (corpus → patterns) | drift | CPU (Drain3 + regex gen) |
| `drift probe` (logprob confirmation) | drift | LLM inference (on-demand only) |
| JSD comparison against baseline | drift | Negligible (histogram math) |
| Pattern validation against held-out corpus | drift | CPU (regex matching) |

## Implementation Order

1. **Phase 1a** — JSON pattern format + JSON Schema + `PatternSet` loader with schema validation in logdigest (patterns still hand-authored, but externalized from Python source)
2. **Phase 1b** — `drift discover` command (corpus → pattern JSON)
3. **Phase 1c** — Re-derive current hand-authored patterns via the pipeline, proving the loop works
4. **Phase 2a** — Version detection in `classify_log()`
5. **Phase 2b** — Version-specific pattern sets (start with Ansible 2.14 vs 2.16)
6. **Phase 3a** — Runtime `DigestStats` + unknown_pct monitoring + unclassified line buffering
7. **Phase 3b** — Logprob escalation path (on-demand `drift probe` after unknown_pct trigger)
8. **Phase 3c** — Auto re-derivation with hot-reload

Each phase is independently useful. Phase 1 alone answers the reviewer's provenance question. Phase 2 handles the multi-version/multi-domain concern. Phase 3 makes the system self-maintaining — with continuous monitoring that costs nothing and expensive confirmation only when there's reason to suspect a problem.
