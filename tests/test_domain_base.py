"""Tests for logdigest domain_base shared machinery."""

import re

from logdigest.domain_base import DomainModule, analyze_lines, classify_line, find_large_blobs

NOISE = [
    (re.compile(r"^DEBUG:"), "debug"),
    (re.compile(r"^\s*$"), "blank"),
]

SIGNAL = [
    (re.compile(r"^ERROR:"), "error"),
    (re.compile(r"^CRITICAL:"), "critical"),
]


def test_classify_noise():
    assert classify_line("DEBUG: something", NOISE, SIGNAL) == "noise:debug"
    assert classify_line("", NOISE, SIGNAL) == "noise:blank"


def test_classify_signal():
    assert classify_line("ERROR: bad thing", NOISE, SIGNAL) == "signal:error"
    assert classify_line("CRITICAL: very bad", NOISE, SIGNAL) == "signal:critical"


def test_classify_unknown():
    assert classify_line("INFO: normal stuff", NOISE, SIGNAL) == "unknown"


def test_classify_noise_takes_priority():
    """If a line matches both noise and signal, noise wins (checked first)."""
    both_noise = [*NOISE, (re.compile(r"^ERROR:"), "also_noise")]
    assert classify_line("ERROR: something", both_noise, SIGNAL) == "noise:also_noise"


def test_classify_strips_whitespace():
    assert classify_line("  DEBUG: indented  ", NOISE, SIGNAL) == "noise:debug"


def test_analyze_lines_counts():
    lines = [
        "DEBUG: trace info",
        "DEBUG: more trace",
        "ERROR: failure",
        "INFO: something normal",
        "",
    ]
    stats = analyze_lines(lines, NOISE, SIGNAL)
    assert stats["total_lines"] == 5
    assert stats["noise_lines"] == 3  # 2 DEBUG + 1 blank
    assert stats["signal_lines"] == 1
    assert stats["unknown_lines"] == 1
    assert stats["noise_pct"] == 60.0


def test_analyze_lines_empty():
    stats = analyze_lines([], NOISE, SIGNAL)
    assert stats["total_lines"] == 0
    assert stats["noise_pct"] == 0


def test_analyze_lines_classifications_detail():
    lines = ["DEBUG: a", "DEBUG: b", "ERROR: c"]
    stats = analyze_lines(lines, NOISE, SIGNAL)
    assert stats["classifications"]["noise:debug"] == 2
    assert stats["classifications"]["signal:error"] == 1


def test_find_large_blobs_no_blobs():
    lines = ["short line", "another short"]
    assert find_large_blobs(lines, min_chars=500) == []


def test_find_large_blobs_by_length():
    lines = ["short", "x" * 600, "also short"]
    blobs = find_large_blobs(lines, min_chars=500)
    assert len(blobs) == 1
    assert blobs[0]["line"] == 1
    assert blobs[0]["length"] == 600
    assert blobs[0]["type"] == "long_line"


def test_find_large_blobs_with_markers():
    lines = [
        "short",
        "x" * 600 + " 'metadata': something",
        "y" * 600,
    ]
    blobs = find_large_blobs(lines, min_chars=500, markers=["'metadata':", "'spec':"])
    assert len(blobs) == 2
    assert blobs[0]["type"] == "'metadata':"
    assert blobs[1]["type"] == "long_line"


def test_find_large_blobs_first_marker_wins():
    line = "z" * 600 + " 'metadata': foo 'spec': bar"
    blobs = find_large_blobs([line], min_chars=500, markers=["'metadata':", "'spec':"])
    assert blobs[0]["type"] == "'metadata':"


class TestDomainModuleProtocol:
    """All domain modules must satisfy the DomainModule protocol."""

    def _check(self, mod):
        assert isinstance(mod, DomainModule)

    def test_ansible(self):
        import logdigest.ansible
        self._check(logdigest.ansible)

    def test_mustgather(self):
        import logdigest.mustgather
        self._check(logdigest.mustgather)

    def test_cicd(self):
        import logdigest.cicd
        self._check(logdigest.cicd)

    def test_journald(self):
        import logdigest.journald
        self._check(logdigest.journald)
