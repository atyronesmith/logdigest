"""Tests for logdigest must-gather pre-digestion."""

from pathlib import Path

from logdigest.mustgather import (
    analyze_log,
    classify_line,
    find_health_check_blocks,
    find_k8s_blobs,
    find_leader_election_blocks,
    find_managed_fields_blocks,
    predigest,
    predigest_bundle,
    predigest_file,
    predigest_file_streaming,
)

FIXTURES = Path(__file__).parent / "fixtures" / "must-gather"

# ---------------------------------------------------------------------------
# Level 1: Line classification
# ---------------------------------------------------------------------------

def test_classify_health_checks():
    assert classify_line('"GET /healthz" 200').startswith("noise:")
    assert classify_line('"GET /readyz" 200').startswith("noise:")
    assert classify_line('"GET /livez" 200').startswith("noise:")


def test_classify_leader_election():
    assert classify_line(
        "I0412 successfully renewed lease kube-system/kube-scheduler"
    ).startswith("noise:")


def test_classify_informer():
    assert classify_line("caches populated").startswith("noise:")
    assert classify_line("Starting shared informer factory").startswith("noise:")


def test_classify_yaml_metadata():
    assert classify_line("  resourceVersion: \"12345\"").startswith("noise:")
    assert classify_line("  uid: abc-def-123").startswith("noise:")
    assert classify_line("  creationTimestamp: 2026-01-01T00:00:00Z").startswith("noise:")


def test_classify_managed_fields():
    assert classify_line("  managedFields:").startswith("noise:")
    assert classify_line("  - manager: kubectl").startswith("noise:")
    assert classify_line("    fieldsV1:").startswith("noise:")


def test_classify_errors():
    assert classify_line('level=error msg="something bad"').startswith("signal:")
    assert classify_line("level=fatal crash").startswith("signal:")


def test_classify_panics():
    assert classify_line("panic: runtime error").startswith("signal:")
    assert classify_line("goroutine 1 [running]:").startswith("signal:")


def test_classify_oom():
    assert classify_line("OOMKilled").startswith("signal:")
    assert classify_line("memory cgroup out of memory").startswith("signal:")


def test_classify_pod_failures():
    assert classify_line("CrashLoopBackOff").startswith("signal:")
    assert classify_line("ErrImagePull").startswith("signal:")
    assert classify_line("ImagePullBackOff").startswith("signal:")


def test_classify_node_conditions():
    assert classify_line("NotReady").startswith("signal:")


def test_classify_unknown():
    assert classify_line("some normal operator log line") == "unknown"


# ---------------------------------------------------------------------------
# Level 2: Block detection
# ---------------------------------------------------------------------------

def test_find_health_check_blocks():
    lines = [
        '"GET /healthz" 200 1ms',
        '"GET /healthz" 200 2ms',
        '"GET /healthz" 200 1ms',
        '"GET /readyz" 200 3ms',
        "some other line",
    ]
    blocks = find_health_check_blocks(lines)
    assert len(blocks) == 1
    assert blocks[0]["count"] == 4


def test_find_health_check_blocks_none():
    lines = ["normal line", "another line"]
    assert find_health_check_blocks(lines) == []


def test_find_leader_election_blocks():
    lines = [
        "successfully renewed lease kube-system/kube-scheduler",
        "successfully renewed lease kube-system/kube-scheduler",
        "successfully renewed lease kube-system/kube-scheduler",
        "successfully renewed lease kube-system/kube-scheduler",
        "some other log",
    ]
    blocks = find_leader_election_blocks(lines)
    assert len(blocks) == 1
    assert blocks[0]["count"] == 4


def test_find_managed_fields_blocks():
    lines = [
        "  managedFields:",
        "  - manager: kubectl",
        "    operation: Apply",
        "    fieldsType: FieldsV1",
        "    fieldsV1:",
        "      f:metadata:",
        "  name: my-pod",
    ]
    blocks = find_managed_fields_blocks(lines)
    assert len(blocks) == 1
    assert blocks[0]["count"] == 6  # managedFields through f:metadata


