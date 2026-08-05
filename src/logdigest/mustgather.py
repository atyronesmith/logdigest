"""OpenShift must-gather pre-digestion: cluster diagnostic noise removal.

Patterns, block detectors, and thresholds are loaded from
patterns/mustgather.json at import time.

Three levels of compression:
1. Line-level: classify lines as noise/signal via regex patterns
2. Block-level: collapse health checks, leader election, managedFields
3. Blob-level: truncate K8s API responses and large embedded data
"""

from __future__ import annotations

import re
from pathlib import Path

from logdigest.domain_base import (
    PatternSet,
    find_large_blobs,
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

_ps = PatternSet.load("mustgather")
NOISE_PATTERNS = _ps.noise_patterns
SIGNAL_PATTERNS = _ps.signal_patterns
K8S_BLOB_MARKERS = _ps.blob_markers

_ERROR_CONTEXT = _ps.error_context
_MAX_LINE_LEN = _ps.max_line_len
_STREAMING_THRESHOLD = _ps.file_streaming_threshold_mb * 1024 * 1024
_BLOCK_DETECTORS = _ps.block_detectors


def classify_line(line: str) -> str:
    """Classify a single log line as noise, signal, or unknown."""
    return _classify_line(line, NOISE_PATTERNS, SIGNAL_PATTERNS)


# ---------------------------------------------------------------------------
# Level 2: Block-level dedup detection
# ---------------------------------------------------------------------------

def find_health_check_blocks(lines: list[str]) -> list[dict]:
    """Find consecutive health check log lines."""
    blocks = []
    i = 0
    health_re = re.compile(r'(health[yz]|readyz|livez)', re.IGNORECASE)
    while i < len(lines):
        if health_re.search(lines[i]):
            start = i
            while i < len(lines) and health_re.search(lines[i]):
                i += 1
            if i - start >= 3:
                blocks.append({"start": start, "end": i, "count": i - start})
        else:
            i += 1
    return blocks


def find_leader_election_blocks(lines: list[str]) -> list[dict]:
    """Find consecutive leader election renewal lines."""
    blocks = []
    i = 0
    lease_re = re.compile(r"(renewed|acquired) lease")
    while i < len(lines):
        if lease_re.search(lines[i]):
            start = i
            while i < len(lines) and lease_re.search(lines[i]):
                i += 1
            if i - start >= 3:
                blocks.append({"start": start, "end": i, "count": i - start})
        else:
            i += 1
    return blocks


def find_managed_fields_blocks(lines: list[str]) -> list[dict]:
    """Find managedFields YAML blocks (multi-line noise)."""
    blocks = []
    i = 0
    start_re = re.compile(r"^\s*managedFields:")
    continuation_re = re.compile(r"^\s*(- manager:|operation:|fieldsType:|fieldsV1:|f:|time:|apiVersion:|\.\.\.|{})")
    while i < len(lines):
        if start_re.match(lines[i]):
            start = i
            i += 1
            while i < len(lines) and continuation_re.match(lines[i]):
                i += 1
            blocks.append({"start": start, "end": i, "count": i - start})
        else:
            i += 1
    return blocks


# ---------------------------------------------------------------------------
# Level 3: Large embedded data detection
# ---------------------------------------------------------------------------

def find_k8s_blobs(lines: list[str], min_chars: int = 500) -> list[dict]:
    """Find lines containing large K8s API response data."""
    return find_large_blobs(lines, min_chars=min_chars, markers=K8S_BLOB_MARKERS)


# ---------------------------------------------------------------------------
# Pre-digestion (all levels combined)
# ---------------------------------------------------------------------------

def predigest(text: str) -> str:
    """Apply all three levels of must-gather pre-digestion.

    Returns the compressed log text.
    """
    lines = text.splitlines()

    # Pre-scan: find lines near errors (keep context around them)
    error_lines: set[int] = set()
    for i, line in enumerate(lines):
        cls = classify_line(line)
        if cls.startswith("signal:"):
            for j in range(max(0, i - _ERROR_CONTEXT), min(len(lines), i + _ERROR_CONTEXT)):
                error_lines.add(j)

    output = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Level 2: Config-driven simple block detectors
        block_matched = False
        if i not in error_lines:
            for det in _BLOCK_DETECTORS:
                if det.match_regex.search(line):
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

        # Level 2: Collapse managedFields blocks
        if re.match(r"^\s*managedFields:", line) and i not in error_lines:
            start = i
            i += 1
            cont_re = re.compile(r"^\s*(- manager:|operation:|fieldsType:|fieldsV1:|f:|time:|apiVersion:|\.\.\.|{})")
            while i < len(lines) and cont_re.match(lines[i]):
                i += 1
            count = i - start
            if count >= 2:
                output.append("[MANAGED FIELDS: block omitted]")
                continue
            else:
                i = start

        cls = classify_line(line)

        # Level 1: Strip pure noise (unless near errors)
        if cls.startswith("noise:") and i not in error_lines:
            i += 1
            continue

        # Level 3: Truncate large K8s API responses
        if len(line) > _MAX_LINE_LEN and any(m in line for m in K8S_BLOB_MARKERS):
            kind_m = re.search(r"['\"]kind['\"]: ['\"](\w+)['\"]", line)
            name_m = re.search(r"['\"]name['\"]: ['\"]([\\w-]+)['\"]", line)
            ns_m = re.search(r"['\"]namespace['\"]: ['\"]([\\w-]+)['\"]", line)
            kind = kind_m.group(1) if kind_m else "resource"
            name = name_m.group(1) if name_m else "?"
            ns = ns_m.group(1) if ns_m else "?"
            output.append(f"[K8S API RESPONSE: {kind}/{name} in {ns} -- full response omitted]")
            i += 1
            continue

        output.append(line)
        i += 1

    return "\n".join(output)


def _blob_handler(line: str) -> str | None:
    """Replace large K8s API response lines with summaries."""
    if not any(m in line for m in K8S_BLOB_MARKERS):
        return None
    kind_m = re.search(r"['\"]kind['\"]: ['\"](\w+)['\"]", line)
    name_m = re.search(r"['\"]name['\"]: ['\"]([\\w-]+)['\"]", line)
    ns_m = re.search(r"['\"]namespace['\"]: ['\"]([\\w-]+)['\"]", line)
    kind = kind_m.group(1) if kind_m else "resource"
    name = name_m.group(1) if name_m else "?"
    ns = ns_m.group(1) if ns_m else "?"
    return f"[K8S API RESPONSE: {kind}/{name} in {ns} -- full response omitted]"


def predigest_file(filepath: Path) -> str:
    """Pre-digest a single must-gather log file.

    For small files, loads into memory. For large files (>50MB),
    uses streaming to avoid OOM.
    """
    size = filepath.stat().st_size
    if size > _STREAMING_THRESHOLD:
        return predigest_file_streaming(filepath)
    text = filepath.read_text(errors="replace")
    return predigest(text)


def predigest_file_streaming(filepath: Path) -> str:
    """Pre-digest a must-gather log file using streaming.

    Reads line-by-line to handle arbitrarily large files (multi-GB
    etcd logs, etc.) without loading the entire file into memory.
    """
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


def predigest_bundle(bundle_dir: Path) -> dict:
    """Pre-digest an extracted must-gather bundle directory.

    Walks the directory, applies per-file pre-digestion to text files.
    Uses streaming for files >50MB to avoid OOM on large pod logs.

    Returns:
        Dict with "files" (relative_path → compressed_text) and
        "skipped" (list of {"file": path, "reason": str} for files
        that could not be processed).
    """
    files = {}
    skipped = []
    for filepath in sorted(bundle_dir.rglob("*")):
        if not filepath.is_file():
            continue
        if filepath.suffix in (".gz", ".tar", ".zip", ".png", ".jpg", ".bin"):
            continue
        rel = str(filepath.relative_to(bundle_dir))
        try:
            size = filepath.stat().st_size
            if size == 0:
                continue
            result = predigest_file(filepath)
            if result.strip():
                files[rel] = result
        except OSError as e:
            skipped.append({"file": rel, "reason": str(e)})
        except UnicodeDecodeError as e:
            skipped.append({"file": rel, "reason": f"encoding: {e.encoding}"})

    return {"files": files, "skipped": skipped}


def analyze_log(filepath: Path) -> dict:
    """Analyze a single must-gather file and return statistics."""
    stats = _analyze_file(filepath, NOISE_PATTERNS, SIGNAL_PATTERNS)

    lines = filepath.read_text(errors="replace").splitlines()
    health_blocks = find_health_check_blocks(lines)
    leader_blocks = find_leader_election_blocks(lines)
    mf_blocks = find_managed_fields_blocks(lines)
    k8s_blobs = find_k8s_blobs(lines)

    total_chars = stats.get("total_chars", sum(len(line) for line in lines))
    blob_chars = sum(b["length"] for b in k8s_blobs)

    stats.update({
        "health_check_blocks": len(health_blocks),
        "health_check_lines": sum(b["count"] for b in health_blocks),
        "leader_election_blocks": len(leader_blocks),
        "leader_election_lines": sum(b["count"] for b in leader_blocks),
        "managed_fields_blocks": len(mf_blocks),
        "managed_fields_lines": sum(b["count"] for b in mf_blocks),
        "k8s_blobs": len(k8s_blobs),
        "k8s_blob_chars": blob_chars,
        "k8s_blob_pct": round(blob_chars / total_chars * 100, 1) if total_chars > 0 else 0,
    })
    stats.pop("classifications", None)
    return stats
