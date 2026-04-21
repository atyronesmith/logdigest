# AGENTS.md

Instructions for AI coding agents working in this repository.

## Key Constraints

- Python 3.11+ required
- No runtime dependencies in the core module — stdlib only
- Domain-specific modules (like `ansible.py`) follow a three-pass pattern: line classification, block deduplication, blob truncation
- The public API is defined in `src/logdigest/__init__.py` — update it when adding new core functions

## Testing

Run `python -m pytest -v` before submitting changes. Tests live in `tests/` and mirror the `src/logdigest/` module structure.

## Architecture Decisions

- **Functional pipeline:** Core operations are stateless functions composed into workflows. Keep it that way.
- **Domain modules are separate:** Each domain gets its own module with a `predigest()` entry point. Don't merge domain logic into `core.py`. Current domain modules: `ansible.py`, `mustgather.py`, `cicd.py`, `journald.py`.
- **Shared machinery lives in `domain_base.py`:** Generic `classify_line()`, `analyze_lines()`, `analyze_file()`, and `find_large_blobs()`. New domain modules should import from here rather than reimplementing.
- **Boilerplate detection is corpus-level:** `build_boilerplate_set()` analyzes across documents, not within a single document. This is intentional.
- **Entropy pre-flight:** Use `entropy.py` to assess a corpus before building a new domain module. If `corpus_entropy()` returns `predicted_impact: "none"`, skip it.
