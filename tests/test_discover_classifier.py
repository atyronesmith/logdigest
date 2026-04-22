"""Tests for classification heuristics and auto-labeling."""

from __future__ import annotations

from logdigest.discover.classifier import (
    CandidatePattern,
    classify_template,
    generate_label,
    _has_error_token,
)
from logdigest.discover.mining import TemplateCluster


def _make_cluster(template: str, size: int = 100, total: int = 1000) -> TemplateCluster:
    return TemplateCluster(
        template=template,
        size=size,
        frequency=size / total,
        cluster_id=1,
        sample_lines=[],
    )


class TestErrorTokenDetection:
    def test_error_detected(self):
        assert _has_error_token("level=error something happened")

    def test_fatal_detected(self):
        assert _has_error_token("fatal: [host1] UNREACHABLE!")

    def test_panic_detected(self):
        assert _has_error_token("panic: runtime error")

    def test_no_error_token(self):
        assert not _has_error_token("ok: [host1]")

    def test_case_insensitive(self):
        assert _has_error_token("FATAL error occurred")

    def test_partial_word_not_matched(self):
        # "default" contains "fault" but shouldn't match
        assert not _has_error_token("using default configuration")


class TestClassifyTemplate:
    def test_noise_high_frequency(self):
        cluster = _make_cluster("ok: [<*>]", size=400, total=1000)
        result = classify_template(cluster, "^ok: \\[", 1000)
        assert result.classification == "noise"
        assert result.confidence > 0.5

    def test_signal_with_error_token(self):
        cluster = _make_cluster("fatal: [<*>]", size=5, total=1000)
        result = classify_template(cluster, "^fatal: \\[", 1000)
        assert result.classification == "signal"

    def test_uncategorized_low_frequency_no_error(self):
        cluster = _make_cluster("some random line", size=1, total=10000)
        result = classify_template(cluster, "^some random", 10000, min_frequency=0.01)
        assert result.classification == "uncategorized"

    def test_signal_high_frequency_lower_confidence(self):
        cluster = _make_cluster("error: <*>", size=100, total=1000)
        result = classify_template(cluster, "^error:", 1000)
        assert result.classification == "signal"
        assert result.confidence == 0.80

    def test_signal_low_frequency_high_confidence(self):
        cluster = _make_cluster("panic: <*>", size=2, total=10000)
        result = classify_template(cluster, "^panic:", 10000)
        assert result.classification == "signal"
        assert result.confidence == 0.95


class TestGenerateLabel:
    def test_simple_label(self):
        assert generate_label("ok: [<*>]") == "ok"

    def test_multi_word_label(self):
        label = generate_label("Starting galaxy role install")
        assert label == "starting_galaxy_role"

    def test_skips_common_words(self):
        label = generate_label("the error is in the system")
        assert "the" not in label.split("_")

    def test_truncation(self):
        label = generate_label("a" * 50 + " " + "b" * 50)
        assert len(label) <= 30

    def test_deduplication(self):
        existing = {"ok"}
        label = generate_label("ok: [<*>]", existing)
        assert label == "ok_2"

    def test_dedup_chain(self):
        existing = {"ok", "ok_2", "ok_3"}
        label = generate_label("ok: [<*>]", existing)
        assert label == "ok_4"

    def test_valid_label_pattern(self):
        import re
        templates = [
            "ok: [<*>]",
            "PLAY [<*>]",
            "123 numeric start",
            "<*> something <*>",
        ]
        pattern = re.compile(r"^[a-z][a-z0-9_]*$")
        for t in templates:
            label = generate_label(t)
            assert pattern.match(label), f"Invalid label {label!r} for template {t!r}"

    def test_wildcard_only_template(self):
        label = generate_label("<*>")
        assert label == "pattern"
