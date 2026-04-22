"""Click-based CLI for logdigest discover.

Requires the [discover] extra (click, drain3). Imported lazily by
__main__.py — never at package-level.
"""

from __future__ import annotations

import json
from pathlib import Path

import click


@click.group()
def cli():
    """logdigest - log pre-digestion toolkit."""


def _collect_corpus(corpus_dir: str, max_file_mb: float) -> list[Path]:
    """Collect text files from corpus directory, skipping oversized and binary files."""
    corpus_path = Path(corpus_dir)
    skip_suffixes = {".gz", ".tar", ".zip", ".bin", ".png", ".jpg"}
    max_bytes = int(max_file_mb * 1024 * 1024)
    valid = []
    skipped_large = 0
    skipped_read = 0

    for f in sorted(corpus_path.rglob("*")):
        if not f.is_file() or f.suffix in skip_suffixes:
            continue
        try:
            if f.stat().st_size > max_bytes:
                skipped_large += 1
                continue
            valid.append(f)
        except OSError:
            skipped_read += 1

    if skipped_large:
        click.echo(f"Skipped {skipped_large} files over {max_file_mb}MB")
    if skipped_read:
        click.echo(f"Skipped {skipped_read} unreadable files")
    return valid


def _iter_lines(files: list[Path]):
    """Yield lines from files, streaming one file at a time."""
    for f in files:
        try:
            with f.open(errors="replace") as fh:
                for raw in fh:
                    yield raw.rstrip("\n")
        except OSError:
            continue


def _read_lines(files: list[Path]) -> list[str]:
    """Read all lines from files into a list."""
    return list(_iter_lines(files))


@cli.command()
@click.argument("corpus_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--domain", "-d", required=True, help="Domain name for output.")
@click.option("--output", "-o", default=None, type=click.Path(), help="Output JSON path (default: stdout).")
@click.option("--reference", type=click.Path(exists=True), help="Existing pattern JSON for conflict resolution.")
@click.option("--min-frequency", default=0.01, type=float, help="Min corpus frequency for noise candidates.")
@click.option("--depth", default=4, type=int, help="Drain3 tree depth.")
@click.option("--similarity", default=0.4, type=float, help="Drain3 similarity threshold.")
@click.option("--split", default=0.2, type=float, help="Held-out validation fraction.")
@click.option("--min-recall", default=0.5, type=float, help="Min recall ratio for validation (default: 0.5).")
@click.option("--max-file-mb", default=50.0, type=float, help="Skip files larger than this (MB, default: 50).")
def discover(
    corpus_dir, domain, output, reference, min_frequency,
    depth, similarity, split, min_recall, max_file_mb,
):
    """Discover noise/signal patterns from a corpus of log files."""
    try:
        from logdigest.discover.mining import mine_templates
    except ImportError:
        click.echo(
            "Error: drain3 is required for discovery.\n"
            "Install with: pip install logdigest[discover]",
            err=True,
        )
        raise SystemExit(1) from None

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
    files = _collect_corpus(corpus_dir, max_file_mb)
    if not files:
        click.echo(f"No files found in {corpus_dir}", err=True)
        raise SystemExit(1)

    documents = []
    corpus_bytes = 0
    for f in files:
        try:
            text = f.read_text(errors="replace")
            corpus_bytes += len(text.encode("utf-8", errors="replace"))
            documents.append(text)
        except OSError:
            continue

    click.echo(f"Corpus: {len(documents)} files, {corpus_bytes / 1024 / 1024:.1f}MB")

    # 2. Entropy pre-flight
    metrics = corpus_entropy(documents)
    click.echo(f"Redundancy: {metrics['overall_redundancy']:.1%}")
    click.echo(f"Predicted impact: {metrics['predicted_impact']}")
    if metrics["predicted_impact"] in ("none", "low"):
        click.echo("Warning: low redundancy — pattern discovery may not be effective.")

    # 3. Train/test split (by file, then stream lines)
    split_idx = max(1, int(len(files) * (1 - split)))
    train_files = files[:split_idx]
    test_files = files[split_idx:]

    del documents

    # 4. Mine templates (streaming — lines never fully materialized)
    clusters = mine_templates(_iter_lines(train_files), depth=depth, similarity=similarity)
    train_line_count = sum(c.size for c in clusters)
    test_lines = _read_lines(test_files)

    click.echo(f"Training: {train_line_count} lines, Test: {len(test_lines)} lines")
    click.echo(f"Templates discovered: {len(clusters)}")

    # 5. Convert and classify
    candidates: list[CandidatePattern] = []
    used_labels: set[str] = set()

    for cluster in clusters:
        regex = template_to_regex(cluster.template)
        if regex is None:
            continue
        candidate = classify_template(
            cluster, regex, train_line_count, min_frequency=min_frequency,
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
        resolved, new_conflicts = resolve_conflicts(noise + signal, ref_noise, ref_signal)
        noise = [c for c in resolved if c.classification == "noise"]
        signal = [c for c in resolved if c.classification == "signal"]
        conflicts.extend(new_conflicts)
        if new_conflicts:
            blocked = sum(1 for c in new_conflicts if c["action"] == "blocked")
            promoted = sum(1 for c in new_conflicts if c["action"] == "promote")
            parts = []
            if blocked:
                parts.append(f"{blocked} blocked")
            if promoted:
                parts.append(f"{promoted} promotions flagged")
            click.echo(f"Conflicts: {', '.join(parts)}")

    # 7. Validate
    if test_lines:
        validated = validate_patterns(noise + signal, test_lines, min_recall_ratio=min_recall)
        noise = [c for c in validated if c.classification == "noise"]
        signal = [c for c in validated if c.classification == "signal"]
        click.echo(f"After validation: {len(noise)} noise, {len(signal)} signal")

    # 8. Export
    data = export_pattern_set(domain, noise, signal, len(train_files) + len(test_files), conflicts or None)

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
