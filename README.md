# logdigest

Task-aware pre-digestion for RAG and LLM pipelines. Removes low-entropy content (boilerplate, duplicates, noise) from document corpora before they reach your retrieval or generation stage.

## Features

- **General-purpose cleaning:** cross-document boilerplate detection, MD5 deduplication, separator line removal, whitespace normalization
- **Domain-specific modules:** Ansible logs, OpenShift must-gather, CI/CD logs (GitHub Actions/Jenkins/GitLab CI), systemd journal — all with line classification, block collapsing, and blob truncation
- **Entropy pre-flight:** predict whether a corpus will benefit from pre-digestion before building anything
- **Zero runtime dependencies for core** — pure Python 3.11+; MCP server requires `fastmcp`, RAG support requires `numpy`

## Installation

```bash
# Core library
pip install logdigest

# With optional RAG support
pip install "logdigest[rag]"
```

## Quick Start

```python
from logdigest import predigest_document, predigest_corpus

# Clean a single document
cleaned = predigest_document(text)

# Corpus-level pipeline (dedup + boilerplate + per-doc cleaning)
docs = ["doc1 text", "doc2 text", ...]
digested, stats = predigest_corpus(docs)
# stats["char_reduction_pct"], stats["boilerplate_lines"], etc.
```

For domain-specific logs:

```python
from logdigest.ansible import predigest
compressed = predigest(ansible_log_text)

from logdigest.mustgather import predigest, predigest_bundle
compressed = predigest(pod_log_text)
result = predigest_bundle(Path("must-gather-dir/"))  # walks entire bundle
bundle = result["files"]        # {relative_path: compressed_text}
skipped = result["skipped"]     # [{file, reason}] for unreadable files

from logdigest.cicd import predigest, detect_ci_system
compressed = predigest(github_actions_log)  # auto-detects CI system

from logdigest.journald import predigest
compressed = predigest(journalctl_output)
```

Entropy pre-flight (should I build a module for this corpus?):

```python
from logdigest import corpus_entropy

result = corpus_entropy(my_documents)
# result["predicted_impact"]  → "high" / "medium" / "low" / "none"
# result["recommendation"]    → human-readable guidance
```

## Pattern Discovery

Automatically generate pattern sets from a corpus of logs using Drain3 template mining:

```bash
# Install with discovery support
uv pip install -e ".[discover]"

# Discover patterns from a corpus
logdigest discover logs/ --domain ansible -o ansible_discovered.json

# Use existing patterns as a reference (signal-always-wins conflict resolution)
logdigest discover logs/ -d myapp -o myapp.json --reference ansible.json

# Tune Drain3 parameters
logdigest discover logs/ -d ansible --depth 6 --similarity 0.3 --min-frequency 0.005
```

The output is a JSON pattern set that can be loaded directly by `PatternSet.load_from_file()`. See [docs/PATTERN_GENERATION.md](docs/PATTERN_GENERATION.md) for the full pipeline design.

## MCP Server (Claude Code Integration)

logdigest can run as an MCP server, giving Claude Code automatic access to pre-digestion tools. Claude will auto-detect the log type and clean it before analysis.

```bash
# Install with MCP support
uv pip install -e ".[mcp]"
```

Add to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "logdigest": {
      "command": "uv",
      "args": ["--directory", "/path/to/logdigest", "run", "python", "-m", "logdigest"],
      "env": {}
    }
  }
}
```

Three tools are exposed: `predigest` (clean logs), `detect_domain` (identify log type), and `entropy_check` (predict impact on a corpus).

### Composing with OpenShift MCP Servers

logdigest pairs naturally with cluster diagnostic MCP servers like [openshift-mcp-server](https://github.com/rh-ecosystem-edge/openshift-mcp-server). The diagnostic server pulls must-gather bundles, pod logs, and cluster events; logdigest pre-digests them before they reach the LLM context window. On real must-gather bundles this cuts diagnostic context by 80-90%, which is the difference between a useful triage response and one that truncates or hallucinates mid-analysis. Today the composition is manual (call `predigest`, then pass the result to the diagnostic tool), but wiring the two MCP servers together in a single `.mcp.json` is the intended workflow — Claude Code sees both tool sets and can chain them in a single conversation.

## Trust Boundaries

Pattern sets (the JSON files in `src/logdigest/patterns/`) contain regexes that run against every line of input. They are supply-chain artifacts — treat them like code. Do not load untrusted or user-supplied pattern JSON into `PatternSet` without review. The built-in patterns are authored and tested within this repo; `logdigest discover` generates candidates that should be reviewed before deployment. Log input itself is untrusted by design and handled safely (regex matching, no eval/exec).

## Documentation

- [Pre-digestion theory and results](docs/PREDIGESTION.md) — why it works (entropy bounds), validated results (83% token savings)
- [Automated pattern generation](docs/PATTERN_GENERATION.md) — corpus-driven pattern discovery, live drift detection
- [Related work](docs/RELATED_WORK.md) — positioning against Drain3, LLMLingua, MCP ecosystem
- Safety guard optimization (tiered classification, empirical findings): see the [drift](https://github.com/atyronesmith/drift) sibling repo

## Development

```bash
# Set up dev environment
uv pip install -e ".[dev]"

# Run tests
python -m pytest -v
```

## License

See [pyproject.toml](pyproject.toml) for project metadata.
