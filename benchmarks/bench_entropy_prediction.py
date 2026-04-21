"""Benchmark: entropy pre-flight predicts pre-digestion impact.

Verifies that corpus_entropy() correctly predicts "high" impact for
noisy log corpora and "none"/"low" for clean text corpora.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from generate_corpus import generate_corpus

from logdigest.entropy import corpus_entropy


def run():
    print("Entropy Prediction Benchmark")
    print("=" * 55)

    passed = True

    # High-redundancy corpus: synthetic Ansible logs
    noisy = generate_corpus(50)
    noisy_result = corpus_entropy(noisy)
    print(f"Ansible logs ({len(noisy)} docs):")
    print(f"  Overall redundancy:   {noisy_result['overall_redundancy']:.1%}")
    print(f"  Predicted impact:     {noisy_result['predicted_impact']}")
    print(f"  Within-doc redundancy:{noisy_result['within_doc_redundancy']:.1%}")
    print(f"  Cross-doc repetition: {noisy_result['cross_doc_repetition_rate']:.1%}")
    if noisy_result["predicted_impact"] not in ("high", "medium"):
        print("  FAIL: expected high or medium impact")
        passed = False
    else:
        print("  PASS")
    print()

    # Low-redundancy corpus: unique sentences
    clean = [
        f"Document {i}: {' '.join(f'word{j}{i}' for j in range(30))}"
        for i in range(50)
    ]
    clean_result = corpus_entropy(clean)
    print(f"Clean text ({len(clean)} docs):")
    print(f"  Overall redundancy:   {clean_result['overall_redundancy']:.1%}")
    print(f"  Predicted impact:     {clean_result['predicted_impact']}")
    if clean_result["predicted_impact"] not in ("none", "low"):
        print("  FAIL: expected none or low impact")
        passed = False
    else:
        print("  PASS")
    print()

    # Mixed corpus: half noisy, half clean
    mixed = generate_corpus(25) + clean[:25]
    mixed_result = corpus_entropy(mixed)
    print(f"Mixed corpus ({len(mixed)} docs):")
    print(f"  Overall redundancy:   {mixed_result['overall_redundancy']:.1%}")
    print(f"  Predicted impact:     {mixed_result['predicted_impact']}")
    print(f"  PASS (informational — no threshold for mixed)")
    print()

    if passed:
        print("ALL PASS")
    else:
        print("SOME FAILED")
    return passed


if __name__ == "__main__":
    success = run()
    sys.exit(0 if success else 1)
