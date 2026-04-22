"""CLI entry point for logdigest."""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    import click
except ImportError:
    print(
        "Error: click is required for the logdigest CLI.\n"
        "Install with: pip install logdigest[discover]",
        file=sys.stderr,
    )
    raise SystemExit(1)


@click.group()
def cli():
    """logdigest - log pre-digestion toolkit."""


@cli.command()
@click.argument("corpus_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--domain", "-d", required=True, help="Domain name for output.")
@click.option("--output", "-o", default=None, type=click.Path(), help="Output JSON path (default: stdout).")
@click.option("--reference", type=click.Path(exists=True), help="Existing pattern JSON for conflict resolution.")
@click.option("--min-frequency", default=0.01, type=float, help="Min corpus frequency for noise candidates.")
@click.option("--depth", default=4, type=int, help="Drain3 tree depth.")
@click.option("--similarity", default=0.4, type=float, help="Drain3 similarity threshold.")
@click.option("--split", default=0.2, type=float, help="Held-out validation fraction.")
def discover(corpus_dir, domain, output, reference, min_frequency, depth, similarity, split):
    """Discover noise/signal patterns from a corpus of log files."""
    try:
        from logdigest.discover.mining import mine_templates
    except ImportError:
        click.echo(
            "Error: drain3 is required for discovery.\n"
            "Install with: pip install logdigest[discover]",
            err=True,
        )
        raise SystemExit(1)

    from logdigest.discover.classifier import (
        CandidatePattern,
        classify_template,
        generate_label,
        resolve_conflicts,
    )
    from logdigest.discover.export import export_pattern_set, write_pattern_set
    from logdigest.discover.regex_convert import template_to_regex
    from logdigest.discover.validation import validate_patterns
    from logdigest.domain_base import PatternSet
    from logdigest.entropy import corpus_entropy

    # 1. Load corpus
    corpus_path = Path(corpus_dir)
    files = sorted(f for f in corpus_path.rglob("*") if f.is_file()
                   and f.suffix not in (".gz", ".tar", ".zip", ".bin", ".png", ".jpg"))
    if not files:
        click.echo(f"No files found in {corpus_dir}", err=True)
        raise SystemExit(1)

    documents = []
    for f in files:
        try:
            documents.append(f.read_text(errors="replace"))
        except OSError:
            pass

    click.echo(f"Corpus: {len(documents)} files")

    # 2. Entropy pre-flight
    metrics = corpus_entropy(documents)
    click.echo(f"Redundancy: {metrics['overall_redundancy']:.1%}")
    click.echo(f"Predicted impact: {metrics['predicted_impact']}")
    if metrics["predicted_impact"] in ("none", "low"):
        click.echo("Warning: low redundancy — pattern discovery may not be effective.")

    # 3. Train/test split
    split_idx = max(1, int(len(documents) * (1 - split)))
    train_docs = documents[:split_idx]
    test_docs = documents[split_idx:]

    train_lines = []
    for doc in train_docs:
        train_lines.extend(doc.splitlines())
    test_lines = []
    for doc in test_docs:
        test_lines.extend(doc.splitlines())

    click.echo(f"Training: {len(train_lines)} lines, Test: {len(test_lines)} lines")

    # 4. Mine templates
    clusters = mine_templates(train_lines, depth=depth, similarity=similarity)
    click.echo(f"Templates discovered: {len(clusters)}")

    # 5. Convert and classify
    candidates: list[CandidatePattern] = []
    used_labels: set[str] = set()

    for cluster in clusters:
        regex = template_to_regex(cluster.template)
        if regex is None:
            continue
        candidate = classify_template(
            cluster, regex, len(train_lines), min_frequency=min_frequency,
        )
        if candidate.classification == "uncategorized":
            continue
        label = generate_label(cluster.template, used_labels)
        candidate.label = label
        used_labels.add(label)
        candidates.append(candidate)

    noise = [c for c in candidates if c.classification == "noise"]
    signal = [c for c in candidates if c.classification == "signal"]
    click.echo(f"Candidates: {len(noise)} noise, {len(signal)} signal")

    # 6. Conflict resolution
    conflicts: list[dict] = []
    if reference:
        ref_data = json.loads(Path(reference).read_text(encoding="utf-8"))
        ref_noise = PatternSet._compile_patterns(ref_data.get("noise_patterns", []))
        ref_signal = PatternSet._compile_patterns(ref_data.get("signal_patterns", []))
        noise, new_conflicts = resolve_conflicts(noise, ref_noise, ref_signal)
        conflicts.extend(new_conflicts)
        if new_conflicts:
            click.echo(f"Conflicts resolved: {len(new_conflicts)} blocked")

    # 7. Validate
    if test_lines:
        noise = validate_patterns(noise + signal, test_lines)
        signal = [c for c in noise if c.classification == "signal"]
        noise = [c for c in noise if c.classification == "noise"]
        click.echo(f"After validation: {len(noise)} noise, {len(signal)} signal")

    # 8. Export
    data = export_pattern_set(domain, noise, signal, len(documents), conflicts or None)

    # Schema-validate before writing
    try:
        PatternSet._validate_schema(data)
    except Exception as e:
        click.echo(f"Warning: output failed schema validation: {e}", err=True)

    PatternSet._compile_patterns(data["noise_patterns"])
    PatternSet._compile_patterns(data["signal_patterns"])

    if output:
        write_pattern_set(data, Path(output))
        click.echo(f"Patterns written to {output}")
    else:
        click.echo(json.dumps(data, indent=2))
