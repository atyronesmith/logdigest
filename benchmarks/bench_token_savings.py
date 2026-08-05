"""Benchmark: token savings from Ansible log pre-digestion.

Generates a synthetic corpus matching the entropy profile of real Ansible
logs, pre-digests each log, and measures raw vs digested size.

Expected: 80%+ character savings, matching the 83.1% headline on real logs.
"""

import sys
import statistics
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from generate_corpus import generate_corpus

from denoise.ansible import predigest


def run(n_logs=200):
    corpus = generate_corpus(n_logs)

    raw_chars = 0
    digest_chars = 0
    savings_per_log = []

    for log in corpus:
        raw_len = len(log)
        digested = predigest(log)
        dig_len = len(digested)

        raw_chars += raw_len
        digest_chars += dig_len
        if raw_len > 0:
            savings_per_log.append(1 - dig_len / raw_len)

    overall_savings = 1 - digest_chars / raw_chars if raw_chars > 0 else 0
    median_savings = statistics.median(savings_per_log)
    p10 = sorted(savings_per_log)[int(len(savings_per_log) * 0.1)]
    p90 = sorted(savings_per_log)[int(len(savings_per_log) * 0.9)]

    print(f"Token Savings Benchmark ({n_logs} synthetic Ansible logs)")
    print("=" * 55)
    print(f"Total raw chars:      {raw_chars:>12,}")
    print(f"Total digest chars:   {digest_chars:>12,}")
    print(f"Overall savings:      {overall_savings:>11.1%}")
    print(f"Median per-log:       {median_savings:>11.1%}")
    print(f"P10-P90 range:        {p10:.1%} - {p90:.1%}")
    print()

    if overall_savings < 0.70:
        print("FAIL: savings below 70% threshold")
        return False

    print("PASS: savings above 70% threshold")
    return True


if __name__ == "__main__":
    success = run()
    sys.exit(0 if success else 1)
