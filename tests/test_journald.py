"""Tests for logdigest systemd journal pre-digestion."""

from logdigest.journald import (
    classify_line,
    find_audit_bursts,
    find_service_restart_loops,
    predigest,
    predigest_file_streaming,
)

# ---------------------------------------------------------------------------
# Level 1: Line classification
# ---------------------------------------------------------------------------

def test_classify_service_lifecycle():
    assert classify_line("Started OpenSSH Server.").startswith("noise:")
    assert classify_line("Stopped OpenSSH Server.").startswith("noise:")
    assert classify_line("Starting NetworkManager...").startswith("noise:")
    assert classify_line("Reached target Multi-User System.").startswith("noise:")
    assert classify_line("Listening on D-Bus System Bus Socket.").startswith("noise:")
    assert classify_line("Finished Cleanup.").startswith("noise:")


def test_classify_pam_session():
    assert classify_line(
        "pam_unix(sshd:session): session opened for user root"
    ).startswith("noise:")
    assert classify_line(
        "pam_unix(sshd:session): session closed for user root"
    ).startswith("noise:")


def test_classify_session_management():
    assert classify_line("New session 42 of user admin").startswith("noise:")
    assert classify_line("Removed session 42").startswith("noise:")


def test_classify_dhcp():
    assert classify_line(
        "dhclient[1234]: bound to 192.168.1.100"
    ).startswith("noise:")
    assert classify_line(
        "NetworkManager: DHCP lease renewed"
    ).startswith("noise:")


def test_classify_cron():
    assert classify_line(
        "CROND[1234]: (root) CMD (/usr/sbin/logrotate)"
    ).startswith("noise:")
    assert classify_line("run-parts: starting /etc/cron.daily").startswith("noise:")


def test_classify_audit_routine():
    assert classify_line(
        "audit[1234]: type=PROCTITLE msg=audit(123:456)"
    ).startswith("noise:")
    assert classify_line(
        "audit[1234]: type=SYSCALL msg=audit(123:456) success=yes"
    ).startswith("noise:")


def test_classify_usb():
    assert classify_line(
        "kernel: usb 1-1: new high speed USB device"
    ).startswith("noise:")


def test_classify_kernel_info():
    assert classify_line(
        "kernel: registered new interface driver usbhid"
    ).startswith("noise:")


def test_classify_oom():
    assert classify_line("Out of memory: Killed process 1234").startswith("signal:")
    assert classify_line("invoked oom-killer: gfp_mask=0x").startswith("signal:")
    assert classify_line("Kill process 1234 (python)").startswith("signal:")


def test_classify_segfault():
    assert classify_line(
        "kernel: myapp[1234]: segfault at 0000000000"
    ).startswith("signal:")
    assert classify_line("general protection fault: 0000").startswith("signal:")


def test_classify_service_failure():
    assert classify_line("Failed to start My Service.").startswith("signal:")
    assert classify_line(
        "myapp.service: Main process exited, code=exited, status=1"
    ).startswith("signal:")
    assert classify_line("myapp.service: Failed with result").startswith("signal:")


def test_classify_disk_errors():
    assert classify_line("I/O error, dev sda, sector 1234").startswith("signal:")
    assert classify_line("ext4 error (device sda1)").startswith("signal:")
    assert classify_line("Buffer I/O error on dev sdb").startswith("signal:")


def test_classify_kernel_panic():
    assert classify_line("Kernel panic - not syncing").startswith("signal:")
    assert classify_line("BUG: soft lockup").startswith("signal:")
    assert classify_line("Call Trace:").startswith("signal:")


def test_classify_selinux():
    assert classify_line(
        "avc:  denied  { read } for  pid=1234"
    ).startswith("signal:")


def test_classify_coredump():
    assert classify_line(
        "Process 1234 (myapp) dumped core"
    ).startswith("signal:")


def test_classify_unknown():
    assert classify_line("some custom application log line") == "unknown"


# ---------------------------------------------------------------------------
# Level 2: Block detection
# ---------------------------------------------------------------------------

def test_find_service_restart_loops():
    lines = [
        "Starting My Service...",
        "Started My Service.",
        "Stopping My Service...",
        "Stopped My Service.",
        "Starting My Service...",
        "Started My Service.",
        "some other line",
    ]
    blocks = find_service_restart_loops(lines)
    assert len(blocks) == 1
    assert blocks[0]["count"] == 6
    assert blocks[0]["service"] == "My Service"


def test_find_service_restart_loops_too_few():
    lines = [
        "Started My Service.",
        "Stopped My Service.",
        "other line",
    ]
    blocks = find_service_restart_loops(lines)
    assert blocks == []


def test_find_audit_bursts():
    lines = [
        "audit[1234]: type=SYSCALL msg=something",
        "audit[1234]: type=CWD msg=something",
        "audit[1234]: type=PATH msg=something",
        "audit[1234]: type=PROCTITLE msg=something",
        "audit[1234]: type=EOE msg=something",
        "some other line",
    ]
    blocks = find_audit_bursts(lines)
    assert len(blocks) == 1
    assert blocks[0]["count"] == 5


def test_find_audit_bursts_too_few():
    lines = [
        "audit[1234]: type=SYSCALL msg=something",
        "audit[1234]: type=EOE msg=something",
        "other line",
    ]
    assert find_audit_bursts(lines) == []


# ---------------------------------------------------------------------------
# Pre-digestion (combined)
# ---------------------------------------------------------------------------

