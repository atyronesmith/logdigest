"""Tests for logdigest core pre-digestion functions."""

from logdigest.core import (
    build_boilerplate_set,
    dedup_documents,
    is_separator_line,
    predigest_corpus,
    predigest_document,
)


def test_is_separator_line():
    assert is_separator_line("*****")
    assert is_separator_line("----------")
    assert is_separator_line("==========")
    assert is_separator_line("~~~~")
    assert is_separator_line("- - - - -")
    assert not is_separator_line("---")  # too short
    assert not is_separator_line("hello world")
    assert not is_separator_line("")


def test_build_boilerplate_set():
    # Need at least 10 docs
    docs = [f"doc {i}\nThis is a boilerplate line that repeats.\nUnique line {i}" for i in range(20)]
    bp = build_boilerplate_set(docs, threshold=0.05)
    assert "This is a boilerplate line that repeats." in bp
    assert "Unique line 0" not in bp


def test_build_boilerplate_set_too_few_docs():
    docs = ["doc 1\nshared", "doc 2\nshared"]
    bp = build_boilerplate_set(docs)
    assert len(bp) == 0


def test_dedup_documents():
    docs = ["alpha", "beta", "alpha", "gamma", "beta"]
    deduped, removed = dedup_documents(docs)
    assert len(deduped) == 3
    assert removed == 2


def test_predigest_document_removes_separators():
    text = "Line one\n==========\nLine two\n----------\nLine three"
    result = predigest_document(text)
    assert "========" not in result
    assert "--------" not in result
    assert "Line one" in result
    assert "Line two" in result


def test_predigest_document_collapses_blanks():
    text = "Line one\n\n\n\n\nLine two"
    result = predigest_document(text)
    assert result == "Line one\n\nLine two"


def test_predigest_document_strips_boilerplate():
    bp = {"This is a disclaimer that appears everywhere."}
    text = "Content here.\nThis is a disclaimer that appears everywhere.\nMore content."
    result = predigest_document(text, boilerplate_lines=bp)
    assert "disclaimer" not in result
    assert "Content here." in result


def test_predigest_corpus():
    docs = [f"doc {i}\nShared footer line here.\nContent {i}" for i in range(20)]
    digested, stats = predigest_corpus(docs, threshold=0.05)
    assert len(digested) == 20
    assert stats["boilerplate_lines"] >= 1
    assert stats["char_reduction_pct"] > 0


def test_predigest_corpus_with_dupes():
    docs = ["same doc"] * 5 + [f"unique {i}" for i in range(15)]
    digested, stats = predigest_corpus(docs, dedup=True)
    assert stats["duplicates_removed"] == 4
    assert len(digested) == 16


def test_predigest_corpus_small_skips_boilerplate():
    docs = [f"doc {i}\nShared footer line here.\nContent {i}" for i in range(5)]
    _digested, stats = predigest_corpus(docs)
    assert stats["boilerplate_skipped"] is True
    assert stats["boilerplate_lines"] == 0
