"""Drain3 template mining wrapper."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass
class TemplateCluster:
    """A group of log lines sharing a common template."""

    template: str
    size: int
    frequency: float
    cluster_id: int
    sample_lines: list[str] = field(default_factory=list)


def mine_templates(
    lines: Iterable[str],
    depth: int = 4,
    similarity: float = 0.4,
    max_samples: int = 10,
) -> list[TemplateCluster]:
    """Run Drain3 over lines and return template clusters.

    Lines are consumed incrementally — pass a generator or file iterator
    to avoid loading the full corpus into memory.

    Args:
        lines: Raw log lines to mine (any iterable).
        depth: Drain3 tree depth (higher = more specific templates).
        similarity: Drain3 similarity threshold (lower = more aggressive grouping).
        max_samples: Maximum sample lines to keep per cluster.

    Returns:
        List of TemplateCluster sorted by size descending.
    """
    from drain3 import TemplateMiner
    from drain3.template_miner_config import TemplateMinerConfig

    config = TemplateMinerConfig()
    config.drain_depth = depth
    config.drain_sim_th = similarity
    config.drain_max_children = 100
    config.profiling_enabled = False

    miner = TemplateMiner(config=config)

    total = 0
    samples: dict[int, list[str]] = {}
    for line in lines:
        stripped = line.strip()
        result = miner.add_log_message(stripped)
        total += 1
        cid = result["cluster_id"]
        if cid not in samples:
            samples[cid] = []
        if len(samples[cid]) < max_samples:
            samples[cid].append(stripped)

    total = total or 1
    clusters = []
    for cluster in miner.drain.clusters:
        clusters.append(TemplateCluster(
            template=cluster.get_template(),
            size=cluster.size,
            frequency=cluster.size / total,
            cluster_id=cluster.cluster_id,
            sample_lines=samples.get(cluster.cluster_id, []),
        ))

    clusters.sort(key=lambda c: c.size, reverse=True)
    return clusters
