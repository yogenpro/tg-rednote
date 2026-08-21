"""Telegraph: the page has to fit, and the way home has to survive the fitting."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from app.telegraph import CONTENT_LIMIT, encode, trim  # noqa: E402


def para(text: str) -> dict:
    return {"tag": "p", "children": [text]}


TAIL = [{"tag": "p", "children": [{"tag": "a", "attrs": {"href": "u"}, "children": ["home"]}]}]


def test_a_page_that_fits_is_left_alone():
    content = [para("short")]
    assert trim(content, tail=TAIL) == content + TAIL


def test_an_oversized_page_loses_whole_nodes_from_the_end():
    content = [para("x" * 900) for _ in range(200)]
    fitted = trim(content, tail=TAIL, note="cut")
    assert len(encode(fitted).encode()) <= CONTENT_LIMIT
    # Whole paragraphs, never a slice through one.
    assert all(node in content or node in fitted[-2:] for node in fitted)
    assert fitted[0] == content[0]


def test_the_link_home_survives_the_trim():
    content = [para("x" * 900) for _ in range(200)]
    fitted = trim(content, tail=TAIL, note="cut")
    assert fitted[-1] == TAIL[0]
    assert "cut" in encode(fitted)


def test_nothing_is_said_about_trimming_when_nothing_was_trimmed():
    assert "cut" not in encode(trim([para("short")], tail=TAIL, note="cut"))
