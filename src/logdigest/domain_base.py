"""Shared machinery for domain-specific pre-digestion modules.

Extracts the common patterns from domain modules (classify_line,
analyze_lines, find_large_blobs) so each new domain only needs to
define its NOISE_PATTERNS, SIGNAL_PATTERNS, and domain-specific
block/blob detectors.
"""

from __future__ import annotations

import importlib.resources
import json
import re
import warnings
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Protocol, TypedDict, runtime_checkable

# Type alias for pattern lists used across all domain modules
PatternList = list[tuple[re.Pattern, str]]


class LineAnalysisStats(TypedDict):
    """Stats returned by :func:`analyze_lines`."""

    total_lines: int
    noise_lines: int
    signal_lines: int
    unknown_lines: int
    noise_pct: float
    classifications: dict[str, int]


class StreamStats(TypedDict):
    """Stats returned by :func:`predigest_stream`."""

    total_lines: int
    noise_lines: int
    signal_lines: int
    kept_lines: int


@runtime_checkable
class DomainModule(Protocol):
    """Contract that every domain module must satisfy.

    Domain modules (ansible, mustgather, cicd, journald) all expose
    this common interface. Domain-specific extras (e.g. cicd's
    ``detect_ci_system``) are not part of the contract.
    """

    NOISE_PATTERNS: PatternList
    SIGNAL_PATTERNS: PatternList

    @staticmethod
    def classify_line(line: str) -> str: ...
    @staticmethod
    def predigest(text: str, **kwargs: object) -> str: ...
    @staticmethod
    def predigest_file(filepath: Path) -> str: ...
    @staticmethod
    def predigest_file_streaming(filepath: Path) -> str: ...
    @staticmethod
    def analyze_log(filepath: Path) -> dict: ...


@dataclass
class BlockConfig:
    """Configuration for a simple block detector."""

    name: str
    match_regex: re.Pattern
    min_count: int
    summary_template: str


def collapse_blocks(lines: list[str], detectors: list[BlockConfig]) -> list[str]:
    """Collapse consecutive matching lines using block detectors.

    For each detector, consecutive lines matching match_regex are
    replaced with summary_template.format(count=N) when N >= min_count.
    """
    if not detectors:
        return lines

    result: list[str] = []
    i = 0
    while i < len(lines):
        matched_detector = None
        for det in detectors:
            if det.match_regex.search(lines[i]):
                matched_detector = det
                break

        if matched_detector is None:
            result.append(lines[i])
            i += 1
            continue

        run_start = i
        while i < len(lines) and matched_detector.match_regex.search(lines[i]):
            i += 1
        count = i - run_start

        if count >= matched_detector.min_count:
            result.append(
                matched_detector.summary_template.format(count=count)
            )
        else:
            result.extend(lines[run_start:i])

    return result


