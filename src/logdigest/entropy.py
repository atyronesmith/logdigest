"""Entropy pre-flight diagnostic for pre-digestion impact prediction.

Measures corpus-level entropy metrics to predict whether building a
domain-specific pre-digestion module is worth the investment. Impact
is inversely proportional to document entropy — high redundancy means
high potential savings.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Literal, TypedDict


class EntropyStats(TypedDict):
    """Stats returned by :func:`corpus_entropy`."""

    mean_line_entropy: float
    cross_doc_repetition_rate: float
    within_doc_redundancy: float
    overall_redundancy: float
    predicted_impact: Literal["high", "medium", "low", "none"]
    recommendation: str
    n_documents: int


def line_entropy(text: str) -> float:
    """Shannon entropy of the line-level distribution within a document.

    Treats each unique line as a symbol. Low entropy means many
    repeated lines (high redundancy). High entropy means most
    lines are unique (low redundancy).

    Returns:
        Entropy in bits. 0.0 for single-line or empty documents.
    """
    lines = text.splitlines()
    if len(lines) <= 1:
        return 0.0

    counts = Counter(line.strip() for line in lines)
    total = len(lines)
    entropy = 0.0
    for count in counts.values():
        if count > 0:
            p = count / total
            entropy -= p * math.log2(p)
    return round(entropy, 4)


def _within_doc_redundancy(text: str) -> float:
    """Fraction of lines that are duplicates within a single document."""
    lines = [line.strip() for line in text.splitlines()]
    if not lines:
        return 0.0
    unique = len(set(lines))
    return round(1.0 - unique / len(lines), 4)


def _cross_doc_repetition(documents: list[str], threshold: float = 0.05) -> float:
    """Fraction of unique lines that appear in >threshold of documents."""
    n_docs = len(documents)
    if n_docs < 2:
        return 0.0

    line_doc_count: Counter[str] = Counter()
    all_unique_lines: set[str] = set()

    for doc in documents:
        seen: set[str] = set()
        for line in doc.splitlines():
            stripped = line.strip()
            if len(stripped) > 5:  # skip trivial lines
                all_unique_lines.add(stripped)
                if stripped not in seen:
                    seen.add(stripped)
                    line_doc_count[stripped] += 1

    if not all_unique_lines:
        return 0.0

    min_count = max(int(n_docs * threshold), 2)
    shared = sum(1 for cnt in line_doc_count.values() if cnt >= min_count)
    return round(shared / len(all_unique_lines), 4)


def corpus_entropy(documents: list[str]) -> EntropyStats:
    """Compute entropy metrics across a corpus.

    Returns:
        Dict with:
        - mean_line_entropy: average per-document line entropy
        - cross_doc_repetition_rate: fraction of lines shared across docs
        - within_doc_redundancy: average fraction of duplicate lines per doc
        - overall_redundancy: combined redundancy estimate (0-1)
        - predicted_impact: "high" | "medium" | "low" | "none"
        - recommendation: human-readable guidance
    """
    if not documents:
        return {
            "mean_line_entropy": 0.0,
            "cross_doc_repetition_rate": 0.0,
            "within_doc_redundancy": 0.0,
            "overall_redundancy": 0.0,
            "predicted_impact": "none",
            "recommendation": "No documents provided.",
            "n_documents": 0,
        }

    # Per-document metrics
    entropies = [line_entropy(doc) for doc in documents]
    redundancies = [_within_doc_redundancy(doc) for doc in documents]

    mean_entropy = round(sum(entropies) / len(entropies), 4)
    mean_redundancy = round(sum(redundancies) / len(redundancies), 4)
    cross_doc = _cross_doc_repetition(documents)

    # Combined redundancy: weight within-doc higher (it drives most savings)
    overall = round(mean_redundancy * 0.7 + cross_doc * 0.3, 4)

    # Impact prediction based on empirical data:
    # Ansible logs: ~80% redundancy -> 83% savings
    # TechQA: ~11% redundancy -> 11% savings
    # EManual/CuAD: ~0% -> 0% savings
    if overall > 0.60:
        impact = "high"
        recommendation = (
            f"High redundancy detected ({overall:.0%}). "
            f"A domain module could achieve 60-85% token savings. "
            f"Strongly recommended."
        )
    elif overall > 0.20:
        impact = "medium"
        recommendation = (
            f"Moderate redundancy ({overall:.0%}). "
            f"Domain-agnostic pre-digestion (core.py) may achieve 10-40% savings. "
            f"A domain module is optional."
        )
    elif overall > 0.05:
        impact = "low"
        recommendation = (
            f"Low redundancy ({overall:.0%}). "
            f"Expect 5-15% savings at best. "
            f"Core pre-digestion is sufficient; skip domain module."
        )
    else:
        impact = "none"
        recommendation = (
            f"Minimal redundancy ({overall:.0%}). "
            f"Pre-digestion will be a no-op. Documents are already information-dense."
        )

    return {
        "mean_line_entropy": mean_entropy,
        "cross_doc_repetition_rate": cross_doc,
        "within_doc_redundancy": mean_redundancy,
        "overall_redundancy": overall,
        "predicted_impact": impact,
        "recommendation": recommendation,
        "n_documents": len(documents),
    }


def entropy_report(documents: list[str]) -> str:
    """Human-readable entropy analysis for quick assessment."""
    metrics = corpus_entropy(documents)

    lines = [
        f"Entropy Pre-flight Report ({metrics['n_documents']} documents)",
        "=" * 50,
        f"Mean line entropy:        {metrics['mean_line_entropy']:.2f} bits",
        f"Within-doc redundancy:    {metrics['within_doc_redundancy']:.1%}",
        f"Cross-doc repetition:     {metrics['cross_doc_repetition_rate']:.1%}",
        f"Overall redundancy:       {metrics['overall_redundancy']:.1%}",
        f"Predicted impact:         {metrics['predicted_impact'].upper()}",
        "",
        metrics["recommendation"],
    ]
    return "\n".join(lines)
