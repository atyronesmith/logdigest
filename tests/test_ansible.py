"""Tests for logdigest Ansible log pre-digestion."""

from logdigest.ansible import (
    analyze_log,
    classify_line,
    find_large_json_blobs,
    find_repeated_ok_blocks,
    find_retry_blocks,
    predigest,
    predigest_file,
    predigest_file_streaming,
)


def test_classify_noise():
    assert classify_line('ok: [host1]').startswith("noise:")
    assert classify_line('skipping: [host1]').startswith("noise:")
    assert classify_line('changed: [host1]').startswith("noise:")
    assert classify_line('FAILED - RETRYING: task (3 retries)').startswith("noise:")
    assert classify_line('').startswith("noise:")


def test_classify_signal():
    assert classify_line('fatal: [host1]: FAILED!').startswith("signal:")
    assert classify_line('PLAY [my play]').startswith("signal:")
    assert classify_line('TASK [my task]').startswith("signal:")
    assert classify_line('PLAY RECAP').startswith("signal:")


def test_classify_unknown():
    assert classify_line('some random log line') == "unknown"


def test_predigest_strips_noise():
    log = """ok: [host1]
skipping: [host2]
changed: [host3]
fatal: [host1]: FAILED! => {"msg": "something broke"}
ok: [host4]"""
    result = predigest(log)
    assert "ok: [host1]" not in result
    assert "skipping:" not in result
    assert "fatal:" in result


def test_predigest_collapses_retries():
    lines = ['FAILED - RETRYING: [host1]: Install package (3 retries left).'] * 5
    lines.append('ok: [host1]')
    log = "\n".join(lines)
    result = predigest(log)
    assert "[RETRY:" in result
    assert "5 retries" in result
    assert "then succeeded" in result


def test_predigest_retries_still_failing():
    lines = ['FAILED - RETRYING: [host1]: Install package (3 retries left).'] * 4
    lines.append('fatal: [host1]: FAILED!')
    log = "\n".join(lines)
    result = predigest(log)
    assert "[RETRY:" in result
    assert "4 retries" in result
    assert "still failing" in result
    assert "fatal:" in result


def test_predigest_collapses_item_loops():
    lines = [f'ok: [host1] => (item=pkg{i})' for i in range(10)]
    log = "\n".join(lines)
    result = predigest(log)
    assert "[ok: 10 items processed]" in result


def test_predigest_item_loop_changed():
    lines = [f'changed: [host1] => (item=svc{i})' for i in range(5)]
    log = "\n".join(lines)
    result = predigest(log)
    assert "[changed: 5 items processed]" in result


def test_predigest_item_loop_under_threshold():
    """Under 3 items: no collapse, but lines are still noise-classified and stripped."""
    lines = [f'ok: [host1] => (item=pkg{i})' for i in range(2)]
    log = "\n".join(lines)
    result = predigest(log)
    assert "[ok:" not in result
    assert result == ""


# ---------------------------------------------------------------------------
# Streaming pre-digestion
# ---------------------------------------------------------------------------

def test_streaming_strips_noise(tmp_path):
    log = "\n".join([
        "ok: [host1]",
        "skipping: [host2]",
        "changed: [host3]",
        "some normal log line",
        "another normal log line",
        "another normal log line",
        "another normal log line",
        "another normal log line",
        "another normal log line",
        'fatal: [host1]: FAILED! => {"msg": "something broke"}',
        "ok: [host4]",
    ])
    path = tmp_path / "ansible.log"
    path.write_text(log)
    result = predigest_file_streaming(path)
    assert "ok: [host1]" not in result
    assert "skipping:" not in result
    assert "fatal:" in result


def test_streaming_preserves_error_context(tmp_path):
    log = "\n".join([
        "normal line 1",
        "normal line 2",
        "normal line 3",
        'fatal: [host1]: FAILED! => {"msg": "crash"}',
        "normal line 4",
    ])
    path = tmp_path / "ansible.log"
    path.write_text(log)
    result = predigest_file_streaming(path)
    assert "fatal:" in result
    assert "normal line 3" in result


