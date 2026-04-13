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

Domain-specific modules live in `src/denoise/` alongside `ansible.py`. Each module should expose a `predigest()` function as its main entry point. See `ansible.py` for the pattern: line-level classification, block-level deduplication, and blob-level truncation.

## Code Style

- Pure Python, no runtime dependencies in core
- Functional style — prefer stateless functions
- All public functions need docstrings and type hints
