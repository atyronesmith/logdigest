# Related Work

## Log Template Mining

**Drain3** (logpai) is the de facto standard for log template mining. It parses log lines into a fixed-depth tree by token position, clusters lines into templates unsupervised, and runs in streaming mode. It is the right tool for Level 1 (line-level) pattern discovery, and logdigest's pattern generation pipeline uses it as the clustering engine (see `PATTERN_GENERATION.md`).

Denoise's contribution is not at the line level. It's the three-level expansion beyond what Drain3 does:

- **Level 2 (block patterns):** Drain3 operates on individual lines. It cannot detect that 47 consecutive retry lines form a collapsible block, or that 200 health-check lines should become `[HEALTH CHECKS: 200 lines, all OK]`. Block detection requires sequence-level analysis across lines.
- **Level 3 (blob patterns):** Drain3 doesn't handle multi-kilobyte embedded structures (K8s API responses, base64 blobs, managedFields YAML) that should be truncated to one-line summaries.
- **Task-aware classification:** Drain3 discovers templates but doesn't classify them as noise vs. signal. Denoise classifies based on corpus frequency and error-token heuristics, preserving a context window around errors.

## Prompt Compression

**LLMLingua / LongLLMLingua / LLMLingua-2** (Microsoft) compress prompts by scoring token importance with a small language model, achieving 5-20x compression. The goal overlaps with logdigest — reduce tokens before they reach the LLM — but the mechanism and tradeoffs differ:

| | Denoise | LLMLingua |
|---|---|---|
| Mechanism | Rules-based, domain-aware | ML-based, task-agnostic |
| Latency | <1ms (regex matching) | 100ms+ (small LM inference) |
| Dependencies | Zero (stdlib Python) | PyTorch, transformers |
| Determinism | Deterministic, auditable | Probabilistic |
| Compression type | Lossy on noise, lossless on signal | Lossy across all tokens |
| Error preservation | Explicit (context window around errors) | Implicit (model decides importance) |

Denoise chose rules over ML because: (a) deterministic behavior matters in ops contexts where the same input must always produce the same output, (b) no model loading or GPU required — runs in constrained environments like customer clusters, (c) which lines were stripped is auditable, which matters when the downstream task is incident triage.

LLMLingua is the better choice when the input is free-form natural language without domain-specific noise structure. Denoise is the better choice when the input is structured logs with known, repeated noise patterns.

## MCP Ecosystem

**kubernetes-mcp-server / openshift-mcp-server** (containers/openshift orgs) expose kubectl/oc commands as MCP tools. Denoise is complementary: these servers produce log output, logdigest pre-digests it before it reaches the LLM's context window. The composition doesn't happen automatically today (MCP tools don't chain), but logdigest sitting between diagnostic output and LLM context is the intended deployment model.

**token-optimizer-mcp** (ooples) claims 95%+ token reduction for Claude Code via caching and compression. Different goal: it caches and compresses MCP tool output generically. Denoise operates on domain-specific log content with semantic understanding of what's noise vs. signal.

## ML Drift Detection

**Alibi Detect, NannyML, Evidently** are production ML monitoring platforms with drift detection (KS tests, MMD, ADWIN, etc.). Denoise's pattern drift monitor (`PATTERN_GENERATION.md` Phase 3) implements JSD on a single specific distribution (mean-token-entropy from logprob probes) as a stdlib-only subset for embedded deployment. It is not a general-purpose drift detection framework.

## Log Anomaly Detection

**DeepLog, LogRobust, LogAnomaly** use deep learning for log anomaly detection. Adjacent but different goal — they find anomalous log sequences, logdigest removes known-noise log lines. No overlap in mechanism or output.

## Document Preprocessing for RAG

**unstructured.io, Haystack preprocessors** clean documents for RAG pipelines. Denoise's `core.py` (corpus-level boilerplate detection, deduplication) overlaps with this space for domain-agnostic cleaning. The domain-specific modules (Ansible, must-gather, CI/CD, journald) go beyond what generic document preprocessors handle.
