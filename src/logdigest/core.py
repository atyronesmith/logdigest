"""General-purpose, domain-agnostic pre-digestion.

These techniques require no domain knowledge — boilerplate detection is
fully corpus-driven. They follow the same rules as lossless compression:
identify redundancy and eliminate it.
"""

from __future__ import annotations

from collections import Counter
from hashlib import md5
from typing import TypedDict


class CorpusStats(TypedDict):
    """Stats returned by :func:`predigest_corpus`."""

    duplicates_removed: int
    boilerplate_lines: int
    boilerplate_threshold: float
    boilerplate_skipped: bool
    raw_chars: int
    digest_chars: int
    char_reduction_pct: float

SEPARATOR_PATTERN_CHARS = set("*=-_~#.+")


def is_separator_line(line: str) -> bool:
    """Lines that are just visual separators (*****, ------, etc.)."""
    stripped = line.strip()
    if len(stripped) < 4:
        return False
    unique = set(stripped)
    return len(unique) <= 2 and unique.issubset(SEPARATOR_PATTERN_CHARS | {" "})


def build_boilerplate_set(
    documents: list[str], threshold: float = 0.05
) -> set[str]:
    """Find lines that appear in >threshold fraction of documents.

    These are cross-document boilerplate (headers, footers, disclaimers).
    Only considers lines >20 chars to avoid matching blank/trivial lines.

    Returns an empty set when the corpus has fewer than 10 documents,
    since boilerplate detection needs enough samples to distinguish
    recurring lines from coincidence.
    """
    n_docs = len(documents)
    if n_docs < 10:
        return set()

    line_doc_count: Counter[str] = Counter()
    for doc in documents:
        seen: set[str] = set()
        for line in doc.split("\n"):
            line = line.strip()
            if len(line) > 20 and line not in seen:
                seen.add(line)
                line_doc_count[line] += 1

    min_count = max(int(n_docs * threshold), 3)
    return {line for line, cnt in line_doc_count.items() if cnt >= min_count}


def dedup_documents(
    documents: list[str],
) -> tuple[list[str], int]:
    """Remove exact duplicate documents. Returns (deduped_list, n_removed)."""
    seen: set[str] = set()
    deduped: list[str] = []
    removed = 0
    for doc in documents:
        h = md5(doc.encode()).hexdigest()
        if h not in seen:
            seen.add(h)
            deduped.append(doc)
        else:
            removed += 1
    return deduped, removed


def predigest_document(
    text: str, boilerplate_lines: set[str] | None = None
) -> str:
    """Apply general pre-digestion to a single document.

    Techniques:
    - Strip cross-document boilerplate lines
    - Remove separator lines (*****, ------)
    - Collapse excessive blank lines
    - Normalize whitespace
    """
    if boilerplate_lines is None:
        boilerplate_lines = set()

    lines = text.split("\n")
    cleaned: list[str] = []
    blank_run = 0

    for line in lines:
        stripped = line.strip()

        # Skip boilerplate
        if stripped in boilerplate_lines:
            continue

        # Skip separator lines
        if is_separator_line(stripped):
            continue

        # Collapse blank lines (max 1)
        if not stripped:
            blank_run += 1
            if blank_run <= 1:
                cleaned.append("")
            continue
        else:
            blank_run = 0

        cleaned.append(line.rstrip())

    return "\n".join(cleaned).strip()


def predigest_corpus(
    documents: list[str],
    threshold: float = 0.05,
    dedup: bool = True,
) -> tuple[list[str], CorpusStats]:
    """Pre-digest an entire corpus. Returns (digested_docs, stats).

    Convenience function that runs the full pipeline:
    1. Deduplicate documents (optional)
    2. Detect corpus-level boilerplate (requires >= 10 docs)
    3. Pre-digest each document

    Stats includes ``boilerplate_skipped: True`` when the corpus
    is too small for reliable boilerplate detection.
    """
    stats: dict = {}

    if dedup:
        documents, n_removed = dedup_documents(documents)
        stats["duplicates_removed"] = n_removed
    else:
        stats["duplicates_removed"] = 0

    boilerplate = build_boilerplate_set(documents, threshold)
    stats["boilerplate_lines"] = len(boilerplate)
    stats["boilerplate_threshold"] = threshold
    stats["boilerplate_skipped"] = len(documents) < 10

    raw_chars = sum(len(d) for d in documents)
    digested = [predigest_document(doc, boilerplate) for doc in documents]
    digest_chars = sum(len(d) for d in digested)

    stats["raw_chars"] = raw_chars
    stats["digest_chars"] = digest_chars
    stats["char_reduction_pct"] = (
        round((1 - digest_chars / raw_chars) * 100, 1) if raw_chars else 0
    )

    return digested, stats
