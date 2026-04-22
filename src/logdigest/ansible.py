"""Ansible log pre-digestion: domain-specific noise removal.

Patterns and thresholds are loaded from patterns/ansible.json at
import time.

Three levels of compression:
1. Line-level: classify lines as noise/signal via regex patterns
2. Block-level: collapse retry blocks and item loops into summaries
3. Blob-level: truncate large embedded data (K8s API responses, base64)
"""

import re
from pathlib import Path

from logdigest.domain_base import (
    PatternSet,
    predigest_stream,
)
from logdigest.domain_base import (
    analyze_file as _analyze_file,
)
from logdigest.domain_base import (
    classify_line as _classify_line,
)

# ---------------------------------------------------------------------------
# Load patterns and config from JSON (shipped with the package)
# ---------------------------------------------------------------------------

_ps = PatternSet.load("ansible")
NOISE_PATTERNS = _ps.noise_patterns
SIGNAL_PATTERNS = _ps.signal_patterns
ANSIBLE_BLOB_MARKERS = _ps.blob_markers

_ERROR_CONTEXT = _ps.error_context
_MAX_LINE_LEN = _ps.max_line_len
_STREAMING_THRESHOLD = _ps.file_streaming_threshold_mb * 1024 * 1024


def classify_line(line: str) -> str:
    """Classify a single log line as noise, signal, or unknown."""
    return _classify_line(line, NOISE_PATTERNS, SIGNAL_PATTERNS)


# ---------------------------------------------------------------------------
# Level 2: Block-level dedup detection
# ---------------------------------------------------------------------------

def find_retry_blocks(lines: list[str]) -> list[dict]:
    """Find consecutive retry lines and summarize them."""
    blocks = []
    i = 0
    while i < len(lines):
        if "FAILED - RETRYING:" in lines[i]:
            start = i
            m = re.search(r"RETRYING: \[.*?\]: (.+?) \(\d+ retries", lines[i])
            task = m.group(1) if m else "unknown"
            while i < len(lines) and "FAILED - RETRYING:" in lines[i]:
                i += 1
            blocks.append({
                "start": start,
                "end": i,
                "count": i - start,
                "task": task,
            })
        else:
            i += 1
    return blocks


def find_repeated_ok_blocks(lines: list[str]) -> list[dict]:
    """Find consecutive ok/changed lines for the same task across items."""
    blocks = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if re.match(r"^(ok|changed): \[.+?\] => \(item=", stripped):
            start = i
            while i < len(lines) and re.match(
                r"^(ok|changed): \[.+?\] => \(item=", lines[i].strip()
            ):
                i += 1
            if i - start >= 3:
                blocks.append({
                    "start": start,
                    "end": i,
                    "count": i - start,
                    "type": "item_loop",
                })
        else:
            i += 1
    return blocks


# ---------------------------------------------------------------------------
# Level 3: Large embedded data detection
# ---------------------------------------------------------------------------

def find_large_json_blobs(lines: list[str], min_chars: int = 500) -> list[dict]:
    """Find lines containing large JSON/dict blobs."""
    blobs = []
    for i, line in enumerate(lines):
        if len(line) > min_chars and (
            "'metadata':" in line
            or '"metadata":' in line
            or "'spec':" in line
            or '"spec":' in line
            or "fieldsV1" in line
        ):
            blobs.append({"line": i, "length": len(line), "type": "k8s_api_response"})
        elif len(line) > min_chars and ("REDACTED" in line or "Base64" in line):
            blobs.append({"line": i, "length": len(line), "type": "redacted_secret"})
        elif len(line) > min_chars:
            blobs.append({"line": i, "length": len(line), "type": "long_line"})
    return blobs


# ---------------------------------------------------------------------------
# Pre-digestion (all levels combined)
# ---------------------------------------------------------------------------

