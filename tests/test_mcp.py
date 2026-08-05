"""Tests for denoise MCP server tools.

Tests the tool functions directly (not the MCP transport layer).
"""

from logdigest.mcp import predigest, detect_domain, entropy_check, _detect_domain


# ---------------------------------------------------------------------------
# Domain detection
# ---------------------------------------------------------------------------

def test_detect_ansible():
    log = "PLAY [deploy app]\nTASK [install packages]\nok: [host1]\nfatal: [host2]"
    assert _detect_domain(log) == "ansible"


def test_detect_mustgather():
    log = '"GET /healthz" 200\nsuccessfully renewed lease\nCrashLoopBackOff'
    assert _detect_domain(log) == "mustgather"


def test_detect_cicd():
    log = "##[group] Run tests\n##[debug] something\nCollecting requests"
    assert _detect_domain(log) == "cicd"


def test_detect_journald():
    log = "systemd[1]: Started sshd\nkernel: something\naudit[123]: type=SYSCALL"
    assert _detect_domain(log) == "journald"


def test_detect_unknown():
    assert _detect_domain("nothing recognizable here at all") is None


def test_detect_domain_tool():
    assert detect_domain("PLAY [test]\nTASK [do thing]\nok: [host]") == "ansible"
    assert detect_domain("just some random text") == "unknown"


# ---------------------------------------------------------------------------
# Predigest tool
# ---------------------------------------------------------------------------

def test_predigest_ansible():
    log = "ok: [host1]\nskipping: [host2]\nfatal: [host1]: FAILED!"
    result = predigest(log, domain="ansible")
    assert "ok: [host1]" not in result
    assert "fatal:" in result


def test_predigest_mustgather():
    lines = ['"GET /healthz" 200 1ms'] * 5
    lines.append("level=error msg=\"something broke\"")
    log = "\n".join(lines)
    result = predigest(log, domain="mustgather")
    assert "[HEALTH CHECKS:" in result
    assert "level=error" in result


def test_predigest_cicd():
    lines = [
        "  Collecting requests>=2.0",
        "  Collecting flask>=2.0",
        "  Downloading requests-2.28.whl",
        "  Installing collected packages: requests, flask",
        "  Successfully installed flask-2.2 requests-2.28",
        "FAILED tests/test_something.py",
    ]
    result = predigest("\n".join(lines), domain="cicd")
    assert "[DEPENDENCY INSTALL:" in result
    assert "FAILED" in result


def test_predigest_journald():
    # Noise must be >5 lines from signal to be stripped
    lines = [
        "Started OpenSSH Server.",
        "pam_unix(sshd:session): session opened for user root",
        "some app log line",
        "another app log line",
        "more app output here",
        "yet another app line",
        "still more app output",
        "app doing things",
        "I/O error, dev sda",
    ]
    result = predigest("\n".join(lines), domain="journald")
    assert "OpenSSH" not in result
    assert "I/O error" in result


def test_predigest_auto_detect():
    log = "PLAY [test]\nTASK [install]\nok: [host1]\nok: [host2]\nfatal: [host3]: FAILED!"
    result = predigest(log)
    assert "ok:" not in result
    assert "fatal:" in result


def test_predigest_fallback():
    """Unknown domain falls back to core predigest_document."""
    log = "normal line\n*****\nnormal line 2"
    result = predigest(log, domain="nonexistent")
    # Core predigest strips separators
    assert "*****" not in result
    assert "normal line" in result


def test_predigest_empty():
    assert predigest("") == ""
    assert predigest("   ") == "   "


# ---------------------------------------------------------------------------
# Entropy check tool
# ---------------------------------------------------------------------------

def test_entropy_check_high_redundancy():
    docs = ["\n".join(["same line"] * 50) for _ in range(10)]
    report = entropy_check(docs)
    assert "HIGH" in report
    assert "10 documents" in report


def test_entropy_check_low_redundancy():
    docs = ["\n".join([f"unique d{d} l{i}" for i in range(50)]) for d in range(10)]
    report = entropy_check(docs)
    assert "HIGH" not in report
