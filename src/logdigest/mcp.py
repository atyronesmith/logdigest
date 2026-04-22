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

from fastmcp import FastMCP

from logdigest.ansible import predigest as ansible_predigest
from logdigest.mustgather import predigest as mustgather_predigest
from logdigest.cicd import predigest as cicd_predigest, detect_ci_system
from logdigest.journald import predigest as journald_predigest
from logdigest.entropy import corpus_entropy, entropy_report
from logdigest.domain_base import PatternSet

mcp = FastMCP(
    "logdigest",
    instructions=(
        "Pre-digestion tools for noisy log data. Use `predigest` to clean "
        "logs before analysis — it strips noise (health checks, dependency "
        "installs, boilerplate) while preserving errors and signal. Use "
        "`detect_domain` first if you're unsure which log type you have."
    ),
)

# Domain detection keywords — loaded from JSON config, with hardcoded fallback
_FALLBACK_SIGNATURES = {
    "ansible": ["PLAY [", "TASK [", "ok: [", "fatal: [", "PLAY RECAP"],
    "cicd": ["##[group]", "##[debug]", "[Pipeline]", "Using Docker executor",
             "Collecting ", "Successfully installed", "npm warn"],
    "journald": ["systemd[", "kernel:", "audit[", "pam_unix", "CROND["],
    "mustgather": ["healthz", "readyz", "livez", "leader election",
                   "renewed lease", "CrashLoopBackOff", "informer"],
}

_DOMAIN_SIGNATURES = []
for _domain in ["ansible", "cicd", "journald", "mustgather"]:
    _dps = PatternSet.load(_domain)
    _kw = _dps.domain_keywords if _dps else _FALLBACK_SIGNATURES[_domain]
    _DOMAIN_SIGNATURES.append((_domain, _kw))

_DIGESTERS = {
    "ansible": ansible_predigest,
    "mustgather": mustgather_predigest,
    "cicd": cicd_predigest,
    "journald": journald_predigest,
}


def _detect_domain(text: str) -> str | None:
    """Detect which domain module best fits the log text."""
    first_chunk = text[:5000]
    scores: dict[str, int] = {}
    for domain, keywords in _DOMAIN_SIGNATURES:
        score = sum(1 for kw in keywords if kw in first_chunk)
        if score > 0:
            scores[domain] = score
    if not scores:
        return None
    return max(scores, key=scores.get)


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
    if not text.strip():
        return text

    if domain is None:
        domain = _detect_domain(text)

    digester = _DIGESTERS.get(domain) if domain else None

    if digester:
        return digester(text)

    # Fallback: apply general-purpose cleaning
    from logdigest.core import predigest_document
    return predigest_document(text)


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
    result = _detect_domain(text)
    return result or "unknown"


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
    return entropy_report(texts)


def main():
    """Run the MCP server with stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
