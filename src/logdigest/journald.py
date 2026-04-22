"""Systemd journal pre-digestion: journalctl noise removal.

Patterns, block detectors, and thresholds are loaded from
patterns/journald.json at import time, with hardcoded fallbacks.

Three levels of compression:
1. Line-level: classify lines as noise/signal via regex patterns
2. Block-level: collapse service restart loops, audit bursts
3. Blob-level: truncate large stack traces
"""

from __future__ import annotations

import re
from pathlib import Path

from logdigest.domain_base import (
    PatternSet,
    classify_line as _classify_line,
    analyze_file as _analyze_file,
    predigest_stream,
)

# ---------------------------------------------------------------------------
# Load patterns and config from JSON (shipped with the package)
# ---------------------------------------------------------------------------

_ps = PatternSet.load("journald")
if _ps is None:
    raise RuntimeError("Missing pattern file: patterns/journald.json")

NOISE_PATTERNS = _ps.noise_patterns
SIGNAL_PATTERNS = _ps.signal_patterns

_ERROR_CONTEXT = _ps.error_context
_MAX_LINE_LEN = _ps.max_line_len
_STREAMING_THRESHOLD = _ps.file_streaming_threshold_mb * 1024 * 1024
_BLOCK_DETECTORS = _ps.block_detectors


def classify_line(line: str) -> str:
    """Classify a single journal line as noise, signal, or unknown."""
    return _classify_line(line, NOISE_PATTERNS, SIGNAL_PATTERNS)


# ---------------------------------------------------------------------------
# Level 2: Block-level dedup detection
# ---------------------------------------------------------------------------

def find_service_restart_loops(lines: list[str]) -> list[dict]:
    """Find repeated start/stop cycles for the same service."""
    blocks = []
    service_re = re.compile(r"(Started|Stopped|Starting|Stopping|Failed to start) (.+?)[\.\s]*$")
    i = 0
    while i < len(lines):
        m = service_re.search(lines[i].strip())
        if m:
            service = m.group(2)
            start = i
            count = 0
            while i < len(lines):
                m2 = service_re.search(lines[i].strip())
                if m2 and m2.group(2) == service:
                    count += 1
                    i += 1
                else:
                    break
            if count >= 4:  # at least 2 full start/stop cycles
                blocks.append({
                    "start": start,
                    "end": i,
                    "count": count,
                    "service": service,
                })
            elif count > 1:
                i = start + 1
            # else single match, advance normally
        else:
            i += 1
    return blocks


def find_audit_bursts(lines: list[str]) -> list[dict]:
    """Find consecutive audit log lines for the same operation."""
    blocks = []
    audit_re = re.compile(r"audit\[\d+\]:")
    i = 0
    while i < len(lines):
        if audit_re.search(lines[i]):
            start = i
            while i < len(lines) and audit_re.search(lines[i]):
                i += 1
            if i - start >= 5:
                blocks.append({"start": start, "end": i, "count": i - start})
        else:
            i += 1
    return blocks


# ---------------------------------------------------------------------------
# Pre-digestion (all levels combined)
# ---------------------------------------------------------------------------

def predigest(text: str, keep_timestamps: bool = False) -> str:
    """Apply all three levels of journal pre-digestion.

    Args:
        text: Raw journalctl output.
        keep_timestamps: If False (default), strip the standard
            journal timestamp prefix (date portion only, keep time).

    Returns:
        Compressed journal text.
    """
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

    service_re = re.compile(r"(Started|Stopped|Starting|Stopping|Failed to start) (.+?)[\.\s]*$")

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Level 2: Collapse service restart loops (complex — stays as code)
        m = service_re.search(stripped)
        if m and i not in error_lines:
            service = m.group(2)
            start = i
            count = 0
            while i < len(lines):
                m2 = service_re.search(lines[i].strip())
                if m2 and m2.group(2) == service:
                    count += 1
                    i += 1
                else:
                    break
            if count >= 4:
                output.append(f"[SERVICE RESTART LOOP: {service}, {count} events]")
                continue
            else:
                i = start

        # Level 2: Config-driven simple block detectors
        block_matched = False
        if i not in error_lines:
            for det in _BLOCK_DETECTORS:
                if det.match_regex.search(stripped):
                    count = 0
                    while i + count < len(lines) and det.match_regex.search(lines[i + count]):
                        count += 1
                    if count >= det.min_count:
                        output.append(det.summary_template.format(count=count))
                        i += count
                        block_matched = True
                    break
        if block_matched:
            continue

        cls = classify_line(line)

        # Level 1: Strip pure noise (unless near errors)
        if cls.startswith("noise:") and i not in error_lines:
            i += 1
            continue

        # Level 3: Truncate large stack traces (keep first and last 5 lines)
        if "Call Trace:" in stripped:
            trace_start = i
            trace_lines = [line]
            i += 1
            while i < len(lines) and (
                lines[i].strip().startswith("[<") or
                lines[i].strip().startswith("?") or
                lines[i].strip().startswith("+") or
                re.match(r"^\s*\w+\+0x", lines[i].strip())
            ):
                trace_lines.append(lines[i])
                i += 1
            if len(trace_lines) > 12:
                output.extend(trace_lines[:6])
                output.append(f"  [... {len(trace_lines) - 10} frames omitted ...]")
                output.extend(trace_lines[-5:])
            else:
                output.extend(trace_lines)
            continue

        # Optional: strip date prefix from timestamps (keep HH:MM:SS)
        if not keep_timestamps:
            # Standard journalctl format: "Mon DD HH:MM:SS hostname ..."
            ts_m = re.match(r"^\w{3}\s+\d{1,2}\s+(\d{2}:\d{2}:\d{2})\s+(.+)", line)
            if ts_m:
                line = f"{ts_m.group(1)} {ts_m.group(2)}"

        output.append(line)
        i += 1

    return "\n".join(output)


def predigest_file(filepath: Path) -> str:
    """Pre-digest a single journal export file.

    For small files, loads into memory. For large files (>50MB),
    uses streaming to avoid OOM.
    """
    size = filepath.stat().st_size
    if size > _STREAMING_THRESHOLD:
        return predigest_file_streaming(filepath)
    text = filepath.read_text(errors="replace")
    return predigest(text)


def predigest_file_streaming(filepath: Path) -> str:
    """Pre-digest a journal file using streaming."""
    output: list[str] = []
    with open(filepath, errors="replace") as f:
        predigest_stream(
            source=f,
            noise_patterns=NOISE_PATTERNS,
            signal_patterns=SIGNAL_PATTERNS,
            sink=output,
            error_context=_ERROR_CONTEXT,
            max_line_len=_MAX_LINE_LEN,
        )
    return "\n".join(output)


def analyze_log(filepath: Path) -> dict:
    """Analyze a single journal file and return statistics."""
    stats = _analyze_file(filepath, NOISE_PATTERNS, SIGNAL_PATTERNS)

    lines = filepath.read_text(errors="replace").splitlines()
    restart_blocks = find_service_restart_loops(lines)
    audit_blocks = find_audit_bursts(lines)

    stats.update({
        "service_restart_loops": len(restart_blocks),
        "service_restart_events": sum(b["count"] for b in restart_blocks),
        "audit_bursts": len(audit_blocks),
        "audit_burst_lines": sum(b["count"] for b in audit_blocks),
    })
    stats.pop("classifications", None)
    return stats
