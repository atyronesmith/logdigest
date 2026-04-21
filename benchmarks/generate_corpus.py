"""Generate synthetic Ansible log corpus for benchmarking.

Produces logs that match the entropy profile of real Ansible job logs:
~80% noise lines (ok, skipping, changed, retries, item loops, timestamps),
~5% signal lines (fatal, failed, error), ~15% structural (PLAY/TASK headers).
"""

import random
import string

HOSTS = [f"node-{i}.example.com" for i in range(1, 21)]
TASKS = [
    "Gather Facts",
    "Install required packages",
    "Configure firewall rules",
    "Deploy application",
    "Set up monitoring",
    "Create service account",
    "Apply security policies",
    "Configure DNS",
    "Set up load balancer",
    "Initialize database",
    "Configure TLS certificates",
    "Deploy ingress controller",
    "Set cluster labels",
    "Validate network policies",
    "Run health checks",
]
PLAYS = [
    "Setup Infrastructure",
    "Deploy Application Stack",
    "Configure Networking",
    "Security Hardening",
    "Post-deployment Validation",
]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
MONTHS = ["January", "February", "March", "April", "May", "June"]


def _timestamp():
    day = random.choice(DAYS)
    d = random.randint(1, 28)
    month = random.choice(MONTHS)
    h, m, s = random.randint(0, 23), random.randint(0, 59), random.randint(0, 59)
    return f"{day} {d:02d} {month} 2026  {h:02d}:{m:02d}:{s:02d} +0000 (0:00:12.345)"


def _k8s_blob():
    name = "".join(random.choices(string.ascii_lowercase, k=8))
    ns = random.choice(["default", "kube-system", "openshift-monitoring"])
    return (
        f"{{'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {{'name': '{name}', "
        f"'namespace': '{ns}', 'labels': {{'app': '{name}'}}, "
        f"'managedFields': [{{'manager': 'kubectl', 'operation': 'Update', "
        f"'fieldsV1': {{'f:metadata': {{'f:labels': {{'f:app': {{}}}}}}}}"
        + "}" * 5
        + ", 'spec': {'containers': [{'name': '"
        + name
        + "', 'image': 'registry.example.com/"
        + name
        + ":latest'}]}"
        + "}" * 2
    )


def generate_log(n_tasks=None, include_failure=True):
    """Generate a single synthetic Ansible log."""
    if n_tasks is None:
        n_tasks = random.randint(15, 60)

    lines = []
    lines.append(_timestamp())
    lines.append("")

    n_plays = random.randint(1, min(4, max(1, n_tasks // 10)))
    tasks_per_play = n_tasks // n_plays

    failure_task = random.randint(n_tasks // 2, n_tasks - 1) if include_failure else -1

    task_idx = 0
    for play_i in range(n_plays):
        play = random.choice(PLAYS)
        lines.append(f"PLAY [{play}] {'*' * 60}")
        lines.append("")

        for _ in range(tasks_per_play):
            task = random.choice(TASKS)
            lines.append(f"TASK [{task}] {'*' * 60}")
            lines.append(_timestamp())

            host = random.choice(HOSTS)

            if task_idx == failure_task:
                # Retry block then failure
                retry_count = random.randint(3, 15)
                for r in range(retry_count):
                    lines.append(
                        f"FAILED - RETRYING: [{host}]: {task} "
                        f"({retry_count - r} retries left)."
                    )
                lines.append(f'fatal: [{host}]: FAILED! => {{"msg": "Connection timed out", "rc": 1}}')
                lines.append(f'    "msg": "Failed to connect to {host}:443"')
                lines.append(f'    "stderr": "curl: (7) Failed to connect"')
                lines.append(f'    "rc": "1"')
            else:
                # Normal noise output
                n_hosts = random.randint(1, 5)
                for h_i in range(n_hosts):
                    h = random.choice(HOSTS)
                    r = random.random()
                    if r < 0.5:
                        lines.append(f"ok: [{h}]")
                    elif r < 0.7:
                        lines.append(f"skipping: [{h}]")
                    elif r < 0.85:
                        lines.append(f"changed: [{h}]")
                    else:
                        lines.append(f"ok: [{h}] => (item={random.choice(TASKS)})")

                # Occasional item loops
                if random.random() < 0.2:
                    item_count = random.randint(5, 20)
                    for _ in range(item_count):
                        lines.append(
                            f"ok: [{host}] => (item=package-{random.randint(1, 50)})"
                        )

                # Occasional K8s API blob
                if random.random() < 0.1:
                    lines.append(_k8s_blob())

                # Occasional JSON fragments
                if random.random() < 0.3:
                    lines.append(f'"changed": {random.choice(["true", "false"])},')
                    lines.append(f'"msg": "All assertions passed",')

            lines.append("")
            task_idx += 1

    # Play recap
    lines.append("PLAY RECAP " + "*" * 60)
    for h in random.sample(HOSTS, min(5, len(HOSTS))):
        ok = random.randint(10, 50)
        changed = random.randint(0, 10)
        failed = 1 if include_failure else 0
        lines.append(
            f"{h:<40} : ok={ok}    changed={changed}    "
            f"unreachable=0    failed={failed}    skipped={random.randint(0, 5)}    "
            f"rescued=0    ignored=0"
        )

    return "\n".join(lines)


def generate_corpus(n_logs=100, failure_rate=0.6):
    """Generate a corpus of synthetic Ansible logs."""
    return [
        generate_log(include_failure=random.random() < failure_rate)
        for _ in range(n_logs)
    ]


if __name__ == "__main__":
    corpus = generate_corpus(10)
    for i, log in enumerate(corpus):
        n_lines = len(log.splitlines())
        print(f"Log {i}: {n_lines} lines, {len(log)} chars")