def test_predigest_strips_noise():
    log = """Started OpenSSH Server.
pam_unix(sshd:session): session opened for user root
New session 42 of user admin
some custom application log line
another application log line
another application log line
another application log line
another application log line
CROND[1234]: (root) CMD (/usr/sbin/logrotate)"""
    result = predigest(log)
    assert "OpenSSH" not in result
    assert "pam_unix" not in result
    assert "CROND" not in result
    assert "custom application" in result


def test_predigest_collapses_restart_loops():
    lines = [
        "Starting My Service...",
        "Started My Service.",
        "Stopping My Service...",
        "Stopped My Service.",
        "Starting My Service...",
        "Started My Service.",
        "Stopping My Service...",
        "Stopped My Service.",
        "some normal log line",
    ]
    result = predigest("\n".join(lines))
    assert "[SERVICE RESTART LOOP: My Service, 8 events]" in result
    assert "some normal log line" in result


def test_predigest_collapses_audit_bursts():
    lines = [
        "audit[1234]: type=SYSCALL msg=audit(1)",
        "audit[1234]: type=CWD msg=audit(1)",
        "audit[1234]: type=PATH msg=audit(1)",
        "audit[1234]: type=PROCTITLE msg=audit(1)",
        "audit[1234]: type=EOE msg=audit(1)",
        "audit[1234]: type=SYSCALL msg=audit(2)",
        "audit[1234]: type=EOE msg=audit(2)",
        "some normal log line",
    ]
    result = predigest("\n".join(lines))
    assert "[AUDIT BLOCK: 7 entries]" in result
    assert "some normal log line" in result


def test_predigest_preserves_errors():
    log = """Started My Service.
myapp.service: Main process exited, code=exited, status=1
Failed to start My Service.
I/O error, dev sda, sector 1234"""
    result = predigest(log)
    assert "exited, status=1" in result
    assert "Failed to start" in result
    assert "I/O error" in result


def test_predigest_preserves_error_context():
    """Lines near errors are kept even if they match noise."""
    log = """Started My Service.
Starting My Service...
myapp.service: Main process exited, code=exited, status=1
Stopped My Service.
some normal line"""
    result = predigest(log)
    assert "exited, status=1" in result


def test_predigest_truncates_stack_traces():
    lines = ["Call Trace:"]
    # Simulate a 20-frame stack trace
    for i in range(20):
        lines.append(f" [<ffffffff{i:08x}>] function_{i}+0x{i:x}/0x100")
    lines.append("some line after trace")
    result = predigest("\n".join(lines))
    assert "frames omitted" in result
    assert "some line after trace" in result


def test_predigest_keeps_short_traces():
    lines = ["Call Trace:"]
    for i in range(5):
        lines.append(f" [<ffffffff{i:08x}>] function_{i}+0x{i:x}/0x100")
    lines.append("after trace")
    result = predigest("\n".join(lines))
    assert "frames omitted" not in result
    assert "function_0" in result
    assert "function_4" in result


def test_predigest_strips_date_from_timestamps():
    log = "Apr 12 14:30:05 myhost kernel: some message"
    result = predigest(log)
    assert "14:30:05" in result
    assert "Apr 12" not in result


def test_predigest_keeps_timestamps_when_requested():
    log = "Apr 12 14:30:05 myhost kernel: some message"
    result = predigest(log, keep_timestamps=True)
    assert "Apr 12" in result


def test_predigest_empty():
    assert predigest("") == ""


def test_predigest_no_noise():
    log = "custom app line 1\ncustom app line 2"
    result = predigest(log)
    assert "custom app line 1" in result
    assert "custom app line 2" in result


# ---------------------------------------------------------------------------
# Streaming pre-digestion
# ---------------------------------------------------------------------------

def test_streaming_strips_noise(tmp_path):
    log = "\n".join([
        "Started OpenSSH Server.",
        "pam_unix(sshd:session): session opened for user root",
        "New session 42 of user admin",
        "some custom application log line",
        "another application log line",
        "another application log line",
        "another application log line",
        "another application log line",
        "another application log line",
        "another application log line",
        "another application log line",
        "CROND[1234]: (root) CMD (/usr/sbin/logrotate)",
    ])
    path = tmp_path / "journal.log"
    path.write_text(log)
    result = predigest_file_streaming(path)
    assert "OpenSSH" not in result
    assert "pam_unix" not in result
    assert "CROND" not in result
    assert "custom application" in result


def test_streaming_preserves_errors(tmp_path):
    log = "\n".join([
        "Started My Service.",
        "normal line",
        "normal line",
        "myapp.service: Main process exited, code=exited, status=1",
        "Failed to start My Service.",
        "I/O error, dev sda, sector 1234",
    ])
    path = tmp_path / "journal.log"
    path.write_text(log)
    result = predigest_file_streaming(path)
    assert "exited, status=1" in result
    assert "I/O error" in result


def test_streaming_matches_inmemory(tmp_path):
    log = "\n".join([
        "Started OpenSSH Server.",
        "custom app log",
        "another custom log",
        "more custom logs",
        "yet more custom logs",
        "still more custom logs",
        "even more custom logs",
        "and more custom logs",
        "and still more custom logs",
        "and even more logs",
        "and yet more logs",
        "myapp.service: Main process exited, code=exited, status=1",
        "final line",
    ])
    path = tmp_path / "journal.log"
    path.write_text(log)

    inmem = predigest(log)
    streamed = predigest_file_streaming(path)

    assert "exited, status=1" in inmem
    assert "exited, status=1" in streamed
    assert "OpenSSH" not in inmem
    assert "OpenSSH" not in streamed
