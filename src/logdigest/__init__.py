"""logdigest: task-aware pre-digestion for RAG and LLM pipelines.

Removes low-entropy content (boilerplate, separators, duplicates) from
document corpora before chunking and embedding. Impact is inversely
proportional to document Shannon entropy — transformative on noisy data,
harmless no-op on clean data.
"""

from logdigest.core import (
    build_boilerplate_set,
    dedup_documents,
    is_separator_line,
    predigest_document,
)
from logdigest.entropy import corpus_entropy

__all__ = [
    "build_boilerplate_set",
    "corpus_entropy",
    "dedup_documents",
    "is_separator_line",
    "predigest_document",
]
