"""Tests for pattern validation against held-out data."""

from __future__ import annotations

from logdigest.discover.classifier import CandidatePattern
from logdigest.discover.validation import validate_patterns


def _make_candidate(
    regex: str,
    classification: str = "noise",
    frequency: float = 0.1,
    confidence: float = 0.9,
) -> CandidatePattern:
    return CandidatePattern(
        regex=regex,
        label="test",
        classification=classification,
        confidence=confidence,
        corpus_frequency=frequency,
        template="test template",
        cluster_size=100,
    )


class TestValidatePatterns:
    def test_keeps_valid_noise(self):
        candidates = [_make_candidate(r"^ok: \[", frequency=0.3)]
        test_lines = ["ok: [host1]", "ok: [host2]", "other line"] * 10
        result = validate_patterns(candidates, test_lines)
        assert len(result) == 1

    def test_removes_noise_matching_signal(self):
        noise = _make_candidate(r"error", classification="noise", frequency=0.1)
        signal = _make_candidate(r"error", classification="signal", frequency=0.01)
        test_lines = ["error: something bad", "normal line"]
        result = validate_patterns([noise, signal], test_lines)
        # Noise candidate should be removed because it matches signal lines
        noise_results = [c for c in result if c.classification == "noise"]
        assert len(noise_results) == 0

    def test_keeps_signal(self):
        candidates = [_make_candidate(r"^fatal:", classification="signal")]
        test_lines = ["fatal: [host1]", "ok: [host2]"]
        result = validate_patterns(candidates, test_lines)
        assert len(result) == 1
        assert result[0].classification == "signal"

    def test_removes_low_recall_noise(self):
        # Training frequency 0.5 but test has zero matches
        candidate = _make_candidate(r"^very_specific_pattern_xyz", frequency=0.5)
        test_lines = ["normal line"] * 100
        result = validate_patterns([candidate], test_lines)
        assert len(result) == 0

    def test_empty_test_lines_passthrough(self):
        candidates = [_make_candidate(r"^ok:")]
        result = validate_patterns(candidates, [])
        assert len(result) == 1

    def test_confidence_adjusted(self):
        candidate = _make_candidate(r"^ok: \[", frequency=0.3, confidence=0.9)
        test_lines = ["ok: [host1]"] * 30 + ["other"] * 70
        result = validate_patterns([candidate], test_lines)
        assert len(result) == 1
        # Confidence should be adjusted but still positive
        assert result[0].confidence > 0
        assert result[0].confidence <= 1.0
