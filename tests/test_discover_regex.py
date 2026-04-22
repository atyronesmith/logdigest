"""Tests for template-to-regex conversion."""

from __future__ import annotations

import re

from logdigest.discover.regex_convert import template_to_regex


class TestTemplateToRegex:
    def test_exact_template(self):
        result = template_to_regex("PLAY RECAP")
        assert result == r"^PLAY\ RECAP"

    def test_prefix_wildcard(self):
        result = template_to_regex("ok: [<*>")
        assert result == r"^ok:\ \["

    def test_multi_wildcard(self):
        result = template_to_regex("FAILED - RETRYING: [<*>]: <*>")
        assert result is not None
        assert result.startswith("^")
        compiled = re.compile(result)
        assert compiled.search("FAILED - RETRYING: [host1]: some task")

    def test_pure_wildcard_returns_none(self):
        assert template_to_regex("<*>") is None

    def test_short_invariant_returns_none(self):
        assert template_to_regex("<*> a <*>") is None

    def test_empty_returns_none(self):
        assert template_to_regex("") is None

    def test_metachar_escaping(self):
        result = template_to_regex("ok: [host1]")
        assert result is not None
        compiled = re.compile(result)
        assert compiled.search("ok: [host1]")

    def test_brackets_escaped(self):
        result = template_to_regex("fatal: [<*>]")
        assert result is not None
        assert r"\[" in result

    def test_regex_compiles(self):
        templates = [
            "ok: [<*>]",
            "PLAY [<*>] ***",
            "TASK [<*>]",
            "<*> GET /healthz <*> 200",
            "Started <*>.",
        ]
        for t in templates:
            result = template_to_regex(t)
            if result is not None:
                re.compile(result)

    def test_prefix_matches_sample(self):
        result = template_to_regex("skipping: [<*>]")
        compiled = re.compile(result)
        assert compiled.search("skipping: [webserver1]")
        assert compiled.search("skipping: [db-host.example.com]")

    def test_multi_wildcard_matches_sample(self):
        result = template_to_regex("RETRYING: [<*>]: <*> (<*> retries)")
        assert result is not None
        compiled = re.compile(result)
        assert compiled.search("RETRYING: [host1]: install pkg (3 retries)")

    def test_trailing_wildcard_stripped(self):
        result = template_to_regex("error: <*> at line <*>")
        assert result is not None
        assert not result.endswith(".*?")
