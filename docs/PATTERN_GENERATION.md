# Automated Pattern Generation

## Status: Phase 1b Complete

NOISE_PATTERNS and SIGNAL_PATTERNS for each domain are loaded from JSON files (`src/logdigest/patterns/*.json`) via `PatternSet.load()`. These pattern sets can be generated automatically from a log corpus using `logdigest discover`, or hand-authored. The shipped baseline patterns were originally hand-authored; new domains should use the discovery pipeline.

This document describes the full pipeline: pattern discovery (implemented), version-aware routing (planned), and live drift detection via `unknown_pct` monitoring (planned). An earlier design included logprob-based escalation for semantic drift detection; this was investigated and deprioritized — see Phase 3 for rationale.

## Problem

Static regex lists have three weaknesses:

1. **Provenance** — no automated trail from corpus to patterns.
2. **Staleness** — log formats change across versions (Ansible 2.14 vs 2.16, RHEL 8 vs 9 systemd). Static patterns drift silently.
3. **Scope** — adding a new domain requires reading logs and writing regex manually. `logdigest discover` automates this.

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
│  runtime stats → unknown_pct monitoring → re-derive     │
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

The template-to-regex conversion is implemented in `src/logdigest/discover/regex_convert.py`.

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

Domain modules load patterns from JSON at import time via `PatternSet.load()`, which raises `FileNotFoundError` if the pattern file is missing.

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
    "conflicts": [{ "pattern", "existing_class", "proposed_class", "action" }]
  }
}
```

Labels match `^[a-z][a-z0-9_]*$`. Pattern entries may also include `confidence`, `corpus_frequency`, `stale`, and `stale_cycles` fields for automated pattern generation.

The loader additionally validates that every `regex` field compiles successfully via `re.compile()` after schema validation passes. Schema validation uses stdlib `json` for parsing; the `jsonschema` package is an optional dependency (`pip install logdigest[dev]`). If `jsonschema` is not installed, the loader skips schema validation and falls back to regex-compile-time error checking only, with a warning.

### Implementation

```bash
logdigest discover <corpus_dir> --domain ansible -o patterns.json
```

Pipeline: corpus loading → file size guard → entropy pre-flight → train/test split → Drain3 mining (streaming) → template-to-regex → noise/signal classification → conflict resolution → held-out validation → export.

Implementation:

- `logdigest discover` CLI command in `src/logdigest/cli.py` (Click-based, lazy-imported via `__main__.py`)
- Discovery pipeline in `src/logdigest/discover/` subpackage:
  - `mining.py` — Drain3 wrapper, accepts any `Iterable[str]` for streaming
  - `regex_convert.py` — template-to-regex conversion (exact, prefix, multi-wildcard)
  - `classifier.py` — noise/signal classification, conflict resolution using real sample lines
  - `validation.py` — held-out test set validation with recall and safety checks
  - `export.py` — JSON export matching `pattern_set.schema.json`
- `PatternSet` class in `domain_base.py` loads from JSON with schema validation
- Each domain module uses `PatternSet.load("ansible")` at import time
- Runtime classification checks signal patterns before noise (signal always wins)

**Version resolution in `PatternSet.load(domain, version=None, *, required=True)`:**

| `version` argument | Behavior |
|---------------------|----------|
| `None` (default) | Load the baseline (`ansible.json`). Raises `FileNotFoundError` if missing (unless `required=False`). |
| `"latest"` | Load the most recently generated pattern file for this domain (by `generated` timestamp in the JSON). Falls back to baseline if no generated files exist. |
| `"2.16"` (explicit) | Load the named version file. Error if not found — no silent fallback when a specific version is requested. |

The baseline file can be hand-authored or generated by `logdigest discover`. Generated files from discovery are validated against the schema and can replace or augment the baseline.

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

logdigest buffers unclassified lines in a rolling window. When `unknown_pct` crosses the threshold (default 0.25), it triggers the escalation path.

### Logprob Escalation — Investigated, Not Pursuing

The original Phase 3 design included a logprob escalation step: after `unknown_pct` triggers, run `drift probe` on sampled pre-digested logs, capture per-token logprobs on the LLM's output, and compare the entropy distribution (via JSD) against a baseline established at pattern generation time. The hypothesis was that logprobs could detect *semantic* drift — cases where patterns still match syntactically but the content they strip has changed in ways that hurt downstream LLM quality.

**Why we're not building this:**

1. **The signal logprobs provide is indirect.** Logprobs measure the LLM's confidence on its *output*, not the quality of the *input*. For log pre-digestion, the question is "did we strip something the LLM needed?" — that's directly observable by comparing task output quality with and without pre-digestion, which is cheaper than a logprob probe pipeline.

2. **The cost is disproportionate to the risk.** Logprob probing requires running an LLM over sampled logs, which has real inference cost. The scenario it detects (patterns match but content semantics shifted) is rare in practice — log format changes almost always manifest as new line formats that `unknown_pct` catches directly.

3. **`unknown_pct` alone is sufficient for practical drift detection.** A rising fraction of unclassified lines is a strong, free, direct signal that patterns are stale. Combined with automated re-derivation via `logdigest discover`, this provides a complete self-maintenance loop without LLM inference in the monitoring path.

4. **Reproducibility concerns.** Logprob baselines are model-specific — changing the downstream LLM invalidates the baseline, creating a coupling between pattern maintenance and model selection that adds operational complexity without clear benefit.

Logprob-based analysis remains valuable in drift's core use case (identifying deterministic output fields for hybrid extraction). It is the wrong tool for input-side pre-digestion quality monitoring, where mechanical approaches (frequency-based classification, `unknown_pct` tracking) answer the relevant questions directly and cheaply.

### Re-derivation

When `unknown_pct` crosses the threshold:

```
┌──────────────────────────────┐
│ Collect unclassified lines   │
│ from rolling buffer          │
└──────────┬───────────────────┘
           ▼
