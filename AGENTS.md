# AGENTS.md

Instructions for AI coding agents working in this repository.

## Key Constraints

- Python 3.11+ required
- No runtime dependencies in the core module — stdlib only
- Domain-specific modules (like `ansible.py`) follow a three-pass pattern: line classification, block deduplication, blob truncation
- The public API is defined in `src/denoise/__init__.py` — update it when adding new core functions

## Testing

Run `python -m pytest -v` before submitting changes. Tests live in `tests/` and mirror the `src/denoise/` module structure.

## Architecture Decisions

- **Functional pipeline:** Core operations are stateless functions composed into workflows. Keep it that way.
- **Domain modules are separate:** Each domain (ansible, etc.) gets its own module with a `predigest()` entry point. Don't merge domain logic into `core.py`.
- **Boilerplate detection is corpus-level:** `build_boilerplate_set()` analyzes across documents, not within a single document. This is intentional.
