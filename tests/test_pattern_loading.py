"""Tests for PatternSet loader and JSON pattern files."""

from __future__ import annotations

import json
import re
from unittest.mock import patch

import pytest

from logdigest.domain_base import BlockConfig, PatternSet, collapse_blocks


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
        import importlib.resources

        import jsonschema

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
        with (
            patch.dict("sys.modules", {"jsonschema": None}),
            patch("builtins.__import__", side_effect=ImportError),
        ):
            pass


# ── Pattern count sanity ─────────────────────────────────────────────


class TestPatternCounts:
    """Verify JSON files loaded successfully with expected pattern counts."""

    @pytest.mark.parametrize("domain,min_noise,min_signal", [
        ("ansible", 30, 8),
        ("mustgather", 35, 18),
        ("cicd", 60, 18),
        ("journald", 34, 24),
    ])
    def test_minimum_patterns(self, domain, min_noise, min_signal):
        ps = PatternSet.load(domain)
        assert len(ps.noise_patterns) >= min_noise
        assert len(ps.signal_patterns) >= min_signal


# ── Classification regression ────────────────────────────────────────


class TestClassificationRegression:
    """Verify JSON-loaded patterns classify known lines correctly.

    Catches regex escaping errors, flag loss, or ordering regressions.
    """

    @pytest.mark.parametrize("line,expected", [
        ("ok: [host1]", "noise:ok"),
        ("skipping: [host2]", "noise:skipping"),
        ("fatal: [host3]", "signal:fatal"),
        ("PLAY [deploy] ***", "signal:play_header"),
        ("TASK [install package]", "signal:task_header"),
        ("some random output", "unknown"),
        ("[DEPRECATION WARNING]", "noise:deprecation"),
        ("PLAY RECAP", "signal:play_recap"),
    ])
    def test_ansible(self, line, expected):
        from logdigest.ansible import classify_line as cls
        assert cls(line) == expected

    @pytest.mark.parametrize("line,expected", [
        ('"GET /healthz" 200', "noise:healthz"),
        ("successfully acquired lease", "noise:leader_election"),
        ("caches populated", "noise:informer"),
        ("level=error something bad", "signal:error"),
        ("LEVEL=ERROR uppercase", "signal:error"),
        ("CrashLoopBackOff", "signal:crashloop"),
        ("Out of memory killed", "signal:oom"),
        ("out of MEMORY", "signal:oom"),
        ("some normal log line", "unknown"),
    ])
    def test_mustgather(self, line, expected):
        from logdigest.mustgather import classify_line as cls
        assert cls(line) == expected

    @pytest.mark.parametrize("line,expected", [
        ("##[debug] some debug", "noise:gha_debug"),
        ("  Collecting requests", "noise:pip_install"),
        ("added 42 packages", "noise:npm_install"),
        ("Process completed with exit code 1", "signal:exit_code"),
        ("FAILED", "signal:test_fail"),
        ("normal build output", "unknown"),
    ])
    def test_cicd(self, line, expected):
        from logdigest.cicd import classify_line as cls
        assert cls(line) == expected

    @pytest.mark.parametrize("line,expected", [
        ("Started some service.", "noise:service_started"),
        ("pam_unix session opened", "noise:pam_session"),
        ("Out of memory: Kill process", "signal:oom"),
        ("segfault at 0x0", "signal:segfault"),
        ("Kernel panic - not syncing", "signal:kernel_panic"),
        ("some normal syslog message", "unknown"),
    ])
    def test_journald(self, line, expected):
        from logdigest.journald import classify_line as cls
        assert cls(line) == expected


# ── Phase 2: Config loading ───────────────────────────────────────────


class TestConfigLoading:
    def test_ansible_config(self):
        ps = PatternSet.load("ansible")
        assert ps.error_context == 5
        assert ps.max_line_len == 500
        assert ps.file_streaming_threshold_mb == 50
        assert "'metadata':" in ps.blob_markers
        assert len(ps.domain_keywords) == 5

    def test_mustgather_config(self):
        ps = PatternSet.load("mustgather")
        assert ps.error_context == 3
        assert ps.max_line_len == 500
        assert "managedFields" in ps.blob_markers
        assert len(ps.block_detectors) == 2
        assert len(ps.domain_keywords) == 7

    def test_cicd_config(self):
        ps = PatternSet.load("cicd")
        assert ps.error_context == 3
        assert len(ps.block_detectors) == 0
        assert len(ps.domain_keywords) == 7

    def test_journald_config(self):
        ps = PatternSet.load("journald")
        assert ps.error_context == 5
        assert len(ps.block_detectors) == 1
        assert ps.block_detectors[0].name == "audit_bursts"
        assert ps.block_detectors[0].min_count == 5

    def test_config_defaults_when_missing(self):
        ps = PatternSet("test", [], [], config={})
        assert ps.error_context == 3
        assert ps.max_line_len == 500
        assert ps.file_streaming_threshold_mb == 50
        assert ps.blob_markers == []
        assert ps.block_detectors == []
        assert ps.domain_keywords == []