┌──────────────────────────────┐
│ logdigest discover <buffer>  │
│   --domain <detected>        │
│   --reference <current.json> │
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
    auto_rediscover=False,        # True = run logdigest discover automatically
)
```

### What logdigest owns

| Concern | Cost |
|---------|------|
| Runtime classification counters | Free (per-line increment) |
| unknown_pct threshold check | Free (division on check interval) |
| Unclassified line buffering | Memory only (rolling window) |
| Pattern set loading and hot-reload | Startup + on trigger |
| Domain/version classification | Free (keyword scoring) |
| `logdigest discover` (corpus → patterns) | CPU (Drain3 + regex gen) |
| Pattern validation against held-out corpus | CPU (regex matching) |

## Implementation Order

1. **Phase 1a** ✅ — JSON pattern format + JSON Schema + `PatternSet` loader with schema validation. Domain modules load patterns from JSON via `PatternSet.load()`.
2. **Phase 1b** ✅ — `logdigest discover` command (corpus → pattern JSON) with streaming Drain3 mining, conflict resolution using real sample lines, and held-out validation.
3. **Phase 1c** — Re-derive baseline patterns via the pipeline on each domain's reference corpus, validating that discovered patterns match or improve on the originals.
4. **Phase 2a** — Version detection in `classify_log()`
5. **Phase 2b** — Version-specific pattern sets (start with Ansible 2.14 vs 2.16)
6. **Phase 3a** — Runtime `DigestStats` + unknown_pct monitoring + unclassified line buffering
7. ~~**Phase 3b** — Logprob escalation path~~ — Investigated, not pursuing (see above).
8. **Phase 3b** — Auto re-derivation with hot-reload

Each phase is independently useful. Phase 1 alone answers the reviewer's provenance question. Phase 2 handles the multi-version/multi-domain concern. Phase 3 makes the system self-maintaining with continuous monitoring that costs nothing — `unknown_pct` alone is sufficient to trigger re-derivation without LLM inference in the monitoring path.
