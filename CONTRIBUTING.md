# Contributing to logdigest

## Setup

```bash
git clone https://github.com/atyronesmith/logdigest.git
cd logdigest
uv pip install -e ".[dev]"
```

## Running Tests

```bash
python -m pytest -v
```

## Adding a New Domain Module

1. Run `corpus_entropy()` on sample data first — if `predicted_impact` is "none", don't build a module.
2. Use `logdigest discover` on a representative corpus to generate an initial pattern set, or author patterns manually as JSON in `src/logdigest/patterns/{domain}.json`.
3. Domain modules live in `src/logdigest/`. Import shared helpers from `domain_base.py` (`classify_line`, `analyze_lines`, `find_large_blobs`).
4. Patterns are loaded via `PatternSet.load("{domain}")` from JSON files — see existing pattern files in `src/logdigest/patterns/` for the format and `schemas/pattern_set.schema.json` for the schema.
5. Each module defines block-level detectors and a `predigest()` entry point.
6. See `ansible.py` for the simplest example, or `cicd.py` for multi-system detection.
7. Add tests in `tests/test_{domain}.py` — cover classify, block detection, and end-to-end predigest.

## Code Style

- Pure Python, no runtime dependencies in core
- Functional style — prefer stateless functions
- All public functions need docstrings and type hints
- Lint with `ruff check src/ tests/` before submitting (config in `pyproject.toml`)

## What Not to Commit

- `.env` files (API keys, credentials)
- `.mcp.json` (local Claude Code MCP config — see README for an example)
- `results/` and `datasets/` (experiment outputs and downloaded corpora)
- `.coverage`, `.DS_Store`
