"""MCP server for logdigest pre-digestion tools.

Exposes logdigest as tools that Claude Code (or any MCP client) can call
to pre-digest logs before reasoning about them.

Usage:
    # stdio transport (for Claude Code)
    uv run python -m logdigest.mcp

    # Or via fastmcp CLI
    fastmcp run logdigest.mcp:mcp
"""

from __future__ import annotations

import importlib.resources
import logging
from typing import TYPE_CHECKING

from fastmcp import FastMCP

from logdigest.entropy import entropy_report

if TYPE_CHECKING:
    from collections.abc import Callable

_log = logging.getLogger(__name__)

mcp = FastMCP(
    "logdigest",
    instructions=(
        "Pre-digestion tools for noisy log data. Use `predigest` to clean "
        "logs before analysis — it strips noise (health checks, dependency "
        "installs, boilerplate) while preserving errors and signal. Use "
        "`detect_domain` first if you're unsure which log type you have."
    ),
)

_domain_signatures: list[tuple[str, list[str]]] | None = None
_digesters: dict[str, Callable[[str], str]] | None = None


def _discover_domain_names() -> list[str]:
    """Auto-discover domains from pattern JSON files in the package."""
    patterns_pkg = importlib.resources.files("logdigest.patterns")
    names = []
    for item in patterns_pkg.iterdir():
        name = item.name if hasattr(item, "name") else str(item)
        if name.endswith(".json") and "@" not in name:
            names.append(name.removesuffix(".json"))
    return sorted(names)


def _load_domains() -> None:
    """Lazy-load domain modules and signatures, skipping broken domains."""
    global _domain_signatures, _digesters
    if _domain_signatures is not None:
        return

    import importlib

    from logdigest.domain_base import DomainModule, PatternSet

    sigs: list[tuple[str, list[str]]] = []
    funcs: dict[str, Callable[[str], str]] = {}

    for name in _discover_domain_names():
        try:
            mod = importlib.import_module(f"logdigest.{name}")
        except ImportError:
            _log.debug("No module logdigest.%s for pattern file — skipping", name)
            continue
        except Exception:
            _log.warning("Failed to load domain %s — skipping", name, exc_info=True)
            continue

        if not isinstance(mod, DomainModule):
            _log.debug("logdigest.%s does not satisfy DomainModule — skipping", name)
            continue

        try:
            ps = PatternSet.load(name)
            funcs[name] = mod.predigest
            if ps is not None:
                sigs.append((name, ps.domain_keywords))
        except Exception:
            _log.warning("Failed to load patterns for %s — skipping", name, exc_info=True)

    _domain_signatures = sigs
    _digesters = funcs


def _detect_domain(text: str) -> str | None:
    """Detect which domain module best fits the log text."""
    _load_domains()
    first_chunk = text[:5000]
    scores: dict[str, int] = {}
    for domain, keywords in _domain_signatures:
        score = sum(1 for kw in keywords if kw in first_chunk)
        if score > 0:
            scores[domain] = score
    if not scores:
        return None
    return max(scores, key=scores.get)


def predigest_impl(text: str, domain: str | None = None) -> str:
    """Pre-digest log text by removing noise and preserving errors/signal.

    Strips health checks, dependency installs, boilerplate, and other
    low-information content. Keeps errors, failures, panics, and context
    around them. Typically reduces token count by 60-85%.

    Args:
        text: Raw log content (from oc logs, journalctl, CI/CD output, etc.)
        domain: Optional domain hint. One of "ansible", "mustgather",
            "cicd", "journald". Auto-detected if not provided.

    Returns:
        Cleaned log text with noise removed.
    """
    if not text.strip():
        return text

    if domain is None:
        domain = _detect_domain(text)

    _load_domains()
    digester = _digesters.get(domain) if domain else None

    if digester:
        return digester(text)

    from logdigest.core import predigest_document
    return predigest_document(text)


def detect_domain_impl(text: str) -> str:
    """Detect which log domain a text sample belongs to.

    Examines the content for domain-specific markers and returns
    the best-matching domain module name.

    Args:
        text: Log content to classify.

    Returns:
        Domain name ("ansible", "mustgather", "cicd", "journald")
        or "unknown" if no domain matches.
    """
    result = _detect_domain(text)
    return result or "unknown"


def entropy_check_impl(texts: list[str]) -> str:
    """Check whether a corpus of documents would benefit from pre-digestion.

    Measures redundancy across the provided documents and predicts
    the impact of building or applying pre-digestion.

    Args:
        texts: List of document/log texts to analyze.

    Returns:
        Human-readable entropy report with impact prediction.
    """
    return entropy_report(texts)


@mcp.tool()
def predigest(text: str, domain: str | None = None) -> str:
    """Pre-digest log text by removing noise and preserving errors/signal.

    Strips health checks, dependency installs, boilerplate, and other
    low-information content. Keeps errors, failures, panics, and context
    around them. Typically reduces token count by 60-85%.

    Args:
        text: Raw log content (from oc logs, journalctl, CI/CD output, etc.)
        domain: Optional domain hint. One of "ansible", "mustgather",
            "cicd", "journald". Auto-detected if not provided.

    Returns:
        Cleaned log text with noise removed.
    """
    return predigest_impl(text, domain)


@mcp.tool()
def detect_domain(text: str) -> str:
    """Detect which log domain a text sample belongs to.

    Examines the content for domain-specific markers and returns
    the best-matching domain module name.

    Args:
        text: Log content to classify.

    Returns:
        Domain name ("ansible", "mustgather", "cicd", "journald")
        or "unknown" if no domain matches.
    """
    return detect_domain_impl(text)


@mcp.tool()
def entropy_check(texts: list[str]) -> str:
    """Check whether a corpus of documents would benefit from pre-digestion.

    Measures redundancy across the provided documents and predicts
    the impact of building or applying pre-digestion.

    Args:
        texts: List of document/log texts to analyze.

    Returns:
        Human-readable entropy report with impact prediction.
    """
    return entropy_check_impl(texts)


def main():
    """Run the MCP server with stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
