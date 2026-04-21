# Pre-digestion: Theory and Results

## Why Pre-digestion Works

Pre-digestion follows the same fundamental rules as lossless text compression. Both identify and eliminate redundancy — the difference is that pre-digestion is *lossy* and *task-aware*: the discarded bits are guaranteed to be noise for the downstream task, so reconstruction isn't needed.

### The Entropy Bound

Shannon's source coding theorem establishes that no lossless compressor can encode data below its entropy rate. Pre-digestion faces the same limit: you cannot reduce a document below its information content.

A document's Shannon entropy measures the average information per symbol. Low entropy means high redundancy — predictable, repetitive content that pre-digestion can eliminate. High entropy means every token carries unique information — nothing to remove.

| Corpus | Entropy Profile | Redundancy | Impact |
|--------|----------------|------------|--------|
| Ansible logs | Very low — `ok:`, `changed:`, `skipping:` repeated thousands of times | ~80% | 82% chunk reduction, 6.8x faster |
| TechQA (IBM support) | Moderate — template headers/footers across documents | ~11% | 11% reduction, +3.7% nDCG |
| EManual (electronics) | High — unique product descriptions | ~0% | No-op |
| CuAD (legal contracts) | High — unique contract clauses | ~0% | No-op |

**Impact is inversely proportional to document entropy.** High-entropy documents pass through unchanged. Low-entropy documents compress dramatically.

### Predictive Power

This gives a pre-flight diagnostic: measure corpus-level entropy before running the pipeline. If document entropy is high, pre-digestion will have minimal impact. If entropy is low, large gains are likely.

Concretely:
- Compute per-line entropy across the corpus
- Measure cross-document repetition rate (lines appearing in >N% of documents)
- Estimate within-document redundancy (repeated patterns, boilerplate fraction)

These three signals predict impact without running the full pipeline.

### Scope Boundary

**Works when the data has low entropy relative to the task:**
- Log files (massive repetition, low information density)
- Template-generated documents (shared boilerplate across instances)
- Structured extraction (LLM output is largely deterministic)

**Does not help when entropy is high:**
- Free-form natural language documents (every token is informative)
- Creative or open-ended generation (high output entropy by design)
- RAG over clean document collections (nothing redundant to remove)

This is not a limitation to work around — it's a fundamental information-theoretic bound.

## Results

### Input Pre-digestion (500 Ansible Logs)

| Metric | Value |
|--------|-------|
| Total raw tokens | ~11.6M |
| Total digest tokens | ~1.96M |
| **Token savings** | **83.1%** |
| Median per-log savings | 84.3% |
| P10-P90 range | 80.6%-93.3% |

Quality validation (50 logs with LLM summaries):

| Metric | Raw | Pre-digested |
|--------|-----|-------------|
| Format compliance | 60% | **100%** |
| LLM failures (timeout/ramble) | **40%** | **0%** |
| Digest better than raw | -- | **53%** of cases |
| Raw better than digest | -- | **0%** of cases |

Pre-digestion eliminated the 40% raw failure rate on large logs entirely.

### RAG Ingestion (500 Logs + 3 RAGBench Datasets)

Pre-digestion before RAG chunking and embedding:

| Metric | Raw | Pre-digested | Change |
|--------|-----|-------------|--------|
| Chunks | 25,367 | 4,511 | **82% fewer** |
| Embedding time | 27 min | 4 min | **6.8x faster** |
| Same-log cohesion | 0.073 | 0.143 | **2x better** |

Validated on three RAGBench datasets (domain-agnostic pre-digestion):

| Dataset | Noise Level | Char Reduction | nDCG@5 Delta |
|---------|-------------|----------------|-------------|
| TechQA (IBM tech support) | 28 boilerplate lines | 11.1% | **+0.0211** |
| EManual (electronics) | Clean | 0.1% | 0.0000 |
| CuAD (legal contracts) | Clean | 0.2% | -0.0067 |

Pre-digestion helps where noise exists and is a harmless no-op on clean data — a safe default for any RAG pipeline.

## How It Works

### Three-level approach (domain-specific modules)

All domain modules follow the same architecture, with domain-specific patterns:

- **Level 1 (line-level)** — Classify each line as noise/signal/unknown via regex. Strip noise lines while preserving signal (errors, failures, structure).
- **Level 2 (block-level)** — Deduplicate repeated patterns into compact descriptors (e.g., `[RETRY: task, 5 retries, then succeeded]`, `[HEALTH CHECKS: 200 lines, all OK]`).
- **Level 3 (blob-level)** — Compress known verbose structures into one-line summaries.

| Module | Domain | Noise Patterns | Signal Patterns | Block Detectors | Key Feature |
|--------|--------|---------------|----------------|-----------------|-------------|
| `ansible.py` | Ansible job logs | 54 | 9 | retry blocks, item loops | K8s API blob truncation |
| `mustgather.py` | OpenShift must-gather | 40+ | 20+ | health checks, leader election, managedFields | `predigest_bundle()` for directory walking |
| `cicd.py` | GitHub Actions / Jenkins / GitLab CI | 60+ | 20+ | dependency installs, container pulls | `detect_ci_system()` auto-detection |
| `journald.py` | systemd journal | 35+ | 25+ | service restart loops, audit bursts | stack trace truncation, timestamp normalization |

Shared machinery (`domain_base.py`) provides `classify_line()`, `analyze_lines()`, and `find_large_blobs()` so new domain modules only define their pattern lists and domain-specific detectors.

### Entropy pre-flight

Before investing in a new domain module, run the entropy diagnostic:

```python
from logdigest import corpus_entropy
result = corpus_entropy(documents)
# predicted_impact: "high" (>60% redundancy) / "medium" / "low" / "none"
```

Impact is inversely proportional to document entropy. If `predicted_impact` is "none", skip it.

### Corpus-level approach (core module)

- **Boilerplate detection** — Find lines appearing in >5% of documents. These are template artifacts, not content.
- **Deduplication** — MD5-based exact document dedup.
- **Per-document cleaning** — Strip boilerplate, separators, collapse blank runs.

## Relationship to Drift

Denoise is the **runtime library** for pre-digestion. The drift sibling project is where the research, experimentation, and validation happen.

- Drift measures *whether* pre-digestion helps for a given corpus (entropy analysis, quality comparisons)
- Denoise *implements* the pre-digestion that drift validates
- Drift's `drift probe` and `drift distill` commands train classifiers that logdigest runs at inference time

For full experiment details, see the drift repo's `docs/PAPER.md` and `docs/RESULTS.md`.
