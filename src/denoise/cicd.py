"""CI/CD log pre-digestion: build/deploy noise removal.

GitHub Actions, Jenkins, and GitLab CI logs are 70-90% noise —
dependency installation, download progress, container pulls, and
cache operations. This module strips all of it, preserving only
errors, test failures, and build output.

Three levels of compression:
1. Line-level: strip noise lines (installs, downloads, cache ops)
2. Block-level: collapse repeated patterns into summaries
3. Blob-level: truncate large dependency trees and build stats
"""

from __future__ import annotations

import re
from pathlib import Path

from denoise.domain_base import (
    classify_line as _classify_line,
    analyze_file as _analyze_file,
    find_large_blobs,
)

# ---------------------------------------------------------------------------
# Level 1: Line-level classification
# ---------------------------------------------------------------------------

# Shared across CI systems
_COMMON_NOISE = [
    # Blank / separator
    (re.compile(r"^\s*$"), "blank"),
    (re.compile(r"^-{5,}$"), "separator"),
    (re.compile(r"^={5,}$"), "separator"),
    (re.compile(r"^\*{5,}$"), "separator"),
    # Download progress
    (re.compile(r"Downloading .+\d+%"), "download_progress"),
    (re.compile(r"Extracting .+\d+%"), "extract_progress"),
    (re.compile(r"^\s*\d+(\.\d+)?%\s"), "progress_pct"),
    (re.compile(r"Receiving objects:\s+\d+%"), "git_progress"),
    (re.compile(r"Resolving deltas:\s+\d+%"), "git_progress"),
    # pip install
    (re.compile(r"^\s*Collecting\s"), "pip_install"),
    (re.compile(r"^\s*Downloading\s.*\.whl"), "pip_install"),
    (re.compile(r"^\s*Installing collected packages"), "pip_install"),
    (re.compile(r"^\s*Successfully installed\s"), "pip_install"),
    (re.compile(r"^\s*Using cached\s"), "pip_install"),
    (re.compile(r"^\s*Requirement already satisfied"), "pip_install"),
    (re.compile(r"^\s*Building wheel"), "pip_install"),
    # npm install
    (re.compile(r"^added \d+ packages"), "npm_install"),
    (re.compile(r"^npm warn"), "npm_warn"),
    (re.compile(r"^npm notice"), "npm_notice"),
    (re.compile(r"^\s*npm http"), "npm_install"),
    # apt/yum install
    (re.compile(r"^\s*Setting up\s"), "apt_install"),
    (re.compile(r"^\s*Unpacking\s"), "apt_install"),
    (re.compile(r"^\s*Selecting previously"), "apt_install"),
    (re.compile(r"^\s*Preparing to unpack"), "apt_install"),
    (re.compile(r"^\s*Get:\d+"), "apt_install"),
    (re.compile(r"^\s*Reading package lists"), "apt_install"),
    (re.compile(r"^\s*Building dependency tree"), "apt_install"),
    (re.compile(r"^\s*Installed:\s"), "yum_install"),
    (re.compile(r"^\s*Installing\s*:"), "yum_install"),
    (re.compile(r"^\s*Verifying\s*:"), "yum_install"),
    # Container pulls
    (re.compile(r"^\s*Pulling from\s"), "container_pull"),
    (re.compile(r"^\s*Digest:\s"), "container_pull"),
    (re.compile(r"^\s*Status: Downloaded"), "container_pull"),
    (re.compile(r"^\s*(Waiting|Downloading|Extracting|Pull complete|Already exists)\s*$"), "container_layer"),
    (re.compile(r"^\s*[a-f0-9]{12}: (Waiting|Downloading|Extracting|Pull complete|Already exists)"), "container_layer"),
    # Cache operations
    (re.compile(r"^\s*Cache (restored|saved|not found)", re.IGNORECASE), "cache_op"),
    (re.compile(r"^\s*Post cache", re.IGNORECASE), "cache_op"),
    (re.compile(r"^\s*Received \d+ of \d+"), "cache_op"),
]

