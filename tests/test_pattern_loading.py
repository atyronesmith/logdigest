"""Tests for PatternSet loader and JSON pattern files."""

from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from logdigest.domain_base import PatternSet, classify_line


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear PatternSet cache before and after each test."""
    PatternSet.clear_cache()
    yield
    PatternSet.clear_cache()


# ── Loader basics ──────────────────────────────────────────────────────


class TestPatternSetLoad:
    def test_load_existing_domain(self):
        ps = PatternSet.load("ansible")
        assert ps is not None
        assert ps.domain == "ansible"
        assert len(ps.noise_patterns) > 0
        assert len(ps.signal_patterns) > 0

    def test_missing_domain_returns_none(self):
        ps = PatternSet.load("nonexistent_domain_xyz")
        assert ps is None

    def test_explicit_missing_version_raises(self):
        with pytest.raises(FileNotFoundError):
            PatternSet.load("ansible", version="v999.999")

    def test_latest_falls_back_to_baseline(self):
        ps = PatternSet.load("ansible", version="latest")
        assert ps is not None
        assert ps.domain == "ansible"

    def test_cache_returns_same_object(self):
        ps1 = PatternSet.load("ansible")
        ps2 = PatternSet.load("ansible")
        assert ps1 is ps2

    def test_cache_miss_returns_none(self):
        ps1 = PatternSet.load("nonexistent_domain_xyz")
        assert ps1 is None
        ps2 = PatternSet.load("nonexistent_domain_xyz")
        assert ps2 is None

    def test_clear_cache(self):
        ps1 = PatternSet.load("ansible")
        PatternSet.clear_cache()
        ps2 = PatternSet.load("ansible")
        assert ps1 is not ps2

    def test_metadata_populated(self):
        ps = PatternSet.load("ansible")
        assert ps.metadata.get("domain") == "ansible"
        assert "generated" in ps.metadata


# ── Pattern compilation ────────────────────────────────────────────────


class TestPatternCompilation:
    def test_bad_regex_raises_valueerror(self):
        bad_patterns = [{"regex": "[invalid(", "label": "broken"}]
        with pytest.raises(ValueError, match="Invalid regex"):
            PatternSet._compile_patterns(bad_patterns)

    def test_compiled_patterns_are_regex(self):
        ps = PatternSet.load("ansible")
        for pattern, label in ps.noise_patterns:
            assert isinstance(pattern, re.Pattern)
            assert isinstance(label, str)

    def test_ignorecase_preserved(self):
        ps = PatternSet.load("mustgather")
        ignorecase_labels = set()
        for pattern, label in ps.noise_patterns + ps.signal_patterns:
            if pattern.flags & re.IGNORECASE:
                ignorecase_labels.add(label)
        assert "healthcheck" in ignorecase_labels
        assert "error" in ignorecase_labels
        assert "oom" in ignorecase_labels


# ── Schema validation ─────────────────────────────────────────────────


class TestSchemaValidation:
    @pytest.fixture
    def _require_jsonschema(self):
        pytest.importorskip("jsonschema")

    @pytest.mark.usefixtures("_require_jsonschema")
    def test_all_domains_valid(self):
        import importlib.resources
        import jsonschema

        schema_pkg = importlib.resources.files("logdigest.schemas")
        schema = json.loads(
            schema_pkg.joinpath("pattern_set.schema.json").read_text("utf-8")
        )
        patterns_pkg = importlib.resources.files("logdigest.patterns")

        for domain in ["ansible", "mustgather", "cicd", "journald"]:
            data = json.loads(
                patterns_pkg.joinpath(f"{domain}.json").read_text("utf-8")
            )
            jsonschema.validate(data, schema)

    @pytest.mark.usefixtures("_require_jsonschema")
    def test_schema_rejects_bad_label(self):
        import jsonschema
        import importlib.resources

        schema_pkg = importlib.resources.files("logdigest.schemas")
        schema = json.loads(
            schema_pkg.joinpath("pattern_set.schema.json").read_text("utf-8")
        )
        bad_data = {
            "domain": "test",
            "generated": "2026-01-01T00:00:00Z",
            "noise_patterns": [{"regex": ".*", "label": "BAD LABEL!"}],
            "signal_patterns": [],
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad_data, schema)

    def test_warns_without_jsonschema(self):
        with patch.dict("sys.modules", {"jsonschema": None}):
            with patch("builtins.__import__", side_effect=ImportError):
                pass


# ── Pattern count parity ──────────────────────────────────────────────


class TestPatternCounts:
    """Verify JSON files have the same pattern counts as Python source."""

    def test_ansible_counts(self):
        from logdigest.ansible import NOISE_PATTERNS, SIGNAL_PATTERNS

        ps = PatternSet.load("ansible")
        assert len(ps.noise_patterns) == len(NOISE_PATTERNS)
        assert len(ps.signal_patterns) == len(SIGNAL_PATTERNS)

    def test_mustgather_counts(self):
        from logdigest.mustgather import NOISE_PATTERNS, SIGNAL_PATTERNS

        ps = PatternSet.load("mustgather")
        assert len(ps.noise_patterns) == len(NOISE_PATTERNS)
        assert len(ps.signal_patterns) == len(SIGNAL_PATTERNS)

    def test_cicd_counts(self):
        from logdigest.cicd import NOISE_PATTERNS, SIGNAL_PATTERNS

        ps = PatternSet.load("cicd")
        assert len(ps.noise_patterns) == len(NOISE_PATTERNS)
        assert len(ps.signal_patterns) == len(SIGNAL_PATTERNS)

    def test_journald_counts(self):
        from logdigest.journald import NOISE_PATTERNS, SIGNAL_PATTERNS

        ps = PatternSet.load("journald")
        assert len(ps.noise_patterns) == len(NOISE_PATTERNS)
        assert len(ps.signal_patterns) == len(SIGNAL_PATTERNS)


# ── Behavioral equivalence ────────────────────────────────────────────


class TestBehavioralEquivalence:
    """Classify the same lines with JSON-loaded vs Python patterns.

    This is the critical regression test — catches regex flag loss,
    escaping errors, or ordering differences.
    """

    ANSIBLE_LINES = [
        ("ok: [host1]", "noise:ok"),
        ("skipping: [host2]", "noise:skipping"),
        ("fatal: [host3]", "signal:fatal"),
        ("PLAY [deploy] ***", "signal:play_header"),
        ("TASK [install package]", "signal:task_header"),
        ("some random output", "unknown"),
        ("[DEPRECATION WARNING]", "noise:deprecation"),
        ("PLAY RECAP", "signal:play_recap"),
    ]

    MUSTGATHER_LINES = [
        ('"GET /healthz" 200', "noise:healthz"),
        ("successfully acquired lease", "noise:leader_election"),
        ("caches populated", "noise:informer"),
        ("level=error something bad", "signal:error"),
        ("LEVEL=ERROR uppercase", "signal:error"),
        ("CrashLoopBackOff", "signal:crashloop"),
        ("Out of memory killed", "signal:oom"),
        ("out of MEMORY", "signal:oom"),
        ("some normal log line", "unknown"),
    ]

    CICD_LINES = [
        ("##[debug] some debug", "noise:gha_debug"),
        ("  Collecting requests", "noise:pip_install"),
        ("added 42 packages", "noise:npm_install"),
        ("Process completed with exit code 1", "signal:exit_code"),
        ("FAILED", "signal:test_fail"),
        ("normal build output", "unknown"),
    ]

    JOURNALD_LINES = [
        ("Started some service.", "noise:service_started"),
        ("pam_unix session opened", "noise:pam_session"),
        ("Out of memory: Kill process", "signal:oom"),
        ("segfault at 0x0", "signal:segfault"),
        ("Kernel panic - not syncing", "signal:kernel_panic"),
        ("some normal syslog message", "unknown"),
    ]

    @pytest.mark.parametrize("line,expected", ANSIBLE_LINES)
    def test_ansible_equivalence(self, line, expected):
        from logdigest.ansible import NOISE_PATTERNS, SIGNAL_PATTERNS

        ps = PatternSet.load("ansible")
        python_result = classify_line(line, NOISE_PATTERNS, SIGNAL_PATTERNS)
        json_result = classify_line(line, ps.noise_patterns, ps.signal_patterns)
        assert python_result == json_result == expected

    @pytest.mark.parametrize("line,expected", MUSTGATHER_LINES)
    def test_mustgather_equivalence(self, line, expected):
        from logdigest.mustgather import NOISE_PATTERNS, SIGNAL_PATTERNS

        ps = PatternSet.load("mustgather")
        python_result = classify_line(line, NOISE_PATTERNS, SIGNAL_PATTERNS)
        json_result = classify_line(line, ps.noise_patterns, ps.signal_patterns)
        assert python_result == json_result == expected

    @pytest.mark.parametrize("line,expected", CICD_LINES)
    def test_cicd_equivalence(self, line, expected):
        from logdigest.cicd import NOISE_PATTERNS, SIGNAL_PATTERNS

        ps = PatternSet.load("cicd")
        python_result = classify_line(line, NOISE_PATTERNS, SIGNAL_PATTERNS)
        json_result = classify_line(line, ps.noise_patterns, ps.signal_patterns)
        assert python_result == json_result == expected

    @pytest.mark.parametrize("line,expected", JOURNALD_LINES)
    def test_journald_equivalence(self, line, expected):
        from logdigest.journald import NOISE_PATTERNS, SIGNAL_PATTERNS

        ps = PatternSet.load("journald")
        python_result = classify_line(line, NOISE_PATTERNS, SIGNAL_PATTERNS)
        json_result = classify_line(line, ps.noise_patterns, ps.signal_patterns)
        assert python_result == json_result == expected