class PatternSet:
    """Load and cache pattern sets from JSON files."""

    _cache: ClassVar[dict[tuple[str, str | None], PatternSet | None]] = {}
    _schema: ClassVar[dict | None] = None

    def __init__(
        self,
        domain: str,
        noise_patterns: PatternList,
        signal_patterns: PatternList,
        metadata: dict | None = None,
        config: dict | None = None,
    ):
        self.domain = domain
        self.noise_patterns = noise_patterns
        self.signal_patterns = signal_patterns
        self.metadata = metadata or {}
        self._config = config or {}

    @property
    def error_context(self) -> int:
        return self._config.get("error_context", 3)

    @property
    def max_line_len(self) -> int:
        return self._config.get("max_line_len", 500)

    @property
    def file_streaming_threshold_mb(self) -> int:
        return self._config.get("file_streaming_threshold_mb", 50)

    @property
    def blob_markers(self) -> list[str]:
        return self._config.get("blob_markers", [])

    @property
    def domain_keywords(self) -> list[str]:
        return self._config.get("domain_keywords", [])

    @property
    def block_detectors(self) -> list[BlockConfig]:
        raw = self._config.get("block_detectors", [])
        result = []
        for entry in raw:
            try:
                compiled = re.compile(entry["regex"])
            except re.error as e:
                raise ValueError(
                    f"Invalid regex in block detector '{entry['name']}': "
                    f"{entry['regex']!r} — {e}"
                ) from e
            result.append(BlockConfig(
                name=entry["name"],
                match_regex=compiled,
                min_count=entry["min_count"],
                summary_template=entry["summary_template"],
            ))
        return result

    @classmethod
    def load(cls, domain: str, version: str | None = None) -> PatternSet | None:
        """Load patterns for a domain from JSON.

        Args:
            domain: Domain name (e.g., "ansible", "mustgather").
            version: None for baseline, "latest" for most recent
                generated file, or explicit version string.

        Returns:
            PatternSet if found, None if file not found.

        Raises:
            FileNotFoundError: If an explicit version is requested
                but the file does not exist.
            ValueError: If the JSON is malformed or contains
                invalid regex patterns.
        """
        cache_key = (domain, version)
        if cache_key in cls._cache:
            return cls._cache[cache_key]

        result = cls._load_uncached(domain, version)
        cls._cache[cache_key] = result
        return result

    @classmethod
    def _load_uncached(cls, domain: str, version: str | None) -> PatternSet | None:
        patterns_pkg = importlib.resources.files("logdigest.patterns")

        if version is None:
            filename = f"{domain}.json"
        elif version == "latest":
            filename = cls._find_latest(domain, patterns_pkg)
            if filename is None:
                filename = f"{domain}.json"
        else:
            filename = f"{domain}@{version}.json"

        resource = patterns_pkg.joinpath(filename)
        try:
            raw = resource.read_text(encoding="utf-8")
        except (FileNotFoundError, TypeError) as exc:
            if version is not None and version != "latest":
                raise FileNotFoundError(
                    f"Pattern file not found: {filename}"
                ) from exc
            return None

        data = json.loads(raw)
        cls._validate_schema(data)

        noise = cls._compile_patterns(data.get("noise_patterns", []))
        signal = cls._compile_patterns(data.get("signal_patterns", []))

        config = data.get("config", {})
        metadata = {
            k: v for k, v in data.items()
            if k not in ("noise_patterns", "signal_patterns", "config")
        }

        return cls(domain, noise, signal, metadata, config)

    @classmethod
    def _find_latest(cls, domain: str, patterns_pkg) -> str | None:
        """Find the most recently versioned pattern file for a domain.

        Version strings in filenames (domain@VERSION.json) are sorted
        lexicographically. This avoids reading every file's contents.
        """
        prefix = f"{domain}@"
        candidates = []
        for item in patterns_pkg.iterdir():
            name = item.name if hasattr(item, "name") else str(item)
            if name.startswith(prefix) and name.endswith(".json"):
                candidates.append(name)
        if not candidates:
            return None
        candidates.sort()
        return candidates[-1]

    @classmethod
    def _validate_schema(cls, data: dict) -> None:
        """Validate against JSON Schema if jsonschema is available."""
        try:
            import jsonschema
        except ImportError:
            warnings.warn(
                "jsonschema not installed; skipping pattern file schema "
                "validation. Install with: pip install logdigest[dev]",
                stacklevel=3,
            )
            return

        if cls._schema is None:
            schema_pkg = importlib.resources.files("logdigest.schemas")
            schema_raw = schema_pkg.joinpath("pattern_set.schema.json").read_text(
                encoding="utf-8"
            )
            cls._schema = json.loads(schema_raw)
        jsonschema.validate(data, cls._schema)

    @classmethod
    def _compile_patterns(cls, pattern_defs: list[dict]) -> PatternList:
        """Compile regex strings from JSON into PatternList."""
        result: PatternList = []
        for entry in pattern_defs:
            regex_str = entry["regex"]
            label = entry["label"]
            try:
                compiled = re.compile(regex_str)
            except re.error as e:
                raise ValueError(
                    f"Invalid regex in pattern '{label}': {regex_str!r} — {e}"
                ) from e
            result.append((compiled, label))
        return result

    @classmethod
    def clear_cache(cls) -> None:
        """Clear the pattern set and schema caches (useful for testing)."""
        cls._cache.clear()
        cls._schema = None


def classify_line(
    line: str,
    noise_patterns: PatternList,
    signal_patterns: PatternList,
) -> str:
    """Classify a single log line as noise, signal, or unknown.

    Args:
        line: Raw log line.
        noise_patterns: List of (compiled_regex, label) for noise.
        signal_patterns: List of (compiled_regex, label) for signal.

    Returns:
        "noise:<label>", "signal:<label>", or "unknown".
    """
    stripped = line.strip()
    for pattern, label in noise_patterns:
        if pattern.search(stripped):
            return f"noise:{label}"
    for pattern, label in signal_patterns:
        if pattern.search(stripped):
            return f"signal:{label}"
    return "unknown"


