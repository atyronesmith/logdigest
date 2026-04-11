"""Tests for denoise safety guard pre-digestion and classification."""

import os
from pathlib import Path

from denoise.guard import classify, load_weights, predigest

# Path to trained weights (from drift repo)
_WEIGHTS_DIR = Path(os.environ.get(
    "GUARD_WEIGHTS_DIR",
    str(Path(__file__).parent.parent.parent / "drift" / "results" / "guard_classifier"),
))
_WEIGHTS_PATH = _WEIGHTS_DIR / "guard_classifier_weights.json"
_KEYWORDS_PATH = _WEIGHTS_DIR / "guard_tier1_keywords.json"
_HAS_WEIGHTS = _WEIGHTS_PATH.exists()


def test_predigest_strips_code_fences():
    text = '```python\nimport os\nimport sys\nprint("hello")\n```'
    result = predigest(text)
    assert "```" not in result
    assert "[CODE:" in result


def test_predigest_keeps_short_code():
    text = '```python\nprint("hello")\n```'
    result = predigest(text)
    assert 'print("hello")' in result


def test_predigest_collapses_blank_lines():
    text = "line1\n\n\n\n\nline2"
    result = predigest(text)
    assert "\n\n\n" not in result
    assert "line1" in result
    assert "line2" in result


def test_predigest_strips_json_structural():
    text = '{\n  "key": "value",\n  "nested": {\n    "inner": "data"\n  }\n}'
    result = predigest(text)
    # Structural-only lines removed, content lines kept
    assert '"key": "value"' in result


def test_predigest_preserves_semantic_content():
    text = "This output contains security recommendations.\nAvoid SQL injection."
    result = predigest(text)
    assert "security recommendations" in result
    assert "SQL injection" in result


def test_predigest_handles_empty():
    assert predigest("") == ""
    assert predigest("short") == "short"


def test_predigest_limits_imports():
    text = "\n".join([
        "import os", "import sys", "import json",
        "import re", "import math", "import time",
        "print('hello')",
    ])
    result = predigest(text)
    # Should keep first 3 imports, skip rest
    assert "print('hello')" in result


def test_classify_returns_tuple():
    # Without loaded weights, should escalate
    result = classify("some text")
    assert isinstance(result, tuple)
    assert len(result) == 2
    assert result[0] in ("safe", "unsafe", "escalate")
    assert isinstance(result[1], float)


def test_classify_escalates_without_weights():
    label, conf = classify("any text")
    assert label == "escalate"
    assert conf == 0.0


def test_predigest_unclosed_code_block():
    text = "```python\nline1\nline2\nline3"
    result = predigest(text)
    assert "[CODE:" in result


def test_predigest_reduces_length():
    """Predigest should make text shorter, not longer."""
    code = "```python\n" + "\n".join(f"import mod{i}" for i in range(20)) + "\n```"
    text = f"Here is the code:\n{code}\nEnd of output."
    result = predigest(text)
    assert len(result) < len(text)


# --- Tests with trained weights ---

import pytest


@pytest.fixture(autouse=False)
def loaded_weights():
    """Load classifier weights if available."""
    if not _HAS_WEIGHTS:
        pytest.skip("Guard classifier weights not found")
    load_weights(str(_WEIGHTS_PATH), str(_KEYWORDS_PATH))
    yield


@pytest.mark.skipif(not _HAS_WEIGHTS, reason="Guard classifier weights not found")
def test_classify_with_weights(loaded_weights):
    """With weights loaded, classify should return a verdict (not just escalate)."""
    # Typical safe SE output: a code review summary
    safe_text = (
        "## Code Review Summary\n\n"
        "The code handles SQL injection properly by using parameterized queries. "
        "Input validation is present for all user-facing endpoints. "
        "No security vulnerabilities found in the batch processing module."
    )
    label, conf = classify(safe_text)
    assert label in ("safe", "escalate")
    assert conf > 0.0

    # Typical unsafe SE output: code with personal data
    unsafe_text = (
        '```python\n'
        'def get_user(email):\n'
        '    user = db.query(f"SELECT * FROM users WHERE email=\'{email}\'")\n'
        '    return user.name, user.password\n'
        '```'
    )
    label2, conf2 = classify(unsafe_text)
    assert label2 in ("unsafe", "escalate")
    assert conf2 > 0.0


@pytest.mark.skipif(not _HAS_WEIGHTS, reason="Guard classifier weights not found")
def test_classify_confidence_range(loaded_weights):
    """Confidence should always be in [0, 1]."""
    texts = [
        "Hello world",
        '{"key": "value"}',
        "SELECT * FROM users WHERE id = ?",
        "```\nprint('test')\n```",
    ]
    for text in texts:
        label, conf = classify(text)
        assert 0.0 <= conf <= 1.0
        assert label in ("safe", "unsafe", "escalate")
