"""Shared machinery for domain-specific pre-digestion modules.

Extracts the common patterns from domain modules (classify_line,
analyze_lines, find_large_blobs) so each new domain only needs to
define its NOISE_PATTERNS, SIGNAL_PATTERNS, and domain-specific
block/blob detectors.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

# Type alias for pattern lists used across all domain modules
PatternList = list[tuple[re.Pattern, str]]


def classify_line(
    line: str,
    noise_patterns: PatternList,
    signal_patterns: PatternList,
) -> str:
    """Classify a single log line as noise, signal, or unknown.

    Args:
        line: Raw log line.
        noise_patterns: List of (compiled_regex, label) for noise.
        signal_patterns: List of (compiled_regex, label) for signal.

    Returns:
        "noise:<label>", "signal:<label>", or "unknown".
    """
    stripped = line.strip()
    for pattern, label in noise_patterns:
        if pattern.search(stripped):
            return f"noise:{label}"
    for pattern, label in signal_patterns:
        if pattern.search(stripped):
            return f"signal:{label}"
    return "unknown"


def analyze_lines(
    lines: list[str],
    noise_patterns: PatternList,
    signal_patterns: PatternList,
) -> dict:
    """Produce noise/signal/unknown statistics for a line list.

    Returns dict with total_lines, noise_lines, signal_lines,
    unknown_lines, noise_pct, and per-label classification counts.
    """
    total = len(lines)
    classifications: Counter[str] = Counter()
    for line in lines:
        classifications[classify_line(line, noise_patterns, signal_patterns)] += 1

    noise_count = sum(v for k, v in classifications.items() if k.startswith("noise:"))
    signal_count = sum(v for k, v in classifications.items() if k.startswith("signal:"))

    return {
        "total_lines": total,
        "noise_lines": noise_count,
        "signal_lines": signal_count,
        "unknown_lines": classifications.get("unknown", 0),
        "noise_pct": round(noise_count / total * 100, 1) if total > 0 else 0,
        "classifications": dict(classifications),
    }


def analyze_file(
    filepath: Path,
    noise_patterns: PatternList,
    signal_patterns: PatternList,
) -> dict:
    """Analyze a single file and return statistics."""
    lines = filepath.read_text(errors="replace").splitlines()
    stats = analyze_lines(lines, noise_patterns, signal_patterns)
    stats["file"] = filepath.name
    total_chars = sum(len(line) for line in lines)
    stats["total_chars"] = total_chars
    return stats


def find_large_blobs(
    lines: list[str],
    min_chars: int = 500,
    markers: list[str] | None = None,
) -> list[dict]:
    """Find lines containing large embedded data.

    Args:
        lines: Log lines to scan.
        min_chars: Minimum line length to consider.
        markers: Substrings that identify known blob types.
            Each marker is checked with `in line`. If any marker
            matches, the blob is tagged with that marker as type.
            Lines exceeding min_chars without a marker match are
            tagged as "long_line".

    Returns:
        List of dicts with line index, length, and type.
    """
    if markers is None:
        markers = []

    blobs = []
    for i, line in enumerate(lines):
        if len(line) <= min_chars:
            continue
        blob_type = "long_line"
        for marker in markers:
            if marker in line:
                blob_type = marker
                break
        blobs.append({"line": i, "length": len(line), "type": blob_type})
    return blobs
