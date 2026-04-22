"""Tests for Drain3 template mining wrapper."""

from __future__ import annotations

import pytest

drain3 = pytest.importorskip("drain3")

from logdigest.discover.mining import mine_templates  # noqa: E402


class TestMineTemplates:
    def test_repeated_lines_cluster(self):
        lines = ["ok: [host1]"] * 50 + ["ok: [host2]"] * 50
        clusters = mine_templates(lines)
        assert len(clusters) >= 1
        # Should find a template like "ok: [<*>]"
        templates = [c.template for c in clusters]
        assert any("<*>" in t for t in templates)

    def test_distinct_lines_many_clusters(self):
        lines = [f"unique line {i} with content" for i in range(100)]
        clusters = mine_templates(lines)
        # Should find a template with wildcard(s)
        assert len(clusters) >= 1

    def test_empty_input(self):
        clusters = mine_templates([])
        assert clusters == []

    def test_single_line(self):
        clusters = mine_templates(["hello world"])
        assert len(clusters) == 1
        assert clusters[0].size == 1

    def test_cluster_has_frequency(self):
        lines = ["ok: [host1]"] * 80 + ["fatal: [host1]"] * 20
        clusters = mine_templates(lines)
        for c in clusters:
            assert 0 < c.frequency <= 1.0
            assert c.size > 0

    def test_sample_lines_captured(self):
        lines = ["ok: [host1]"] * 20
        clusters = mine_templates(lines, max_samples=5)
        assert len(clusters[0].sample_lines) <= 5
        assert len(clusters[0].sample_lines) > 0

    def test_sorted_by_size(self):
        lines = (
            ["ok: [host1]"] * 100
            + ["fatal: [host1]"] * 10
            + ["changed: [host1]"] * 50
        )
        clusters = mine_templates(lines)
        sizes = [c.size for c in clusters]
        assert sizes == sorted(sizes, reverse=True)

    def test_depth_affects_granularity(self):
        lines = ["GET /api/v1/users 200"] * 50 + ["GET /api/v2/users 200"] * 50
        shallow = mine_templates(lines, depth=3)
        deep = mine_templates(lines, depth=6)
        # Deeper trees tend to produce more specific (more) clusters
        assert len(deep) >= len(shallow)

    def test_ansible_like_corpus(self):
        lines = (
            ["ok: [webserver1]"] * 200
            + ["ok: [dbserver1]"] * 150
            + ["skipping: [webserver1]"] * 100
            + ["changed: [webserver1]"] * 80
            + ["TASK [Install packages]"] * 30
            + ["TASK [Configure service]"] * 30
            + ["fatal: [webserver1]: UNREACHABLE!"] * 5
            + ["PLAY RECAP"] * 10
        )
        clusters = mine_templates(lines)
        # Should discover "ok: [<*>]" as a high-frequency template
        ok_clusters = [c for c in clusters if "ok" in c.template.lower()]
        assert len(ok_clusters) >= 1
        assert ok_clusters[0].size >= 300