# ── Phase 2: collapse_blocks ─────────────────────────────────────────


class TestCollapseBlocks:
    def test_no_detectors_passthrough(self):
        lines = ["a", "b", "c"]
        assert collapse_blocks(lines, []) == lines

    def test_collapse_above_threshold(self):
        det = BlockConfig(
            name="test",
            match_regex=re.compile(r"^health"),
            min_count=3,
            summary_template="[HEALTH: {count} checks]",
        )
        lines = [
            "health check ok",
            "health check ok",
            "health check ok",
            "health check ok",
            "error: something bad",
        ]
        result = collapse_blocks(lines, [det])
        assert result == ["[HEALTH: 4 checks]", "error: something bad"]

    def test_below_threshold_passthrough(self):
        det = BlockConfig(
            name="test",
            match_regex=re.compile(r"^health"),
            min_count=3,
            summary_template="[HEALTH: {count} checks]",
        )
        lines = ["health check ok", "health check ok", "other line"]
        result = collapse_blocks(lines, [det])
        assert result == lines

    def test_multiple_detectors(self):
        health = BlockConfig(
            name="health",
            match_regex=re.compile(r"healthz"),
            min_count=2,
            summary_template="[HEALTH: {count}]",
        )
        audit = BlockConfig(
            name="audit",
            match_regex=re.compile(r"audit\["),
            min_count=2,
            summary_template="[AUDIT: {count}]",
        )
        lines = [
            "GET /healthz 200",
            "GET /healthz 200",
            "GET /healthz 200",
            "normal line",
            "audit[123]: op1",
            "audit[124]: op2",
        ]
        result = collapse_blocks(lines, [health, audit])
        assert result == ["[HEALTH: 3]", "normal line", "[AUDIT: 2]"]

    def test_mustgather_block_detectors_from_json(self):
        ps = PatternSet.load("mustgather")
        dets = ps.block_detectors
        lines = (
            ['GET /healthz 200 ok'] * 5
            + ["normal log line"]
            + ["successfully renewed lease foo/bar"] * 4
        )
        result = collapse_blocks(lines, dets)
        assert result == [
            "[HEALTH CHECKS: 5 lines, all OK]",
            "normal log line",
            "[LEADER ELECTION: 4 renewals]",
        ]

    def test_journald_block_detectors_from_json(self):
        ps = PatternSet.load("journald")
        dets = ps.block_detectors
        lines = (
            ["audit[100]: type=SYSCALL"] * 6
            + ["kernel: something important"]
        )
        result = collapse_blocks(lines, dets)
        assert result == [
            "[AUDIT BLOCK: 6 entries]",
            "kernel: something important",
        ]


# ── Phase 2: Config-driven predigest ─────────────────────────────────


class TestConfigDrivenPredigest:
    def test_mustgather_health_collapse(self):
        from logdigest.mustgather import predigest

        lines = (
            ['GET /healthz 200 ok'] * 5
            + ["level=error something bad happened"]
        )
        result = predigest("\n".join(lines))
        assert "[HEALTH CHECKS: 5 lines, all OK]" in result
        assert "something bad happened" in result

    def test_mustgather_leader_election_collapse(self):
        from logdigest.mustgather import predigest

        lines = (
            ["successfully renewed lease foo/bar"] * 4
            + ["CrashLoopBackOff pod-xyz"]
        )
        result = predigest("\n".join(lines))
        assert "[LEADER ELECTION: 4 renewals]" in result
        assert "CrashLoopBackOff" in result

    def test_journald_audit_collapse(self):
        from logdigest.journald import predigest

        lines = (
            ["audit[100]: type=SYSCALL msg=test"] * 7
            + ["segfault at 0x42"]
        )
        result = predigest("\n".join(lines))
        assert "[AUDIT BLOCK: 7 entries]" in result
        assert "segfault" in result
