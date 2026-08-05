# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

logdigest is a Python library for task-aware pre-digestion of log data before RAG/LLM processing. It provides general-purpose cleaning (boilerplate detection, deduplication) and domain-specific modules (Ansible, OpenShift must-gather, CI/CD, systemd journal). Pure Python 3.11+, zero runtime dependencies for core; MCP server requires `fastmcp`, RAG support requires `numpy`.

## Commands

```bash
# Install for development (uses uv, not pip)
uv pip install -e ".[dev]"

# Run all tests
python -m pytest -v

# Run benchmarks
uv run python benchmarks/run_all.py
```

Linter: `ruff check src/ tests/` (config in `pyproject.toml`).

## Architecture

**Two-layer design:** domain-agnostic core + domain-specific modules, with shared machinery in `domain_base.py`.

- `src/logdigest/core.py` — General-purpose pipeline: boilerplate detection, MD5 deduplication, per-document cleaning. Stateless functional style.

- `src/logdigest/domain_base.py` — Shared machinery for domain modules: `DomainModule` Protocol, `classify_line()`, `analyze_lines()`, `find_large_blobs()`, `predigest_stream()`, `PatternSet` loader. Domain modules implement the `DomainModule` Protocol; patterns are loaded from JSON files in `src/logdigest/patterns/`.

- `src/logdigest/ansible.py` — Ansible log compression. Three levels: (1) line-level classification via regex, (2) block-level dedup (retry loops, item loops), (3) blob-level truncation (K8s API responses). Entry point: `predigest()`.

- `src/logdigest/mustgather.py` — OpenShift must-gather. Health checks, leader election, managedFields. `predigest_bundle()` for directory walking.

- `src/logdigest/cicd.py` — GitHub Actions / Jenkins / GitLab CI. `detect_ci_system()` auto-detection.

- `src/logdigest/journald.py` — systemd journal. Stack trace truncation, timestamp normalization.

- `src/logdigest/entropy.py` — Entropy pre-flight: `corpus_entropy()` predicts impact before building a module.

- `src/logdigest/mcp.py` — MCP server exposing `predigest`, `detect_domain`, `entropy_check` tools.

- `src/logdigest/__init__.py` — Public API re-exports from `core.py` and `entropy.py`. Domain modules used directly.

All domain modules support streaming pre-digestion for files >50MB via `predigest_file_streaming()`.

## Documentation

- `docs/PREDIGESTION.md` — Theory (entropy bounds, scope), results (83% token savings, RAG validation)
- `docs/PATTERN_GENERATION.md` — Automated pattern discovery plan (Drain3, drift detection, conflict resolution)
- `docs/RELATED_WORK.md` — Positioning vs Drain3, LLMLingua, MCP ecosystem

## Benchmarks

- `benchmarks/run_all.py` — Run all benchmarks
- `benchmarks/bench_token_savings.py` — Token savings (84.5% on synthetic Ansible logs)
- `benchmarks/bench_entropy_prediction.py` — Entropy pre-flight accuracy
- `benchmarks/generate_corpus.py` — Synthetic Ansible log corpus generator

## What is NOT in this repo

Guard/classifier/monitor code was moved to the drift sibling repo (`drift/src/drift/safety/`). Do not look for or create `guard.py` or `monitor.py` here.

## Relationship to Drift

logdigest is the **runtime pre-digestion library**. The drift sibling repo handles training, probing, experimentation, and safety classification.

- Drift experiments validate logdigest's pre-digestion effectiveness
- `logdigest discover` generates pattern sets from log corpora (requires `[discover]` extra)
- Guard classification and drift monitoring live in `drift/src/drift/safety/`, not here