# ---------------------------------------------------------------------------
# Pre-digestion (combined)
# ---------------------------------------------------------------------------

def test_predigest_strips_noise():
    # Noise lines must be far from errors (>3 lines) to be stripped
    log = """caches populated
"GET /healthz" 200 1ms
successfully renewed lease kube-system/sched
Throttling request
some normal operator log
another normal operator log
another normal operator log
another normal operator log
level=error msg="pod crashed"
  resourceVersion: "999" """
    result = predigest(log)
    assert "caches populated" not in result
    assert "Throttling" not in result
    assert "level=error" in result


def test_predigest_collapses_health_checks():
    lines = ['"GET /healthz" 200 1ms'] * 10
    lines.append("level=error msg=\"something broke\"")
    log = "\n".join(lines)
    result = predigest(log)
    assert "[HEALTH CHECKS: 10 lines, all OK]" in result
    assert "level=error" in result


def test_predigest_collapses_leader_election():
    lines = ["successfully renewed lease kube-system/sched"] * 8
    lines.append("some normal log")
    log = "\n".join(lines)
    result = predigest(log)
    assert "[LEADER ELECTION: 8 renewals]" in result


def test_predigest_collapses_managed_fields():
    log = """apiVersion: v1
kind: Pod
metadata:
  name: my-pod
  managedFields:
  - manager: kubectl
    operation: Apply
    fieldsType: FieldsV1
    fieldsV1:
      f:metadata:
  namespace: default"""
    result = predigest(log)
    assert "[MANAGED FIELDS: block omitted]" in result
    assert "fieldsV1" not in result
    assert "kind: Pod" in result


def test_predigest_truncates_k8s_blobs():
    blob = "x" * 300 + " 'metadata': {'name': 'foo'} 'kind': 'Pod' " + "y" * 300
    log = f"normal line\n{blob}\nanother normal line"
    result = predigest(log)
    assert "[K8S API RESPONSE:" in result
    assert "full response omitted" in result
    assert "normal line" in result


def test_predigest_preserves_error_context():
    """Lines near errors should be kept even if they match noise."""
    log = """  resourceVersion: "123"
  uid: abc-def
level=error msg="pod OOMKilled"
  resourceVersion: "456"
normal log line"""
    result = predigest(log)
    # The resourceVersion near the error should be preserved
    assert "level=error" in result


def test_predigest_empty():
    assert predigest("") == ""


def test_predigest_no_noise():
    log = "important log line 1\nimportant log line 2"
    result = predigest(log)
    assert "important log line 1" in result
    assert "important log line 2" in result


# ---------------------------------------------------------------------------
# Streaming pre-digestion
# ---------------------------------------------------------------------------

def test_streaming_strips_noise(tmp_path):
    log = "\n".join([
        "caches populated",
        '"GET /healthz" 200 1ms',
        "successfully renewed lease kube-system/sched",
        "Throttling request",
        "some normal operator log",
        "another normal log",
        "another normal log",
        "another normal log",
        'level=error msg="pod crashed"',
        '  resourceVersion: "999"',
    ])
    path = tmp_path / "pod.log"
    path.write_text(log)
    result = predigest_file_streaming(path)
    assert "caches populated" not in result
    assert "Throttling" not in result
    assert "level=error" in result


def test_streaming_preserves_error_context(tmp_path):
    log = "\n".join([
        "normal line 1",
        "normal line 2",
        "normal line 3",
        'level=error msg="crash"',
        "normal line 4",
        "normal line 5",
    ])
    path = tmp_path / "pod.log"
    path.write_text(log)
    result = predigest_file_streaming(path)
    assert "level=error" in result
    assert "normal line 3" in result


def test_streaming_handles_large_blob(tmp_path):
    blob = "x" * 300 + " 'metadata': {'name': 'foo'} 'kind': 'Pod' " + "y" * 300
    log = f"normal line\n{blob}\nanother line"
    path = tmp_path / "pod.log"
    path.write_text(log)
    result = predigest_file_streaming(path)
    assert "[K8S API RESPONSE:" in result
    assert "normal line" in result


