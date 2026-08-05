# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Denoise is a Python library for removing low-entropy content (noise) from document corpora before RAG/LLM processing. It provides general-purpose pre-digestion (boilerplate detection, deduplication, separator removal) and domain-specific preprocessing (Ansible log compression). Pure Python, no runtime dependencies.

## Commands

```bash
# Install for development (uses uv)
uv pip install -e ".[dev]"

# Run all tests
python -m pytest -v

# Run a single test file
python -m pytest tests/test_core.py -v

# Run a specific test
python -m pytest tests/test_core.py::test_function_name -v
```

No linter or formatter is currently configured.

## Architecture

**Two-layer design:** a domain-agnostic core + domain-specific modules.

- `src/denoise/core.py` — General-purpose pipeline: corpus-level boilerplate detection (`build_boilerplate_set`), MD5 deduplication (`dedup_documents`), per-document cleaning (`predigest_document`), and full orchestration (`predigest_corpus`). Stateless functional style.

- `src/denoise/ansible.py` — Ansible log compression using three passes: (1) line-level classification via regex into noise/signal/unknown, (2) block-level deduplication collapsing retry and ok-item loops, (3) blob-level truncation of large JSON (K8s API responses). Entry point is `predigest()`.

- `src/denoise/__init__.py` — Public API re-exports from `core.py` only. Ansible module is used directly.

The project is a library (no CLI entry points). Built with hatchling, dependencies managed with uv.

## Documentation

- `docs/PREDIGESTION.md` — Theory (entropy bounds, scope), results (83% token savings, RAG validation)
- `docs/GUARD.md` — Guard module: pre-digestion, tiered classification, empirical findings (47.8% FPR, coin-flip confidence)
- `docs/CONTINUOUS_CALIBRATION.md` — Runtime drift detection, JSD monitoring, auto-retrain workflow
- `docs/CONDITIONAL_CLASSIFIERS.md` — Planned: conditional classifiers, escalation enrichment

## Relationship to Drift

Denoise is the **runtime library**. The drift sibling repo is where training, probing, and experimentation happen.

- Drift trains classifiers (`drift distill`, `scripts/train_guard_classifier.py`) → exports weights
- Denoise loads weights and runs classifiers at inference time
- Drift validates denoise's accuracy across experiments
- Shared contract: weight serialization format (JSON with vocabulary, weight matrices, bias vectors)