# GitHub Actions specific
_GITHUB_ACTIONS_NOISE = [
    (re.compile(r"^##\[debug\]"), "gha_debug"),
    (re.compile(r"^##\[group\]"), "gha_group"),
    (re.compile(r"^##\[endgroup\]"), "gha_endgroup"),
    (re.compile(r"^##\[command\]"), "gha_command"),
    (re.compile(r"^\s*Syncing repository"), "gha_checkout"),
    (re.compile(r"^\s*Deleting the contents"), "gha_checkout"),
    (re.compile(r"^\s*Initializing the repository"), "gha_checkout"),
    (re.compile(r"^\s*Fetching the repository"), "gha_checkout"),
    (re.compile(r"^\s*Checking out\s"), "gha_checkout"),
    (re.compile(r"^\s*\[command\]"), "gha_command"),
]

# Jenkins specific
_JENKINS_NOISE = [
    (re.compile(r"^\s*Started by\s"), "jenkins_start"),
    (re.compile(r"^\s*Building on\s"), "jenkins_build"),
    (re.compile(r"^\s*Building in\s"), "jenkins_build"),
    (re.compile(r"^\s*Finished:\s"), "jenkins_finish"),
    (re.compile(r"^\s*Polling SCM"), "jenkins_scm"),
    (re.compile(r"^\s*SCM polling completed"), "jenkins_scm"),
    (re.compile(r"^\s*Running on\s"), "jenkins_agent"),
    (re.compile(r"^\s*Waiting for next available executor"), "jenkins_agent"),
    (re.compile(r"^\s*\[Pipeline\]\s*(Start|End|node|stage|sh|echo)"), "jenkins_pipeline"),
]

# GitLab CI specific
_GITLAB_CI_NOISE = [
    (re.compile(r"^\s*Using Docker executor"), "gitlab_executor"),
    (re.compile(r"^\s*Getting source from\s"), "gitlab_source"),
    (re.compile(r"^\s*Checking out [a-f0-9]+ as"), "gitlab_checkout"),
    (re.compile(r"^\s*Uploading artifacts"), "gitlab_artifacts"),
    (re.compile(r"^\s*Uploading artifacts to coordinator"), "gitlab_artifacts"),
    (re.compile(r"^\s*Restoring cache"), "gitlab_cache"),
    (re.compile(r"^\s*Saving cache"), "gitlab_cache"),
    (re.compile(r"^\s*\$ echo"), "gitlab_echo"),
]

NOISE_PATTERNS = (
    _COMMON_NOISE
    + _GITHUB_ACTIONS_NOISE
    + _JENKINS_NOISE
    + _GITLAB_CI_NOISE
)

SIGNAL_PATTERNS = [
    # Exit codes
    (re.compile(r"Process completed with exit code [1-9]"), "exit_code"),
    (re.compile(r"exit code [1-9]"), "exit_code"),
    (re.compile(r"exited with code [1-9]"), "exit_code"),
    # General errors
    (re.compile(r"^ERROR:", re.IGNORECASE), "error"),
    (re.compile(r"^FATAL:", re.IGNORECASE), "fatal"),
    (re.compile(r"^\s*error\["), "compiler_error"),
    # Test failures
    (re.compile(r"\bFAILED\b"), "test_fail"),
    (re.compile(r"^FAIL:"), "test_fail"),
    (re.compile(r"AssertionError"), "assertion"),
    (re.compile(r"AssertionError"), "assertion"),  # common typo in logs
    (re.compile(r"AssertError"), "assertion"),
    (re.compile(r"error TS\d+"), "typescript_error"),
    (re.compile(r"FAILURES"), "test_fail_header"),
    # Build failures
    (re.compile(r"Build FAILED", re.IGNORECASE), "build_fail"),
    (re.compile(r"COMPILATION ERROR", re.IGNORECASE), "build_fail"),
    (re.compile(r"error\[E\d+\]"), "rust_error"),
    # Timeouts
    (re.compile(r"Cancelling", re.IGNORECASE), "cancel"),
    (re.compile(r"Timed out", re.IGNORECASE), "timeout"),
    (re.compile(r"exceeded the maximum"), "timeout"),
    # GitHub Actions error/warning annotations
    (re.compile(r"^##\[error\]"), "gha_error"),
    (re.compile(r"^##\[warning\]"), "gha_warning"),
]


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
            for j in range(max(0, i - 3), min(len(lines), i + 3)):
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
    """Pre-digest a single CI/CD log file."""
    text = filepath.read_text(errors="replace")
    return predigest(text)


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