def analyze_lines(
    lines: list[str],
    noise_patterns: PatternList,
    signal_patterns: PatternList,
) -> LineAnalysisStats:
    """Produce noise/signal/unknown statistics for a line list."""
    total = len(lines)
    classifications: Counter[str] = Counter()
    for line in lines:
        classifications[classify_line(line, noise_patterns, signal_patterns)] += 1

    noise_count = sum(v for k, v in classifications.items() if k.startswith("noise:"))
    signal_count = sum(v for k, v in classifications.items() if k.startswith("signal:"))

    return {
        "total_lines": total,
        "noise_lines": noise_count,
        "signal_lines": signal_count,
        "unknown_lines": classifications.get("unknown", 0),
        "noise_pct": round(noise_count / total * 100, 1) if total > 0 else 0,
        "classifications": dict(classifications),
    }


def analyze_file(
    filepath: Path,
    noise_patterns: PatternList,
    signal_patterns: PatternList,
) -> dict:
    """Analyze a single file and return statistics."""
    lines = filepath.read_text(errors="replace").splitlines()
    stats = analyze_lines(lines, noise_patterns, signal_patterns)
    stats["file"] = filepath.name
    total_chars = sum(len(line) for line in lines)
    stats["total_chars"] = total_chars
    return stats


def predigest_stream(
    source,
    noise_patterns: PatternList,
    signal_patterns: PatternList,
    sink,
    error_context: int = 3,
    max_line_len: int = 500,
    blob_handler=None,
) -> StreamStats:
    """Streaming pre-digestion for arbitrarily large files.

    Reads lines from source (file object or iterable), writes to sink
    (file object or list). Uses a bounded buffer for error context
    instead of loading the entire file.

    Args:
        source: Iterable of lines (e.g., open file handle).
        noise_patterns: Noise pattern list for classify_line.
        signal_patterns: Signal pattern list for classify_line.
        sink: Writable file object or list to append output lines.
        error_context: Lines of context to preserve around errors.
        max_line_len: Lines longer than this are checked for blobs.
        blob_handler: Optional callable(line) -> str|None. If it
            returns a string, that replaces the line. If None, long
            lines pass through.
    """
    write = sink.append if isinstance(sink, list) else lambda line: sink.write(line + "\n")

    buffer: list[tuple[str, str]] = []  # (line, classification)
    stats = {"total_lines": 0, "noise_lines": 0, "signal_lines": 0, "kept_lines": 0}

    def _flush_buffer(up_to: int, error_at: int | None = None) -> None:
        """Flush buffer entries, keeping context around errors."""
        for idx, (line, cls) in enumerate(buffer[:up_to]):
            keep = False
            if cls.startswith("signal:") or (error_at is not None and abs(idx - error_at) <= error_context) or not cls.startswith("noise:"):
                keep = True

            if keep:
                if len(line) > max_line_len and blob_handler:
                    replacement = blob_handler(line)
                    if replacement is not None:
                        write(replacement)
                        stats["kept_lines"] += 1
                        continue
                write(line)
                stats["kept_lines"] += 1
        del buffer[:up_to]

    window = error_context * 2 + 1

    for raw_line in source:
        line = raw_line.rstrip("\n").rstrip("\r")
        stats["total_lines"] += 1
        cls = classify_line(line, noise_patterns, signal_patterns)

        if cls.startswith("noise:"):
            stats["noise_lines"] += 1
        elif cls.startswith("signal:"):
            stats["signal_lines"] += 1

        buffer.append((line, cls))

        if cls.startswith("signal:"):
            _flush_buffer(len(buffer), error_at=len(buffer) - 1)
        elif len(buffer) > window * 2:
            _flush_buffer(len(buffer) - window)

    if buffer:
        _flush_buffer(len(buffer))

    return stats


def find_large_blobs(
    lines: list[str],
    min_chars: int = 500,
    markers: list[str] | None = None,
) -> list[dict]:
    """Find lines containing large embedded data.

    Args:
        lines: Log lines to scan.
        min_chars: Minimum line length to consider.
        markers: Substrings that identify known blob types.
            Each marker is checked with `in line`. If any marker
            matches, the blob is tagged with that marker as type.
            Lines exceeding min_chars without a marker match are
            tagged as "long_line".

    Returns:
        List of dicts with line index, length, and type.
    """
    if markers is None:
        markers = []

    blobs = []
    for i, line in enumerate(lines):
        if len(line) <= min_chars:
            continue
        blob_type = "long_line"
        for marker in markers:
            if marker in line:
                blob_type = marker
                break
        blobs.append({"line": i, "length": len(line), "type": blob_type})
    return blobs
