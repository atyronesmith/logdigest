"""Validate discovered patterns against held-out test data."""

from __future__ import annotations

import re

from logdigest.discover.classifier import CandidatePattern


def validate_patterns(
    candidates: list[CandidatePattern],
    test_lines: list[str],
    min_recall_ratio: float = 0.5,
) -> list[CandidatePattern]:
    """Validate candidates against held-out test lines.

    Removes candidates that:
    - Match signal lines from the same candidate set (false positive)
    - Have recall below min_recall_ratio of expected frequency

    Adjusts confidence based on test performance.

    Returns validated candidates only.
    """
    if not test_lines:
        return candidates

    total_test = len(test_lines)

    # Build signal regex set for safety check
    signal_regexes: list[re.Pattern] = []
    for c in candidates:
        if c.classification == "signal":
            try:
                signal_regexes.append(re.compile(c.regex))
            except re.error:
                pass

    validated: list[CandidatePattern] = []

    for candidate in candidates:
        try:
            compiled = re.compile(candidate.regex)
        except re.error:
            continue

        # Count matches in test set
        match_count = sum(1 for line in test_lines if compiled.search(line))
        test_frequency = match_count / total_test if total_test > 0 else 0

        if candidate.classification == "noise":
            # Safety: noise pattern must not match signal lines
            matches_signal = False
            for sig_re in signal_regexes:
                for line in test_lines:
                    if compiled.search(line) and sig_re.search(line):
                        matches_signal = True
                        break
                if matches_signal:
                    break
            if matches_signal:
                continue

            # Recall: test frequency should be >= min_recall_ratio * training frequency
            expected = candidate.corpus_frequency * min_recall_ratio
            if candidate.corpus_frequency > 0.001 and test_frequency < expected:
                continue

            # Adjust confidence based on test performance
            if test_frequency > 0:
                ratio = min(test_frequency / max(candidate.corpus_frequency, 0.001), 1.5)
                candidate.confidence = round(
                    candidate.confidence * (0.7 + 0.3 * ratio), 2
                )
            else:
                candidate.confidence = round(candidate.confidence * 0.5, 2)

        validated.append(candidate)

    return validated
