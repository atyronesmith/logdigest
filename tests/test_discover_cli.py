"""Tests for the logdigest discover CLI command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

drain3 = pytest.importorskip("drain3")
click = pytest.importorskip("click")

from click.testing import CliRunner

from logdigest.cli import cli


@pytest.fixture
def corpus_dir(tmp_path):
    """Create a synthetic corpus directory with Ansible-like logs."""
    for i in range(20):
        lines = (
            ["ok: [host1]"] * 50
            + ["ok: [host2]"] * 30
            + ["skipping: [host1]"] * 20
            + ["changed: [host1]"] * 10
            + [f"TASK [task_{j}]" for j in range(5)]
            + ["fatal: [host1]: UNREACHABLE!"] * 2
            + ["PLAY RECAP"] * 1
        )
        (tmp_path / f"log_{i:03d}.txt").write_text("\n".join(lines))
    return tmp_path


class TestDiscoverCLI:
    def test_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["discover", "--help"])
        assert result.exit_code == 0
        assert "corpus_dir" in result.output.lower() or "CORPUS_DIR" in result.output

    def test_basic_run(self, corpus_dir):
        runner = CliRunner()
        result = runner.invoke(cli, [
            "discover", str(corpus_dir), "-d", "test",
        ])
        assert result.exit_code == 0
        assert "Templates discovered:" in result.output

    def test_output_to_file(self, corpus_dir, tmp_path):
        output_path = tmp_path / "output.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "discover", str(corpus_dir), "-d", "test", "-o", str(output_path),
        ])
        assert result.exit_code == 0
        assert output_path.exists()

        data = json.loads(output_path.read_text())
        assert data["domain"] == "test"
        assert "noise_patterns" in data
        assert "signal_patterns" in data
        assert data["corpus_size"] == 20

    def test_output_valid_json(self, corpus_dir):
        runner = CliRunner()
        result = runner.invoke(cli, [
            "discover", str(corpus_dir), "-d", "ansible",
        ])
        assert result.exit_code == 0
        # Find JSON in output (after the status lines)
        lines = result.output.strip().split("\n")
        json_start = None
        for i, line in enumerate(lines):
            if line.strip().startswith("{"):
                json_start = i
                break
        if json_start is not None:
            json_text = "\n".join(lines[json_start:])
            data = json.loads(json_text)
            assert data["domain"] == "ansible"

    def test_discovers_noise_patterns(self, corpus_dir, tmp_path):
        output_path = tmp_path / "output.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "discover", str(corpus_dir), "-d", "test", "-o", str(output_path),
        ])
        assert result.exit_code == 0
        data = json.loads(output_path.read_text())
        assert len(data["noise_patterns"]) > 0

    def test_discovers_signal_patterns(self, corpus_dir, tmp_path):
        output_path = tmp_path / "output.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "discover", str(corpus_dir), "-d", "test", "-o", str(output_path),
        ])
        assert result.exit_code == 0
        data = json.loads(output_path.read_text())
        # Should find "fatal" as a signal
        signal_labels = [p["label"] for p in data["signal_patterns"]]
        assert any("fatal" in l for l in signal_labels)

    def test_empty_corpus(self, tmp_path):
        runner = CliRunner()
        result = runner.invoke(cli, [
            "discover", str(tmp_path), "-d", "test",
        ])
        assert result.exit_code != 0

    def test_reference_flag(self, corpus_dir, tmp_path):
        # Use existing ansible.json as reference
        import importlib.resources
        patterns_pkg = importlib.resources.files("logdigest.patterns")
        ref_path = patterns_pkg.joinpath("ansible.json")

        output_path = tmp_path / "output.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "discover", str(corpus_dir), "-d", "test",
            "-o", str(output_path),
            "--reference", str(ref_path),
        ])
        assert result.exit_code == 0

    def test_custom_depth_and_similarity(self, corpus_dir, tmp_path):
        output_path = tmp_path / "output.json"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "discover", str(corpus_dir), "-d", "test",
            "-o", str(output_path),
            "--depth", "6", "--similarity", "0.3",
        ])
        assert result.exit_code == 0
