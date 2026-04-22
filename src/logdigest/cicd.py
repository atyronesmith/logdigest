"""CI/CD log pre-digestion: GitHub Actions, Jenkins, GitLab CI.

Patterns and thresholds are loaded from patterns/cicd.json at
import time, with hardcoded fallbacks if the JSON is missing.

Three levels of compression:
1. Line-level: classify lines as noise/signal via regex patterns
2. Block-level: collapse install blocks and container pull sequences
3. Blob-level: truncate large dependency trees
"""

from __future__ import annotations

import re
from pathlib import Path

from logdigest.domain_base import (
    PatternSet,
    classify_line as _classify_line,
    analyze_file as _analyze_file,
    find_large_blobs,
    predigest_stream,
)

# ---------------------------------------------------------------------------
# Load patterns and config from JSON (shipped with the package)
# ---------------------------------------------------------------------------

_ps = PatternSet.load("cicd")
if _ps is None:
    raise RuntimeError("Missing pattern file: patterns/cicd.json")

NOISE_PATTERNS = _ps.noise_patterns
SIGNAL_PATTERNS = _ps.signal_patterns

_ERROR_CONTEXT = _ps.error_context
_MAX_LINE_LEN = _ps.max_line_len
_STREAMING_THRESHOLD = _ps.file_streaming_threshold_mb * 1024 * 1024


def classify_line(line: str) -> str:
    """Classify a single log line as noise, signal, or unknown."""
    return _classify_line(line, NOISE_PATTERNS, SIGNAL_PATTERNS)


# ---------------------------------------------------------------------------
# CI system detection
# ---------------------------------------------------------------------------

def detect_ci_system(text: str) -> str | None:
    """Auto-detect CI system from log content.

    Examines the first 50 lines for system-specific markers.

    Returns:
        "github_actions", "jenkins", "gitlab_ci", or None.
    """
    lines = text.splitlines()[:50]
    joined = "\n".join(lines)

    if "##[group]" in joined or "::set-output" in joined or "GITHUB_" in joined:
        return "github_actions"
    if "[Pipeline]" in joined or "Started by" in joined or "Building in workspace" in joined:
        return "jenkins"
    if "Using Docker executor" in joined or "Getting source from" in joined or "gitlab-runner" in joined:
        return "gitlab_ci"

    return None


# ---------------------------------------------------------------------------
# Level 2: Block-level dedup detection
# ---------------------------------------------------------------------------

def find_install_blocks(lines: list[str]) -> list[dict]:
    """Find consecutive package installation lines."""
    blocks = []
    install_re = re.compile(
        r"(Collecting |Downloading .*\.whl|Installing collected|"
        r"Successfully installed|Using cached|Requirement already|"
        r"Building wheel|"
        r"Setting up |Unpacking |Selecting previously|Preparing to unpack|"
        r"Get:\d+|"
        r"Installed:|Installing\s*:|Verifying\s*:|"
        r"added \d+ packages|npm http)"
    )
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if install_re.search(stripped):
            start = i
            pkg_mgr = _detect_pkg_manager(stripped)
            while i < len(lines) and install_re.search(lines[i].strip()):
                i += 1
            if i - start >= 3:
                blocks.append({
                    "start": start,
                    "end": i,
                    "count": i - start,
                    "pkg_manager": pkg_mgr,
                })
        else:
            i += 1
    return blocks


def _detect_pkg_manager(line: str) -> str:
    """Detect which package manager is installing."""
    if any(kw in line for kw in ("Collecting", "Downloading", "pip", "wheel", "Requirement")):
        return "pip"
    if any(kw in line for kw in ("npm", "added")):
        return "npm"
    if any(kw in line for kw in ("Setting up", "Unpacking", "Get:", "apt")):
        return "apt"
    if any(kw in line for kw in ("Installed:", "Installing", "Verifying", "yum")):
        return "yum"
    return "unknown"


def find_container_pull_blocks(lines: list[str]) -> list[dict]:
    """Find consecutive container image pull lines."""
    blocks = []
    pull_re = re.compile(
        r"(Pulling from|Digest:|Status: Downloaded|"
        r"Waiting|Downloading|Extracting|Pull complete|Already exists|"
        r"[a-f0-9]{12}: (Waiting|Downloading|Extracting|Pull complete))"
    )
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if pull_re.search(stripped):
            start = i
            image = ""
            m = re.search(r"Pulling from (.+)", stripped)
            if m:
                image = m.group(1)
            while i < len(lines) and pull_re.search(lines[i].strip()):
                i += 1
            if i - start >= 3:
                blocks.append({
                    "start": start,
                    "end": i,
                    "count": i - start,
                    "image": image,
                })
        else:
            i += 1
    return blocks


# ---------------------------------------------------------------------------
# Pre-digestion (all levels combined)
# ---------------------------------------------------------------------------