def test_streaming_matches_inmemory(tmp_path):
    log = "\n".join([
        "ok: [host1]",
        "some normal log",
        "another normal log",
        "more logs here",
        "yet more logs",
        "still more logs",
        "even more logs",
        "and more logs",
        "and still more",
        'fatal: [host1]: FAILED! => {"msg": "broke"}',
        "final line",
    ])
    path = tmp_path / "ansible.log"
    path.write_text(log)

    inmem = predigest(log)
    streamed = predigest_file_streaming(path)

    assert "fatal:" in inmem
    assert "fatal:" in streamed
    assert "ok: [host1]" not in inmem
    assert "ok: [host1]" not in streamed


# ---------------------------------------------------------------------------
# Block helpers (find_retry_blocks, find_repeated_ok_blocks)
# ---------------------------------------------------------------------------

def test_find_retry_blocks_basic():
    lines = [
        "TASK [Install packages]",
        "FAILED - RETRYING: [host1]: Install packages (3 retries left).",
        "FAILED - RETRYING: [host1]: Install packages (2 retries left).",
        "FAILED - RETRYING: [host1]: Install packages (1 retries left).",
        "ok: [host1]",
    ]
    blocks = find_retry_blocks(lines)
    assert len(blocks) == 1
    assert blocks[0]["count"] == 3
    assert blocks[0]["task"] == "Install packages"
    assert blocks[0]["start"] == 1
    assert blocks[0]["end"] == 4


def test_find_retry_blocks_multiple():
    lines = [
        "FAILED - RETRYING: [host1]: Task A (2 retries left).",
        "FAILED - RETRYING: [host1]: Task A (1 retries left).",
        "ok: [host1]",
        "FAILED - RETRYING: [host2]: Task B (3 retries left).",
        "fatal: [host2]: FAILED!",
    ]
    blocks = find_retry_blocks(lines)
    assert len(blocks) == 2
    assert blocks[0]["task"] == "Task A"
    assert blocks[1]["task"] == "Task B"


def test_find_retry_blocks_none():
    lines = ["ok: [host1]", "TASK [something]", "changed: [host1]"]
    assert find_retry_blocks(lines) == []


def test_find_repeated_ok_blocks():
    lines = [
        "ok: [host1] => (item=pkg1)",
        "ok: [host1] => (item=pkg2)",
        "ok: [host1] => (item=pkg3)",
        "ok: [host1] => (item=pkg4)",
        "TASK [Next step]",
    ]
    blocks = find_repeated_ok_blocks(lines)
    assert len(blocks) == 1
    assert blocks[0]["count"] == 4
    assert blocks[0]["type"] == "item_loop"


def test_find_repeated_ok_blocks_changed():
    lines = [
        "changed: [host1] => (item=svc1)",
        "changed: [host1] => (item=svc2)",
        "changed: [host1] => (item=svc3)",
    ]
    blocks = find_repeated_ok_blocks(lines)
    assert len(blocks) == 1
    assert blocks[0]["count"] == 3


def test_find_repeated_ok_blocks_under_threshold():
    lines = [
        "ok: [host1] => (item=pkg1)",
        "ok: [host1] => (item=pkg2)",
        "TASK [Next]",
    ]
    blocks = find_repeated_ok_blocks(lines)
    assert blocks == []


# ---------------------------------------------------------------------------
# Blob / long-line handling
# ---------------------------------------------------------------------------

def test_find_large_json_blobs_k8s():
    k8s_line = "x" * 300 + " 'metadata': {'name': 'test'} 'spec': {} " + "x" * 300
    lines = ["short line", k8s_line, "another short"]
    blobs = find_large_json_blobs(lines)
    assert len(blobs) == 1
    assert blobs[0]["type"] == "k8s_api_response"
    assert blobs[0]["line"] == 1