def predigest(text: str) -> str:
    """Apply all three levels of Ansible log pre-digestion.

    Returns the compressed log text.
    """
    lines = text.splitlines()

    # Pre-scan: find which lines are near errors (keep those TASK headers)
    error_lines: set[int] = set()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if (stripped.startswith("fatal:") or stripped.startswith("FAILED!")
                or stripped.startswith("failed:") or "UNREACHABLE!" in stripped):
            for j in range(max(0, i - _ERROR_CONTEXT), min(len(lines), i + _ERROR_CONTEXT)):
                error_lines.add(j)

    output = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Level 2: Collapse retry blocks
        if "FAILED - RETRYING:" in stripped:
            m = re.search(r"RETRYING: \[(.+?)\]: (.+?) \((\d+) retries", stripped)
            host = m.group(1) if m else "?"
            task = m.group(2) if m else "?"
            count = 0
            while i < len(lines) and "FAILED - RETRYING:" in lines[i]:
                count += 1
                i += 1
            succeeded = i < len(lines) and lines[i].strip().startswith("ok:")
            status = "then succeeded" if succeeded else "still failing"
            output.append(f"[RETRY: {task} on {host}, {count} retries, {status}]")
            continue

        # Level 2: Collapse item loops
        if re.match(r"^(ok|changed): \[.+?\] => \(item=", stripped):
            action = "ok" if stripped.startswith("ok:") else "changed"
            count = 0
            while i < len(lines) and re.match(
                r"^(ok|changed): \[.+?\] => \(item=", lines[i].strip()
            ):
                count += 1
                i += 1
            if count >= 3:
                output.append(f"[{action}: {count} items processed]")
                continue
            else:
                i -= count

        cls = classify_line(line)

        # Level 1: Strip pure noise
        if cls.startswith("noise:"):
            i += 1
            continue

        # Level 1: Strip TASK headers not near errors
        if cls == "signal:task_header" and i not in error_lines:
            i += 1
            continue

        # Level 3: Truncate huge K8s API responses
        if len(line) > _MAX_LINE_LEN and any(m in line for m in ANSIBLE_BLOB_MARKERS):
            m = re.search(r"'kind': '(\w+)'", line) or re.search(r'"kind": "(\w+)"', line)
            kind = m.group(1) if m else "resource"
            m = re.search(r"'name': '([\w-]+)'", line) or re.search(r'"name": "([\w-]+)"', line)
            name = m.group(1) if m else "?"
            m = re.search(r"'namespace': '([\w-]+)'", line) or re.search(r'"namespace": "([\w-]+)"', line)
            ns = m.group(1) if m else "?"
            output.append(f"[K8S API RESPONSE: {kind}/{name} in {ns} — full response omitted]")
            i += 1
            continue

        output.append(line)
        i += 1

    return "\n".join(output)


def _blob_handler(line: str) -> str | None:
    """Replace large K8s API response lines with summaries."""
    if not any(m in line for m in ANSIBLE_BLOB_MARKERS):
        return None
    m = re.search(r"'kind': '(\w+)'", line) or re.search(r'"kind": "(\w+)"', line)
    kind = m.group(1) if m else "resource"
    m = re.search(r"'name': '([\w-]+)'", line) or re.search(r'"name": "([\w-]+)"', line)
    name = m.group(1) if m else "?"
    m = re.search(r"'namespace': '([\w-]+)'", line) or re.search(r'"namespace": "([\w-]+)"', line)
    ns = m.group(1) if m else "?"
    return f"[K8S API RESPONSE: {kind}/{name} in {ns} — full response omitted]"


def predigest_file(filepath: Path) -> str:
    """Pre-digest a single Ansible log file.

    For small files, loads into memory. For large files (>50MB),
    uses streaming to avoid OOM.
    """
    size = filepath.stat().st_size
    if size > _STREAMING_THRESHOLD:
        return predigest_file_streaming(filepath)
    text = filepath.read_text(errors="replace")
    return predigest(text)


def predigest_file_streaming(filepath: Path) -> str:
    """Pre-digest an Ansible log file using streaming."""
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
    """Analyze a single log file and return statistics."""
    # Base stats from shared machinery
    stats = _analyze_file(filepath, NOISE_PATTERNS, SIGNAL_PATTERNS)

    # Domain-specific block/blob analysis
    lines = filepath.read_text(errors="replace").splitlines()
    retry_blocks = find_retry_blocks(lines)
    item_blocks = find_repeated_ok_blocks(lines)
    json_blobs = find_large_json_blobs(lines)

    total_chars = stats.get("total_chars", sum(len(line) for line in lines))
    blob_chars = sum(b["length"] for b in json_blobs)

    stats.update({
        "retry_blocks": len(retry_blocks),
        "retry_lines": sum(b["count"] for b in retry_blocks),
        "item_loop_blocks": len(item_blocks),
        "item_loop_lines": sum(b["count"] for b in item_blocks),
        "json_blobs": len(json_blobs),
        "json_blob_chars": blob_chars,
        "json_blob_pct": round(blob_chars / total_chars * 100, 1) if total_chars > 0 else 0,
    })
    # Remove classifications detail (not in original API)
    stats.pop("classifications", None)
    return stats