def predigest(text: str, ci_system: str | None = None) -> str:
    """Apply all three levels of CI/CD log pre-digestion.

    Args:
        text: Raw CI/CD log content.
        ci_system: Optional CI system hint ("github_actions", "jenkins",
            "gitlab_ci"). Auto-detected if not provided.

    Returns:
        Compressed log text.
    """
    if ci_system is None:
        ci_system = detect_ci_system(text)

    lines = text.splitlines()

    # Pre-scan: find lines near errors
    error_lines: set[int] = set()
    for i, line in enumerate(lines):
        cls = classify_line(line)
        if cls.startswith("signal:"):
            for j in range(max(0, i - _ERROR_CONTEXT), min(len(lines), i + _ERROR_CONTEXT)):
                error_lines.add(j)

    output = []
    i = 0

    install_re = re.compile(
        r"(Collecting |Downloading .*\.whl|Installing collected|"
        r"Successfully installed|Using cached|Requirement already|"
        r"Building wheel|"
        r"Setting up |Unpacking |Selecting previously|Preparing to unpack|"
        r"Get:\d+|"
        r"Installed:|Installing\s*:|Verifying\s*:|"
        r"added \d+ packages|npm http)"
    )
    pull_re = re.compile(
        r"(Pulling from|Digest:|Status: Downloaded|"
        r"[a-f0-9]{12}: (Waiting|Downloading|Extracting|Pull complete))"
    )
    layer_re = re.compile(
        r"^\s*(Waiting|Downloading|Extracting|Pull complete|Already exists)\s*$"
    )

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Level 2: Collapse install blocks
        if install_re.search(stripped) and i not in error_lines:
            start = i
            pkg_mgr = _detect_pkg_manager(stripped)
            while i < len(lines) and install_re.search(lines[i].strip()):
                i += 1
            count = i - start
            if count >= 3:
                output.append(f"[DEPENDENCY INSTALL: {count} lines via {pkg_mgr}]")
                continue
            else:
                i = start

        # Level 2: Collapse container pull blocks
        if (pull_re.search(stripped) or layer_re.match(stripped)) and i not in error_lines:
            start = i
            image = ""
            m = re.search(r"Pulling from (.+)", stripped)
            if m:
                image = m.group(1)
            while i < len(lines) and (
                pull_re.search(lines[i].strip()) or layer_re.match(lines[i].strip())
            ):
                if not image:
                    m = re.search(r"Pulling from (.+)", lines[i].strip())
                    if m:
                        image = m.group(1)
                i += 1
            count = i - start
            if count >= 3:
                img_str = f" {image}" if image else ""
                output.append(f"[CONTAINER PULL:{img_str} {count} lines]")
                continue
            else:
                i = start

        cls = classify_line(line)

        # Level 1: Strip pure noise (unless near errors)
        if cls.startswith("noise:") and i not in error_lines:
            i += 1
            continue

        # Level 3: Truncate large dependency trees / build stats
        if len(line) > 500:
            if "node_modules" in line or "site-packages" in line:
                output.append("[DEPENDENCY TREE: large output omitted]")
                i += 1
                continue

        output.append(line)
        i += 1

    return "\n".join(output)


def predigest_file(filepath: Path) -> str:
    """Pre-digest a single CI/CD log file.

    For small files, loads into memory. For large files (>50MB),
    uses streaming to avoid OOM.
    """
    size = filepath.stat().st_size
    if size > _STREAMING_THRESHOLD:
        return predigest_file_streaming(filepath)
    text = filepath.read_text(errors="replace")
    return predigest(text)


def _blob_handler(line: str) -> str | None:
    """Replace large dependency tree lines with summaries."""
    if "node_modules" in line or "site-packages" in line:
        return "[DEPENDENCY TREE: large output omitted]"
    return None


def predigest_file_streaming(filepath: Path) -> str:
    """Pre-digest a CI/CD log file using streaming."""
    output: list[str] = []
    with open(filepath, errors="replace") as f:
        predigest_stream(
            source=f,
            noise_patterns=NOISE_PATTERNS,
            signal_patterns=SIGNAL_PATTERNS,
            sink=output,
            error_context=_ERROR_CONTEXT,
            max_line_len=_MAX_LINE_LEN,
            blob_handler=_blob_handler,
        )
    return "\n".join(output)


def analyze_log(filepath: Path) -> dict:
    """Analyze a single CI/CD log file and return statistics."""
    stats = _analyze_file(filepath, NOISE_PATTERNS, SIGNAL_PATTERNS)

    lines = filepath.read_text(errors="replace").splitlines()
    install_blocks = find_install_blocks(lines)
    pull_blocks = find_container_pull_blocks(lines)

    stats.update({
        "ci_system": detect_ci_system("\n".join(lines[:50])),
        "install_blocks": len(install_blocks),
        "install_lines": sum(b["count"] for b in install_blocks),
        "container_pull_blocks": len(pull_blocks),
        "container_pull_lines": sum(b["count"] for b in pull_blocks),
    })
    stats.pop("classifications", None)
    return stats