def test_find_large_json_blobs_redacted():
    secret_line = "x" * 600 + " REDACTED "
    lines = [secret_line]
    blobs = find_large_json_blobs(lines)
    assert len(blobs) == 1
    assert blobs[0]["type"] == "redacted_secret"


def test_find_large_json_blobs_generic_long():
    long_line = "x" * 600
    lines = [long_line]
    blobs = find_large_json_blobs(lines)
    assert len(blobs) == 1
    assert blobs[0]["type"] == "long_line"


def test_find_large_json_blobs_short_lines():
    lines = ["short", "also short", "'metadata': yes but short"]
    assert find_large_json_blobs(lines) == []


def test_predigest_truncates_k8s_blob():
    """K8s blob on a non-noise line gets truncated."""
    blob = (
        "result: {\"changed\": false, \"result\": "
        "{\"kind\": \"Deployment\", \"metadata\": {\"name\": \"myapp\", "
        "\"namespace\": \"prod\"}, \"spec\": {\"replicas\": 3, "
        "\"template\": " + "x" * 600 + "}}"
    )
    result = predigest(blob)
    assert "[K8S API RESPONSE:" in result
    assert "Deployment/myapp" in result
    assert "prod" in result
    assert "x" * 100 not in result


def test_predigest_blob_on_noise_line_stripped():
    """K8s blob on an ok: line is stripped as noise, never reaching blob handler."""
    blob = (
        "ok: [host1]: => {\"kind\": \"Pod\", \"metadata\": "
        "{\"name\": \"test\"}, \"spec\": " + "x" * 600 + "}"
    )
    result = predigest(blob)
    assert "[K8S API RESPONSE:" not in result
    assert result == ""


def test_predigest_passes_short_lines():
    log = "result: {\"kind\": \"Pod\", \"metadata\": {\"name\": \"x\"}}"
    result = predigest(log)
    assert "[K8S API RESPONSE:" not in result
    assert "Pod" in result


# ---------------------------------------------------------------------------
# TASK header near errors
# ---------------------------------------------------------------------------

def test_predigest_keeps_task_near_error():
    log = "\n".join([
        "TASK [Deploy app]",
        "fatal: [host1]: FAILED! => {\"msg\": \"timeout\"}",
    ])
    result = predigest(log)
    assert "TASK [Deploy app]" in result
    assert "fatal:" in result


def test_predigest_strips_task_far_from_error():
    lines = ["TASK [Unrelated task]"]
    lines.extend(["ok: [host1]"] * 20)
    lines.append("fatal: [host2]: FAILED!")
    result = predigest("\n".join(lines))
    assert "TASK [Unrelated task]" not in result
    assert "fatal:" in result


# ---------------------------------------------------------------------------
# File-based paths
# ---------------------------------------------------------------------------

def test_predigest_file_small(tmp_path):
    log = "\n".join([
        "ok: [host1]",
        "ok: [host2]",
        "fatal: [host1]: FAILED!",
    ])
    path = tmp_path / "small.log"
    path.write_text(log)
    result = predigest_file(path)
    assert "ok:" not in result
    assert "fatal:" in result


def test_predigest_file_streaming_blob(tmp_path):
    blob = (
        "result: {\"kind\": \"ConfigMap\", \"metadata\": "
        "{\"name\": \"cfg1\", \"namespace\": \"default\"}, "
        "\"data\": " + "x" * 600 + "}"
    )
    log = "\n".join([
        "ok: [host1]",
        "some context line",
        "more context",
        "even more context",
        "still going",
        "keeping going",
        "yet more",
        blob,
        "normal line after",
    ])
    path = tmp_path / "blob.log"
    path.write_text(log)
    result = predigest_file_streaming(path)
    assert "ok: [host1]" not in result
    assert "[K8S API RESPONSE:" in result
    assert "ConfigMap/cfg1" in result


