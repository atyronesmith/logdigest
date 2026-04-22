"""Noise/signal classification and auto-labeling for discovered templates."""

from __future__ import annotations

import re
from dataclasses import dataclass

from logdigest.discover.mining import TemplateCluster

ERROR_TOKENS = frozenset({
    "error", "fatal", "fail", "failed", "failure",
    "panic", "crash", "crashed",
    "exception", "traceback",
    "segfault", "oom", "killed",
    "unreachable", "critical",
    "abort", "aborted",
    "timeout", "denied",
    "refused", "rejected",
})

_SKIP_WORDS = frozenset({
    "the", "a", "an", "is", "was", "are", "were", "to", "from",
    "in", "on", "at", "by", "for", "of", "and", "or", "with",
})

_LABEL_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9]*")


@dataclass
class CandidatePattern:
    """A discovered pattern candidate."""

    regex: str
    label: str
    classification: str  # "noise", "signal", "uncategorized"
    confidence: float
    corpus_frequency: float
    template: str
    cluster_size: int


def _has_error_token(template: str) -> bool:
    """Check if template contains any error-indicating token."""
    tokens = {t.lower() for t in _WORD_RE.findall(template)}
    return bool(tokens & ERROR_TOKENS)


def generate_label(template: str, existing_labels: set[str] | None = None) -> str:
    """Generate a label from a Drain3 template.

    Extracts first 1-3 significant words, lowercased, joined with '_'.
    Deduplicates against existing_labels with numeric suffix.
    """
    # Strip wildcards and extract words
    clean = template.replace("<*>", " ")
    words = _WORD_RE.findall(clean)
    significant = [w.lower() for w in words if w.lower() not in _SKIP_WORDS]

    base = "pattern" if not significant else "_".join(significant[:3])

    # Truncate
    base = base[:30]

    # Ensure valid label
    if not _LABEL_RE.match(base):
        base = "p_" + re.sub(r"[^a-z0-9_]", "", base)
        if not _LABEL_RE.match(base):
            base = "pattern"

    if existing_labels is None:
        return base

    if base not in existing_labels:
        return base

    # Deduplicate with numeric suffix
    for i in range(2, 1000):
        candidate = f"{base}_{i}"
        if candidate not in existing_labels:
            return candidate

    return f"{base}_x"


def classify_template(
    cluster: TemplateCluster,
    regex: str,
    total_lines: int,
    min_frequency: float = 0.01,
) -> CandidatePattern:
    """Classify a template cluster as noise, signal, or uncategorized."""
    frequency = cluster.size / max(total_lines, 1)
    has_error = _has_error_token(cluster.template)

    if has_error:
        classification = "signal"
        if frequency >= 0.05:
            confidence = 0.80
        elif frequency >= min_frequency:
            confidence = 0.90
        else:
            confidence = 0.95
    elif frequency >= min_frequency:
        classification = "noise"
        confidence = min(0.99, frequency * 1.5 + 0.5)
    else:
        classification = "uncategorized"
        confidence = 0.0

    return CandidatePattern(
        regex=regex,
        label="",  # filled in by caller after dedup
        classification=classification,
        confidence=round(confidence, 2),
        corpus_frequency=round(frequency, 6),
        template=cluster.template,
        cluster_size=cluster.size,
    )


def resolve_conflicts(
    candidates: list[CandidatePattern],
    reference_noise: list[tuple[re.Pattern, str]],
    reference_signal: list[tuple[re.Pattern, str]],
) -> tuple[list[CandidatePattern], list[dict]]:
    """Apply signal-always-wins conflict resolution against reference patterns.

    Returns (resolved_candidates, conflicts_list).
    """
    conflicts: list[dict] = []
    resolved: list[CandidatePattern] = []

    for candidate in candidates:
        try:
            compiled = re.compile(candidate.regex)
        except re.error:
            continue

        if candidate.classification == "noise":
            # Check if this noise candidate matches any reference signal pattern
            blocked = False
            for sig_pattern, sig_label in reference_signal:
                for sample in _generate_test_strings(compiled):
                    if sig_pattern.search(sample):
                        conflicts.append({
                            "pattern": candidate.regex,
                            "existing_class": "signal",
                            "proposed_class": "noise",
                            "reason": f"matches reference signal pattern '{sig_label}'",
                            "action": "blocked",
                        })
                        blocked = True
                        break
                if blocked:
                    break
            if not blocked:
                resolved.append(candidate)
        else:
            resolved.append(candidate)

    return resolved, conflicts


def _generate_test_strings(pattern: re.Pattern) -> list[str]:
    """Generate simple test strings from a pattern for conflict checking.

    Uses the pattern string itself (stripping anchors) as a rough test.
    """
    raw = pattern.pattern
    raw = raw.lstrip("^").rstrip("$")
    # Unescape to get approximate literal
    unescaped = raw.replace("\\[", "[").replace("\\]", "]")
    unescaped = unescaped.replace("\\(", "(").replace("\\)", ")")
    unescaped = unescaped.replace(".*?", "test").replace(".*", "test")
    return [unescaped]
