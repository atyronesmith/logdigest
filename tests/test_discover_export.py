"""Tests for pattern export and schema compliance."""

from __future__ import annotations

import json
import re

import pytest

from logdigest.discover.classifier import CandidatePattern
from logdigest.discover.export import export_pattern_set


def _make_candidate(
    regex: str,
    label: str,
    classification: str = "noise",
    confidence: float = 0.9,
    frequency: float = 0.1,
) -> CandidatePattern:
    return CandidatePattern(
        regex=regex,
        label=label,
        classification=classification,
        confidence=confidence,
        corpus_frequency=frequency,
        template="test",
        cluster_size=100,
    )


class TestExportPatternSet:
    def test_basic_structure(self):
        noise = [_make_candidate(r"^ok: \[", "ok")]
        signal = [_make_candidate(r"^fatal: \[", "fatal", "signal")]
        data = export_pattern_set("test", noise, signal, 50)

        assert data["domain"] == "test"
        assert "generated" in data
        assert data["corpus_size"] == 50
        assert len(data["noise_patterns"]) == 1
        assert len(data["signal_patterns"]) == 1

    def test_noise_sorted_by_frequency(self):
        noise = [
            _make_candidate(r"^low", "low", frequency=0.01),
            _make_candidate(r"^high", "high", frequency=0.5),
            _make_candidate(r"^mid", "mid", frequency=0.1),
        ]
        data = export_pattern_set("test", noise, [], 100)
        freqs = [p["corpus_frequency"] for p in data["noise_patterns"]]
        assert freqs == sorted(freqs, reverse=True)

    def test_signal_sorted_by_label(self):
        signal = [
            _make_candidate(r"^z", "zebra", "signal"),
            _make_candidate(r"^a", "alpha", "signal"),
            _make_candidate(r"^m", "mid", "signal"),
        ]
        data = export_pattern_set("test", [], signal, 100)
        labels = [p["label"] for p in data["signal_patterns"]]
        assert labels == sorted(labels)

    def test_conflicts_included(self):
        conflicts = [{"pattern": "^test", "action": "blocked"}]
        data = export_pattern_set("test", [], [], 10, conflicts)
        assert data["conflicts"] == conflicts

    def test_no_conflicts_omitted(self):
        data = export_pattern_set("test", [], [], 10)
        assert "conflicts" not in data

    def test_pattern_entry_fields(self):
        noise = [_make_candidate(r"^ok:", "ok", confidence=0.95, frequency=0.42)]
        data = export_pattern_set("test", noise, [], 100)
        entry = data["noise_patterns"][0]
        assert entry["regex"] == r"^ok:"
        assert entry["label"] == "ok"
        assert entry["confidence"] == 0.95
        assert entry["corpus_frequency"] == 0.42

    def test_no_config_section(self):
        data = export_pattern_set("test", [], [], 10)
        assert "config" not in data

    @pytest.fixture
    def _require_jsonschema(self):
        pytest.importorskip("jsonschema")

    @pytest.mark.usefixtures("_require_jsonschema")
    def test_schema_validation(self):
        from logdigest.domain_base import PatternSet

        noise = [_make_candidate(r"^ok: \[", "ok")]
        signal = [_make_candidate(r"^fatal: \[", "fatal", "signal")]
        data = export_pattern_set("test", noise, signal, 50)
        PatternSet._validate_schema(data)

    def test_regexes_compile(self):
        from logdigest.domain_base import PatternSet

        noise = [_make_candidate(r"^ok: \[", "ok")]
        signal = [_make_candidate(r"^fatal: \[", "fatal", "signal")]
        data = export_pattern_set("test", noise, signal, 50)
        PatternSet._compile_patterns(data["noise_patterns"])
        PatternSet._compile_patterns(data["signal_patterns"])

    def test_generated_is_iso8601(self):
        data = export_pattern_set("test", [], [], 10)
        from datetime import datetime
        datetime.fromisoformat(data["generated"].replace("Z", "+00:00"))