def test_streaming_matches_inmemory(tmp_path):
    """Streaming and in-memory should produce similar output."""
    log = "\n".join([
        "caches populated",
        "normal log",
        "another normal log",
        "more logs here",
        "yet more logs here",
        "still more logs here",
        "even more logs here",
        'level=error msg="something broke"',
        "final line",
    ])
    path = tmp_path / "pod.log"
    path.write_text(log)

    inmem = predigest(log)
    streamed = predigest_file_streaming(path)

    assert "level=error" in inmem
    assert "level=error" in streamed
    # "caches populated" is >3 lines from the error, so both strip it
    assert "caches populated" not in inmem
    assert "caches populated" not in streamed


# ---------------------------------------------------------------------------
# predigest_file
# ---------------------------------------------------------------------------

def test_predigest_file_small(tmp_path):
    log = "\n".join([
        '"GET /healthz" 200 1ms',
        '"GET /healthz" 200 2ms',
        "normal line",
        "another normal line",
        "more normal lines",
        "still more normal lines",
        "even more normal lines",
        'level=error msg="crash"',
    ])
    path = tmp_path / "pod.log"
    path.write_text(log)
    result = predigest_file(path)
    assert "healthz" not in result
    assert "level=error" in result


# ---------------------------------------------------------------------------
# find_k8s_blobs
# ---------------------------------------------------------------------------

def test_find_k8s_blobs_detects():
    blob = "x" * 300 + " 'metadata': {'name': 'foo'} 'kind': 'Pod' " + "y" * 300
    lines = ["short line", blob, "another short"]
    blobs = find_k8s_blobs(lines)
    assert len(blobs) == 1
    assert blobs[0]["line"] == 1


def test_find_k8s_blobs_short_lines():
    lines = ["short", "'metadata': yes but short"]
    assert find_k8s_blobs(lines) == []


# ---------------------------------------------------------------------------
# analyze_log
# ---------------------------------------------------------------------------

def test_analyze_log(tmp_path):
    lines = [
        '"GET /healthz" 200 1ms',
        '"GET /healthz" 200 2ms',
        '"GET /healthz" 200 1ms',
        '"GET /readyz" 200 1ms',
        "successfully renewed lease kube-system/sched",
        "successfully renewed lease kube-system/sched",
        "successfully renewed lease kube-system/sched",
        'level=error msg="crash"',
    ]
    path = tmp_path / "pod.log"
    path.write_text("\n".join(lines))
    stats = analyze_log(path)
    assert stats["total_lines"] == 8
    assert stats["health_check_blocks"] == 1
    assert stats["health_check_lines"] == 4
    assert stats["leader_election_blocks"] == 1
    assert stats["leader_election_lines"] == 3


# ---------------------------------------------------------------------------
# predigest_bundle (fixture-based)
# ---------------------------------------------------------------------------

def test_predigest_bundle_processes_files():
    result = predigest_bundle(FIXTURES)
    assert "files" in result
    assert "skipped" in result
    assert any("pod.log" in k for k in result["files"])
    assert any("configmap.yaml" in k for k in result["files"])


def test_predigest_bundle_strips_noise():
    result = predigest_bundle(FIXTURES)
    pod_log = next(v for k, v in result["files"].items() if "pod.log" in k)
    assert "healthz" not in pod_log
    assert "level=error" in pod_log


def test_predigest_bundle_collapses_managed_fields():
    result = predigest_bundle(FIXTURES)
    cm = next(v for k, v in result["files"].items() if "configmap.yaml" in k)
    assert "[MANAGED FIELDS:" in cm
    assert "fieldsV1" not in cm
    assert "kind: ConfigMap" in cm


def test_predigest_bundle_skips_binary():
    result = predigest_bundle(FIXTURES)
    assert not any("data.bin" in k for k in result["files"])


def test_predigest_bundle_skips_empty():
    result = predigest_bundle(FIXTURES)
    assert not any("empty.log" in k for k in result["files"])
