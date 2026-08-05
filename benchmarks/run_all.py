"""Run all logdigest benchmarks."""

import sys
import importlib
from pathlib import Path

BENCHMARKS = [
    "bench_token_savings",
    "bench_entropy_prediction",
]


def main():
    bench_dir = Path(__file__).parent
    sys.path.insert(0, str(bench_dir))

    results = {}
    for name in BENCHMARKS:
        print(f"\n{'─' * 60}")
        print(f"Running: {name}")
        print(f"{'─' * 60}\n")

        mod = importlib.import_module(name)
        passed = mod.run()
        results[name] = passed

    print(f"\n{'═' * 60}")
    print("Summary")
    print(f"{'═' * 60}")
    all_passed = True
    for name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  {name}: {status}")
        if not passed:
            all_passed = False

    print()
    if all_passed:
        print("All benchmarks passed.")
    else:
        print("Some benchmarks failed.")
    return all_passed


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