def test_analyze_log(tmp_path):
    lines = [
        "TASK [Install packages]",
        "FAILED - RETRYING: [host1]: Install packages (3 retries left).",
        "FAILED - RETRYING: [host1]: Install packages (2 retries left).",
        "ok: [host1]",
        "ok: [host1] => (item=pkg1)",
        "ok: [host1] => (item=pkg2)",
        "ok: [host1] => (item=pkg3)",
        "ok: [host1] => (item=pkg4)",
        "fatal: [host1]: FAILED!",
    ]
    path = tmp_path / "analyze.log"
    path.write_text("\n".join(lines))
    stats = analyze_log(path)
    assert stats["total_lines"] == 9
    assert stats["retry_blocks"] == 1
    assert stats["retry_lines"] == 2
    assert stats["item_loop_blocks"] == 1
    assert stats["item_loop_lines"] == 4
    assert stats["json_blobs"] == 0


# ---------------------------------------------------------------------------
# Integration: realistic log → expected output
# ---------------------------------------------------------------------------

def test_integration_full_playbook():
    """Realistic Ansible playbook output → verify all three compression levels."""
    log = "\n".join([
        "PLAY [Deploy Application] ***",
        "",
        "TASK [Gathering Facts] ***",
        "ok: [webserver1]",
        "ok: [webserver2]",
        "ok: [dbserver1]",
        "",
        "TASK [Install packages] ***",
        "ok: [webserver1] => (item=nginx)",
        "ok: [webserver1] => (item=certbot)",
        "ok: [webserver1] => (item=python3)",
        "ok: [webserver1] => (item=logrotate)",
        "changed: [dbserver1]",
        "",
        "TASK [Configure service] ***",
        "FAILED - RETRYING: [dbserver1]: Configure service (3 retries left).",
        "FAILED - RETRYING: [dbserver1]: Configure service (2 retries left).",
        "FAILED - RETRYING: [dbserver1]: Configure service (1 retries left).",
        "ok: [dbserver1]",
        "ok: [webserver1]",
        "ok: [webserver2]",
        "skipping: [webserver1]",
        "",
        "TASK [Verify deployment] ***",
        "fatal: [dbserver1]: FAILED! => {\"msg\": \"connection refused\"}",
        "",
        "PLAY RECAP ***",
        "webserver1  : ok=3  changed=0  unreachable=0  failed=0",
        "dbserver1   : ok=2  changed=1  unreachable=0  failed=1",
    ])
    result = predigest(log)

    # Level 1: noise stripped
    assert "ok: [webserver1]\n" not in result
    assert "skipping:" not in result

    # Level 2: item loop collapsed
    assert "[ok: 4 items processed]" in result

    # Level 2: retry block collapsed
    assert "[RETRY: Configure service" in result
    assert "3 retries" in result
    assert "then succeeded" in result

    # Signal preserved
    assert "fatal:" in result
    assert "connection refused" in result
    assert "PLAY RECAP" in result

    # TASK near error kept, distant TASKs stripped
    assert "TASK [Verify deployment]" in result
    assert "TASK [Gathering Facts]" not in result

    # Significant compression
    assert len(result) < len(log)


def test_integration_k8s_blob():
    """K8s API response on a non-noise line gets truncated."""
    k8s_response = (
        "debug: {\"changed\": false, \"result\": "
        "{\"apiVersion\": \"v1\", \"kind\": \"Service\", "
        "\"metadata\": {\"name\": \"my-svc\", \"namespace\": \"kube-system\", "
        "\"uid\": \"abc-123\"}, \"spec\": {\"clusterIP\": \"10.0.0.1\", "
        "\"ports\": [{\"port\": 443}], "
        "\"selector\": {\"app\": \"test\"}}, "
        "\"status\": {\"loadBalancer\": " + "x" * 500 + "}}}"
    )
    log = "\n".join([
        "TASK [Get service info] ***",
        k8s_response,
        "fatal: [host1]: FAILED!",
    ])
    result = predigest(log)
    assert "[K8S API RESPONSE: Service/my-svc in kube-system" in result
    assert "x" * 100 not in result
    assert "fatal:" in result
