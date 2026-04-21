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


def predigest_stream(
    source,
    noise_patterns: PatternList,
    signal_patterns: PatternList,
    sink,
    error_context: int = 3,
    max_line_len: int = 500,
    blob_handler=None,
) -> dict:
    """Streaming pre-digestion for arbitrarily large files.

    Reads lines from source (file object or iterable), writes to sink
    (file object or list). Uses a bounded buffer for error context
    instead of loading the entire file.

    Args:
        source: Iterable of lines (e.g., open file handle).
        noise_patterns: Noise pattern list for classify_line.
        signal_patterns: Signal pattern list for classify_line.
        sink: Writable file object or list to append output lines.
        error_context: Lines of context to preserve around errors.
        max_line_len: Lines longer than this are checked for blobs.
        blob_handler: Optional callable(line) -> str|None. If it
            returns a string, that replaces the line. If None, long
            lines pass through.

    Returns:
        Dict with total_lines, noise_lines, signal_lines, kept_lines.
    """
    write = sink.append if isinstance(sink, list) else lambda l: sink.write(l + "\n")

    buffer: list[tuple[str, str]] = []  # (line, classification)
    stats = {"total_lines": 0, "noise_lines": 0, "signal_lines": 0, "kept_lines": 0}

    def _flush_buffer(up_to: int, error_at: int | None = None) -> None:
        """Flush buffer entries, keeping context around errors."""
        for idx, (line, cls) in enumerate(buffer[:up_to]):
            keep = False
            if cls.startswith("signal:"):
                keep = True
            elif error_at is not None and abs(idx - error_at) <= error_context:
                keep = True
            elif not cls.startswith("noise:"):
                keep = True

            if keep:
                if len(line) > max_line_len and blob_handler:
                    replacement = blob_handler(line)
                    if replacement is not None:
                        write(replacement)
                        stats["kept_lines"] += 1
                        continue
                write(line)
                stats["kept_lines"] += 1
        del buffer[:up_to]

    window = error_context * 2 + 1

    for raw_line in source:
        line = raw_line.rstrip("\n").rstrip("\r")
        stats["total_lines"] += 1
        cls = classify_line(line, noise_patterns, signal_patterns)

        if cls.startswith("noise:"):
            stats["noise_lines"] += 1
        elif cls.startswith("signal:"):
            stats["signal_lines"] += 1

        buffer.append((line, cls))

        if cls.startswith("signal:"):
            _flush_buffer(len(buffer), error_at=len(buffer) - 1)
        elif len(buffer) > window * 2:
            _flush_buffer(len(buffer) - window)

    if buffer:
        _flush_buffer(len(buffer))

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
