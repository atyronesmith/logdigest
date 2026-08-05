# Contributing to denoise

## Setup

```bash
git clone https://github.com/atyronesmith/denoise.git
cd denoise
uv pip install -e ".[dev]"
```

## Running Tests

```bash
python -m pytest -v
```

## Adding a New Domain Module

1. Run `corpus_entropy()` on sample data first — if `predicted_impact` is "none", don't build a module.
2. Domain modules live in `src/denoise/`. Import shared helpers from `domain_base.py` (`classify_line`, `analyze_lines`, `find_large_blobs`).
3. Each module defines `NOISE_PATTERNS` and `SIGNAL_PATTERNS` (list of `(compiled_regex, label)` tuples), block-level detectors, and a `predigest()` entry point.
4. See `ansible.py` for the simplest example, or `cicd.py` for multi-system detection.
5. Add tests in `tests/test_{domain}.py` — cover classify, block detection, and end-to-end predigest.

## Code Style

- Pure Python, no runtime dependencies in core
- Functional style — prefer stateless functions
- All public functions need docstrings and type hints
