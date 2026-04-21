"""Tests for denoise Ansible log pre-digestion."""

from logdigest.ansible import classify_line, predigest, predigest_file_streaming


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


def test_predigest_collapses_item_loops():
    lines = [f'ok: [host1] => (item=pkg{i})' for i in range(10)]
    log = "\n".join(lines)
    result = predigest(log)
    assert "[ok: 10 items processed]" in result


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
