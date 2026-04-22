"""Systemd journal pre-digestion: system log noise removal.

journalctl output has per-entry boilerplate (timestamp, hostname, PID,
facility) and massive noise from routine system operations — DHCP
renewals, session management, cron execution, kernel informational.

Three levels of compression:
1. Line-level: strip noise lines (routine kernel, systemd, audit)
2. Block-level: collapse service restart loops, audit bursts
3. Blob-level: truncate large kernel dmesg dumps, stack traces
"""

from __future__ import annotations

import re
from pathlib import Path

from logdigest.domain_base import (
    BlockConfig,
    PatternSet,
    classify_line as _classify_line,
    analyze_file as _analyze_file,
    predigest_stream,
)

# ---------------------------------------------------------------------------
# Level 1: Line-level classification
# ---------------------------------------------------------------------------

_FALLBACK_NOISE = [
    # Blank / separator
    (re.compile(r"^\s*$"), "blank"),
    (re.compile(r"^-{5,}\s*(Reboot|Boot)?\s*-{0,}$"), "boot_separator"),
    # Systemd lifecycle (successful)
    (re.compile(r"Started .+\.$"), "service_started"),
    (re.compile(r"Stopped .+\.$"), "service_stopped"),
    (re.compile(r"Starting .+\.\.\.$"), "service_starting"),
    (re.compile(r"Stopping .+\.\.\.$"), "service_stopping"),
    (re.compile(r"Reached target .+\.$"), "target_reached"),
    (re.compile(r"Listening on .+\.$"), "socket_listen"),
    (re.compile(r"Mounted .+\.$"), "mount"),
    (re.compile(r"Finished .+\.$"), "service_finished"),
    # Session management
    (re.compile(r"pam_unix.*session (opened|closed)"), "pam_session"),
    (re.compile(r"New session \d+ of user"), "session_new"),
    (re.compile(r"Removed session \d+"), "session_removed"),
    (re.compile(r"session-\d+\.scope: Succeeded"), "session_scope"),
    # DHCP / network
    (re.compile(r"dhclient.*bound to", re.IGNORECASE), "dhcp"),
    (re.compile(r"NetworkManager.*DHCP.*renewed", re.IGNORECASE), "dhcp"),
    (re.compile(r"NetworkManager.*state change.*connected", re.IGNORECASE), "nm_connected"),
    # Cron
    (re.compile(r"CROND\[\d+\].*CMD"), "cron"),
    (re.compile(r"run-parts.*starting"), "cron_parts"),
    (re.compile(r"run-parts.*finished"), "cron_parts"),
    (re.compile(r"anacron.*job.*started"), "anacron"),
    # Audit routine ops
    (re.compile(r"audit.*type=PROCTITLE"), "audit_proctitle"),
    (re.compile(r"audit.*type=SYSCALL.*success=yes"), "audit_success"),
    (re.compile(r"audit.*type=CWD"), "audit_cwd"),
    (re.compile(r"audit.*type=PATH"), "audit_path"),
    (re.compile(r"audit.*type=EOE"), "audit_eoe"),
    # USB device events
    (re.compile(r"usb.*new (full|high|low|super) speed"), "usb_device"),
    (re.compile(r"usb.*USB disconnect"), "usb_disconnect"),
    # Kernel informational
    (re.compile(r"kernel:.*registered new interface driver"), "kernel_driver"),
    (re.compile(r"kernel:.*loaded module"), "kernel_module"),
    (re.compile(r"kernel:.*\blink (up|down)\b.*\bspeed\b"), "kernel_link"),
    (re.compile(r"kernel:.*audit:.*initialized"), "kernel_audit_init"),
    # Logrotate
    (re.compile(r"logrotate.*rotating"), "logrotate"),
    # Polkit
    (re.compile(r"polkitd.*Registered"), "polkit"),
    (re.compile(r"polkitd.*Unregistered"), "polkit"),
    # Timestamp-only lines (some journal formats)
    (re.compile(r"^-- .+ --$"), "journal_marker"),
]

_FALLBACK_SIGNAL = [
    # OOM killer
    (re.compile(r"Out of memory", re.IGNORECASE), "oom"),
    (re.compile(r"oom-killer"), "oom"),
    (re.compile(r"Kill process \d+"), "oom_kill"),
    (re.compile(r"Killed process \d+"), "oom_kill"),
    (re.compile(r"invoked oom-killer"), "oom"),
    # Segfaults
    (re.compile(r"segfault at"), "segfault"),
    (re.compile(r"general protection fault"), "gpf"),
    (re.compile(r"trap invalid opcode"), "invalid_opcode"),
    # Service failures
    (re.compile(r"Failed to start .+\.$"), "service_failed"),
    (re.compile(r"\.service: Failed"), "service_failed"),
    (re.compile(r"\.service: Main process exited, code=exited, status=[1-9]"), "service_exit_fail"),
    (re.compile(r"\.service: Control process exited, code=exited, status=[1-9]"), "service_exit_fail"),
    (re.compile(r"entered failed state"), "service_failed"),
    # Disk errors
    (re.compile(r"I/O error"), "io_error"),
    (re.compile(r"ext[234].*error"), "fs_error"),
    (re.compile(r"XFS.*error"), "fs_error"),
    (re.compile(r"Buffer I/O error"), "io_error"),
    (re.compile(r"SCSI error"), "scsi_error"),
    # Kernel panics
    (re.compile(r"Kernel panic"), "kernel_panic"),
    (re.compile(r"BUG:"), "kernel_bug"),
    (re.compile(r"WARNING:.*at "), "kernel_warning"),
    (re.compile(r"Call Trace:"), "call_trace"),
    # SELinux
    (re.compile(r"avc:\s+denied"), "selinux_denied"),
    (re.compile(r"SELinux.*denied"), "selinux_denied"),
    # Coredumps
    (re.compile(r"Process \d+ .+ dumped core"), "coredump"),
    (re.compile(r"systemd-coredump.*dumped"), "coredump"),
]

_ps = PatternSet.load("journald")
NOISE_PATTERNS = _ps.noise_patterns if _ps else _FALLBACK_NOISE
SIGNAL_PATTERNS = _ps.signal_patterns if _ps else _FALLBACK_SIGNAL

_ERROR_CONTEXT = _ps.error_context if _ps else 5
_MAX_LINE_LEN = _ps.max_line_len if _ps else 500
_STREAMING_THRESHOLD = (_ps.file_streaming_threshold_mb if _ps else 50) * 1024 * 1024
_BLOCK_DETECTORS = _ps.block_detectors if _ps else []


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
