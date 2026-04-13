"""Tests for denoise CI/CD log pre-digestion."""

from denoise.cicd import (
    classify_line,
    detect_ci_system,
    predigest,
    find_install_blocks,
    find_container_pull_blocks,
)


# ---------------------------------------------------------------------------
# Level 1: Line classification
# ---------------------------------------------------------------------------

def test_classify_pip_install():
    assert classify_line("  Collecting requests>=2.0").startswith("noise:")
    assert classify_line("  Successfully installed flask-2.0").startswith("noise:")
    assert classify_line("  Requirement already satisfied: pip").startswith("noise:")


def test_classify_npm_install():
    assert classify_line("added 150 packages in 5s").startswith("noise:")
    assert classify_line("npm warn deprecated package@1.0").startswith("noise:")


def test_classify_apt_install():
    assert classify_line("  Setting up libssl3 (3.0.2-1)").startswith("noise:")
    assert classify_line("  Unpacking libssl3 (3.0.2-1)").startswith("noise:")
    assert classify_line("  Get:1 http://archive.ubuntu.com focal InRelease").startswith("noise:")


def test_classify_container_pull():
    assert classify_line("  Pulling from library/python").startswith("noise:")
    assert classify_line("  Digest: sha256:abc123").startswith("noise:")
    assert classify_line("  Status: Downloaded newer image").startswith("noise:")


def test_classify_container_layers():
    assert classify_line("abc123def456: Waiting").startswith("noise:")
    assert classify_line("abc123def456: Pull complete").startswith("noise:")


def test_classify_download_progress():
    assert classify_line("Downloading package-1.0.tar.gz (50%)").startswith("noise:")


def test_classify_cache_ops():
    assert classify_line("  Cache restored successfully").startswith("noise:")
    assert classify_line("  Cache saved successfully").startswith("noise:")


def test_classify_gha_debug():
    assert classify_line("##[debug] some debug info").startswith("noise:")
    assert classify_line("##[group] Run tests").startswith("noise:")
    assert classify_line("##[endgroup]").startswith("noise:")


def test_classify_jenkins_pipeline():
    assert classify_line("[Pipeline] Start of Pipeline").startswith("noise:")
    assert classify_line("  Started by user admin").startswith("noise:")


def test_classify_gitlab_executor():
    assert classify_line("  Using Docker executor with image python:3.11").startswith("noise:")
    assert classify_line("  Uploading artifacts to coordinator").startswith("noise:")


def test_classify_errors():
    assert classify_line("ERROR: build failed").startswith("signal:")
    assert classify_line("FATAL: cannot continue").startswith("signal:")
    assert classify_line("Process completed with exit code 1").startswith("signal:")


def test_classify_test_failures():
    assert classify_line("FAILED tests/test_something.py").startswith("signal:")
    assert classify_line("FAIL: test_addition").startswith("signal:")


def test_classify_build_failures():
    assert classify_line("Build FAILED with errors").startswith("signal:")
    assert classify_line("COMPILATION ERROR").startswith("signal:")


def test_classify_timeouts():
    assert classify_line("Timed out waiting for condition").startswith("signal:")
    assert classify_line("exceeded the maximum time limit").startswith("signal:")


def test_classify_gha_error():
    assert classify_line("##[error] Process completed with exit code 1").startswith("signal:")


def test_classify_unknown():
    assert classify_line("Running test suite...") == "unknown"


# ---------------------------------------------------------------------------
# CI system detection
# ---------------------------------------------------------------------------

def test_detect_github_actions():
    log = "##[group] Run tests\nGITHUB_WORKSPACE=/home/runner\nsome other line"
    assert detect_ci_system(log) == "github_actions"


def test_detect_jenkins():
    log = "Started by user admin\n[Pipeline] Start of Pipeline\nBuilding in workspace /var/jenkins"
    assert detect_ci_system(log) == "jenkins"


def test_detect_gitlab_ci():
    log = "Using Docker executor with image python:3.11\nGetting source from Git repository"
    assert detect_ci_system(log) == "gitlab_ci"


def test_detect_unknown():
    log = "some generic log output\nnothing specific here"
    assert detect_ci_system(log) is None


