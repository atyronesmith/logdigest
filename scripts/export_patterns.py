#!/usr/bin/env python3
"""Export hardcoded Python patterns to JSON files.

One-time migration script for Phase 1 of pattern externalization.
Reads NOISE_PATTERNS and SIGNAL_PATTERNS from each domain module,
serializes to JSON matching pattern_set.schema.json, and writes
to src/logdigest/patterns/<domain>.json.

Usage:
    python scripts/export_patterns.py
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

src = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(src))

from logdigest import ansible, mustgather, cicd, journald  # noqa: E402

DOMAINS = {
    "ansible": (ansible.NOISE_PATTERNS, ansible.SIGNAL_PATTERNS),
    "mustgather": (mustgather.NOISE_PATTERNS, mustgather.SIGNAL_PATTERNS),
    "cicd": (cicd.NOISE_PATTERNS, cicd.SIGNAL_PATTERNS),
    "journald": (journald.NOISE_PATTERNS, journald.SIGNAL_PATTERNS),
}

OUT_DIR = src / "logdigest" / "patterns"


def pattern_to_dict(compiled: re.Pattern, label: str) -> dict:
    regex = compiled.pattern
    if compiled.flags & re.IGNORECASE and not regex.startswith("(?i)"):
        regex = "(?i)" + regex
    return {"regex": regex, "label": label}


def export_domain(domain: str, noise, signal) -> dict:
    return {
        "domain": domain,
        "version": "baseline",
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "noise_patterns": [pattern_to_dict(p, l) for p, l in noise],
        "signal_patterns": [pattern_to_dict(p, l) for p, l in signal],
    }


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for domain, (noise, signal) in DOMAINS.items():
        data = export_domain(domain, noise, signal)
        path = OUT_DIR / f"{domain}.json"
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(
            f"{domain}: {len(noise)} noise + {len(signal)} signal → {path.name}"
        )


if __name__ == "__main__":
    main()
