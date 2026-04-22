"""OpenShift must-gather pre-digestion: cluster diagnostic noise removal.

Must-gather bundles contain pod logs, event JSONs, resource YAMLs, and
operator logs. 80-90% is boilerplate — health checks, leader election
renewals, informer syncs, and full K8s API response blobs.

Three levels of compression:
1. Line-level: strip noise lines (health checks, leader election, informer sync)
2. Block-level: collapse repeated patterns into summaries
3. Blob-level: truncate K8s API responses, managedFields, large ConfigMaps
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
# Level 1: Line-level classification
# ---------------------------------------------------------------------------

_FALLBACK_NOISE = [
    # Health checks
    (re.compile(r'"GET /health[yz]'), "healthz"),
    (re.compile(r'"GET /readyz'), "readyz"),
    (re.compile(r'"GET /livez'), "livez"),
    (re.compile(r"health check (passed|ok|200)", re.IGNORECASE), "healthcheck"),
    # Leader election
    (re.compile(r"successfully (acquired|renewed) lease"), "leader_election"),
    (re.compile(r"lock is held by .+ and has not yet expired"), "leader_election"),
    # Informer / reflector
    (re.compile(r"caches populated"), "informer"),
    (re.compile(r"(Starting|Stopping) shared informer"), "informer"),
    (re.compile(r"Listing and watching"), "informer"),
    (re.compile(r"Watch close"), "informer"),
    (re.compile(r"watch chan closed"), "informer"),
    # Client-go throttling
    (re.compile(r"Throttling request"), "throttle"),
    (re.compile(r"client rate limiter"), "throttle"),
    # Reconciliation no-ops
    (re.compile(r"Reconcil(ing|ed).*no changes", re.IGNORECASE), "reconcile_noop"),
    (re.compile(r"skipping reconcil", re.IGNORECASE), "reconcile_skip"),
    # YAML metadata noise
    (re.compile(r"^\s*resourceVersion:"), "yaml_meta"),
    (re.compile(r"^\s*uid:"), "yaml_meta"),
    (re.compile(r"^\s*creationTimestamp:"), "yaml_meta"),
    (re.compile(r"^\s*selfLink:"), "yaml_meta"),
    (re.compile(r"^\s*managedFields:"), "managed_fields"),
    (re.compile(r"^\s*- manager:"), "managed_fields"),
    (re.compile(r"^\s*operation:"), "managed_fields"),
    (re.compile(r"^\s*fieldsType:"), "managed_fields"),
    (re.compile(r"^\s*fieldsV1:"), "managed_fields"),
    (re.compile(r'^\s*f:'), "managed_fields"),
    # Event recorder boilerplate
    (re.compile(r"Event\(v1\.ObjectReference"), "event_recorder"),
    # TLS / cert rotation
    (re.compile(r"certificate signed by unknown authority"), "cert_bootstrap"),
    (re.compile(r"TLS handshake error"), "tls_noise"),
    (re.compile(r"serving cert rotation"), "cert_rotation"),
    # Blank / separator lines
    (re.compile(r"^\s*$"), "blank"),
    (re.compile(r"^-{5,}$"), "separator"),
    (re.compile(r"^={5,}$"), "separator"),
    # Operator SDK / controller-runtime
    (re.compile(r"controller-runtime.*Starting EventSource"), "controller_rt"),
    (re.compile(r"controller-runtime.*Starting Controller"), "controller_rt"),
    (re.compile(r"controller-runtime.*Starting workers"), "controller_rt"),
    # API request logging (non-error)
    (re.compile(r'"(GET|LIST|WATCH)" .* 200'), "api_200"),
    # Prometheus metrics scrape
    (re.compile(r"GET /metrics"), "metrics_scrape"),
]

_FALLBACK_SIGNAL = [
    # Errors
    (re.compile(r"level=error", re.IGNORECASE), "error"),
    (re.compile(r"level=fatal", re.IGNORECASE), "fatal"),
    (re.compile(r"\bE\d{4}\b"), "klog_error"),
    (re.compile(r"^E[0-9]{4} "), "klog_error"),
    # Panics / crashes
    (re.compile(r"panic:"), "panic"),
    (re.compile(r"goroutine \d+ \["), "goroutine"),
    (re.compile(r"signal SIGSEGV"), "sigsegv"),
    # OOM
    (re.compile(r"OOMKilled"), "oom"),
    (re.compile(r"Out of memory", re.IGNORECASE), "oom"),
    (re.compile(r"memory cgroup out of memory"), "oom"),
    # Pod failures
    (re.compile(r"CrashLoopBackOff"), "crashloop"),
    (re.compile(r"Back-off restarting"), "crashloop"),
    (re.compile(r"ErrImagePull"), "image_pull"),
    (re.compile(r"ImagePullBackOff"), "image_pull"),
    (re.compile(r"Evicted"), "eviction"),
    # Node conditions
    (re.compile(r"NotReady"), "node_not_ready"),
    (re.compile(r"node condition.*pressure", re.IGNORECASE), "node_pressure"),
    # Operator degraded
    (re.compile(r"Degraded.*True", re.IGNORECASE), "degraded"),
    (re.compile(r"Available.*False", re.IGNORECASE), "unavailable"),
    # API errors
    (re.compile(r'"(GET|LIST|WATCH|PUT|POST|DELETE)" .* (4\d\d|5\d\d)'), "api_error"),
    # Warning events
    (re.compile(r"type.*Warning", re.IGNORECASE), "warning_event"),
]

_ps = PatternSet.load("mustgather")
NOISE_PATTERNS = _ps.noise_patterns if _ps else _FALLBACK_NOISE
SIGNAL_PATTERNS = _ps.signal_patterns if _ps else _FALLBACK_SIGNAL


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

K8S_BLOB_MARKERS = [
    "'metadata':",
    '"metadata":',
    "'spec':",
    '"spec":',
    "fieldsV1",
    "managedFields",
]


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
            for j in range(max(0, i - 3), min(len(lines), i + 3)):
                error_lines.add(j)

    output = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Level 2: Collapse health check blocks
        health_re = re.compile(r'(health[yz]|readyz|livez)', re.IGNORECASE)
        if health_re.search(line) and i not in error_lines:
            count = 0
            while i < len(lines) and health_re.search(lines[i]):
                count += 1
                i += 1
            if count >= 3:
                output.append(f"[HEALTH CHECKS: {count} lines, all OK]")
                continue
            else:
                i -= count

        # Level 2: Collapse leader election renewals
        lease_re = re.compile(r"(renewed|acquired) lease")
        if lease_re.search(line) and i not in error_lines:
            count = 0
            while i < len(lines) and lease_re.search(lines[i]):
                count += 1
                i += 1
            if count >= 3:
                output.append(f"[LEADER ELECTION: {count} renewals]")
                continue
            else:
                i -= count

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
        if len(line) > 500 and any(m in line for m in K8S_BLOB_MARKERS):
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
    if size > 50 * 1024 * 1024:
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
            error_context=3,
            max_line_len=500,
            blob_handler=_blob_handler,
        )
    return "\n".join(output)


def predigest_bundle(bundle_dir: Path) -> dict[str, str]:
    """Pre-digest an extracted must-gather bundle directory.

    Walks the directory, applies per-file pre-digestion to text files,
    and returns a dict of {relative_path: compressed_text}.

    Uses streaming for files >50MB to avoid OOM on large pod logs.
    """
    results = {}
    for filepath in sorted(bundle_dir.rglob("*")):
        if not filepath.is_file():
            continue
        if filepath.suffix in (".gz", ".tar", ".zip", ".png", ".jpg", ".bin"):
            continue
        try:
            size = filepath.stat().st_size
            if size == 0:
                continue
            result = predigest_file(filepath)
            if result.strip():
                rel = str(filepath.relative_to(bundle_dir))
                results[rel] = result
        except (OSError, UnicodeDecodeError):
            continue

    return results


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
