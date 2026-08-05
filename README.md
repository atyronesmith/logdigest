# denoise

Task-aware pre-digestion for RAG and LLM pipelines. Removes low-entropy content (boilerplate, duplicates, noise) from document corpora before they reach your retrieval or generation stage.

## Features

- **General-purpose cleaning:** cross-document boilerplate detection, MD5 deduplication, separator line removal, whitespace normalization
- **Domain-specific modules:** Ansible logs, OpenShift must-gather, CI/CD logs (GitHub Actions/Jenkins/GitLab CI), systemd journal — all with line classification, block collapsing, and blob truncation
- **Entropy pre-flight:** predict whether a corpus will benefit from pre-digestion before building anything
- **Safety guard optimization:** tiered classification (keyword scan → logistic regression → LLM escalation) that eliminates most LLM guard calls on software engineering content
- **Zero runtime dependencies** — pure Python 3.11+

## Installation

```bash
# Core library
pip install denoise

# With optional RAG support
pip install "denoise[rag]"
```

## Quick Start

```python
from denoise import predigest_document, build_boilerplate_set, dedup_documents

# Clean a single document
cleaned = predigest_document(text)

# Corpus-level pipeline
docs = ["doc1 text", "doc2 text", ...]
boilerplate = build_boilerplate_set(docs)
unique_docs = dedup_documents(docs)
```

For domain-specific logs:

```python
from denoise.ansible import predigest
compressed = predigest(ansible_log_text)

from denoise.mustgather import predigest, predigest_bundle
compressed = predigest(pod_log_text)
bundle = predigest_bundle(Path("must-gather-dir/"))  # walks entire bundle

from denoise.cicd import predigest, detect_ci_system
compressed = predigest(github_actions_log)  # auto-detects CI system

from denoise.journald import predigest
compressed = predigest(journalctl_output)
```

Entropy pre-flight (should I build a module for this corpus?):

```python
from denoise import corpus_entropy

result = corpus_entropy(my_documents)
# result["predicted_impact"]  → "high" / "medium" / "low" / "none"
# result["recommendation"]    → human-readable guidance
```

For safety guard pre-digestion:

```python
from denoise.guard import predigest, classify, load_weights

# Reduce tokens before sending to a safety guard
cleaned = predigest(llm_output)

# Or skip the guard entirely with tiered classification
load_weights("weights/guard_classifier_weights.json")
verdict, confidence = classify(llm_output)
# ("safe", 0.96) → no guard needed
# ("escalate", 0.72) → call LLM guard
```

## MCP Server (Claude Code Integration)

Denoise can run as an MCP server, giving Claude Code automatic access to pre-digestion tools. Claude will auto-detect the log type and clean it before analysis.

```bash
# Install with MCP support
uv pip install -e ".[mcp]"
```

Add to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "denoise": {
      "command": "uv",
      "args": ["--directory", "/path/to/denoise", "run", "python", "-m", "denoise"],
      "env": {}
    }
  }
}
```

Three tools are exposed: `predigest` (clean logs), `detect_domain` (identify log type), and `entropy_check` (predict impact on a corpus).

## Documentation

- [Pre-digestion theory and results](docs/PREDIGESTION.md) — why it works (entropy bounds), validated results (83% token savings)
- [Guard module](docs/GUARD.md) — tiered classification, empirical findings, training weights
- [Continuous calibration](docs/CONTINUOUS_CALIBRATION.md) — runtime drift detection and recalibration
- Training and experimentation: see the [drift](https://github.com/atyronesmith/drift) sibling repo

## Development

```bash
# Set up dev environment
uv pip install -e ".[dev]"

# Run tests
python -m pytest -v
```

## License

See [pyproject.toml](pyproject.toml) for project metadata.
