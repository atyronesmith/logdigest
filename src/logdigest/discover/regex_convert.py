"""Convert Drain3 templates to anchored regex patterns."""

from __future__ import annotations

import re

_WILDCARD = "<*>"


def template_to_regex(
    template: str,
    min_invariant_len: int = 4,
) -> str | None:
    """Convert a Drain3 template to an anchored regex string.

    Returns None if the template is too generic to be useful.
    """
    stripped = template.strip()
    if not stripped or stripped == _WILDCARD:
        return None

    parts = stripped.split(_WILDCARD)
    invariant = "".join(parts)
    invariant_stripped = invariant.strip()

    if len(invariant_stripped) < min_invariant_len:
        return None

    if _WILDCARD not in stripped:
        return "^" + re.escape(stripped)

    # Single trailing wildcard — prefix match
    if stripped.endswith(_WILDCARD) and stripped.count(_WILDCARD) == 1:
        prefix = stripped[: stripped.index(_WILDCARD)]
        return "^" + re.escape(prefix)

    # Multi-wildcard
    escaped_parts = [re.escape(p) for p in parts]
    # Collapse empty parts from consecutive wildcards
    non_empty = [p for p in escaped_parts if p]
    if not non_empty:
        return None

    regex = "^" + ".*?".join(escaped_parts)
    # Strip trailing .*? if template ends with wildcard
    if regex.endswith(".*?"):
        regex = regex[:-3]

    try:
        re.compile(regex)
    except re.error:
        return None

    return regex
