"""Export discovered patterns to JSON matching pattern_set.schema.json."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from logdigest.discover.classifier import CandidatePattern


def export_pattern_set(
    domain: str,
    noise: list[CandidatePattern],
    signal: list[CandidatePattern],
    corpus_size: int,
    conflicts: list[dict] | None = None,
) -> dict:
    """Build a JSON-serializable dict matching pattern_set.schema.json.

    Does NOT include the 'config' section (hand-authored only).
    """
    def _pattern_entry(c: CandidatePattern) -> dict:
        entry: dict = {"regex": c.regex, "label": c.label}
        if c.confidence > 0:
            entry["confidence"] = c.confidence
        if c.corpus_frequency > 0:
            entry["corpus_frequency"] = c.corpus_frequency
        return entry

    # Sort noise by frequency descending, signal by label
    sorted_noise = sorted(noise, key=lambda c: c.corpus_frequency, reverse=True)
    sorted_signal = sorted(signal, key=lambda c: c.label)

    data: dict = {
        "domain": domain,
        "generated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "corpus_size": corpus_size,
        "noise_patterns": [_pattern_entry(c) for c in sorted_noise],
        "signal_patterns": [_pattern_entry(c) for c in sorted_signal],
    }

    if conflicts:
        data["conflicts"] = conflicts

    return data


def write_pattern_set(data: dict, output_path: Path) -> None:
    """Write pattern set JSON to a file."""
    output_path.write_text(
        json.dumps(data, indent=2) + "\n",
        encoding="utf-8",
    )