# ---------------------------------------------------------------------------
# Level 2: Block detection
# ---------------------------------------------------------------------------

def test_find_install_blocks():
    lines = [
        "  Collecting requests>=2.0",
        "  Collecting flask>=2.0",
        "  Downloading requests-2.28.whl",
        "  Downloading flask-2.2.whl",
        "  Installing collected packages: requests, flask",
        "  Successfully installed flask-2.2 requests-2.28",
        "Running tests...",
    ]
    blocks = find_install_blocks(lines)
    assert len(blocks) == 1
    assert blocks[0]["count"] == 6
    assert blocks[0]["pkg_manager"] == "pip"


def test_find_install_blocks_apt():
    lines = [
        "  Setting up libssl3",
        "  Unpacking libssl3",
        "  Setting up curl",
        "  Unpacking curl",
        "done.",
    ]
    blocks = find_install_blocks(lines)
    assert len(blocks) == 1
    assert blocks[0]["pkg_manager"] == "apt"


def test_find_install_blocks_none():
    lines = ["normal line", "another line"]
    assert find_install_blocks(lines) == []


def test_find_container_pull_blocks():
    lines = [
        "Pulling from library/python",
        "abc123def456: Downloading",
        "abc123def456: Pull complete",
        "def789abc012: Downloading",
        "def789abc012: Pull complete",
        "Digest: sha256:abc123",
        "Status: Downloaded newer image",
        "Running container...",
    ]
    blocks = find_container_pull_blocks(lines)
    assert len(blocks) == 1
    assert blocks[0]["count"] == 7
    assert blocks[0]["image"] == "library/python"


# ---------------------------------------------------------------------------
# Pre-digestion (combined)
# ---------------------------------------------------------------------------

def test_predigest_strips_noise():
    log = """##[group] Run tests
  Collecting requests>=2.0
  Successfully installed requests-2.28
normal output line
more normal output
more normal output
more normal output
more normal output
##[debug] some debug info"""
    result = predigest(log)
    assert "##[group]" not in result
    assert "##[debug]" not in result
    assert "normal output" in result


def test_predigest_collapses_installs():
    lines = [
        "  Collecting requests>=2.0",
        "  Collecting flask>=2.0",
        "  Downloading requests-2.28.whl",
        "  Downloading flask-2.2.whl",
        "  Installing collected packages: requests, flask",
        "  Successfully installed flask-2.2 requests-2.28",
        "Running tests...",
    ]
    result = predigest("\n".join(lines))
    assert "[DEPENDENCY INSTALL: 6 lines via pip]" in result
    assert "Running tests..." in result
    assert "Collecting" not in result


def test_predigest_collapses_container_pulls():
    lines = [
        "Pulling from library/python",
        "abc123def456: Downloading",
        "abc123def456: Pull complete",
        "def789abc012: Pull complete",
        "Digest: sha256:abc",
        "Status: Downloaded newer image",
        "Starting container...",
    ]
    result = predigest("\n".join(lines))
    assert "[CONTAINER PULL:" in result
    assert "library/python" in result
    assert "Starting container..." in result


def test_predigest_preserves_errors():
    log = """##[group] Run tests
FAILED tests/test_something.py::test_it
ERROR: 1 failed, 5 passed
##[error] Process completed with exit code 1"""
    result = predigest(log)
    assert "FAILED" in result
    assert "ERROR:" in result
    assert "exit code 1" in result


def test_predigest_preserves_error_context():
    """Lines near errors kept even if they match noise."""
    log = """  Collecting requests>=2.0
FAILED tests/test_something.py
  Successfully installed requests-2.28"""
    result = predigest(log)
    assert "FAILED" in result


def test_predigest_auto_detects_ci():
    gha_log = "##[group] Checkout\n##[endgroup]\nnormal line"
    result = predigest(gha_log)
    assert "##[group]" not in result


def test_predigest_empty():
    assert predigest("") == ""


def test_predigest_no_noise():
    log = "step 1: running tests\nstep 2: all passed"
    result = predigest(log)
    assert "step 1" in result
    assert "step 2" in result
