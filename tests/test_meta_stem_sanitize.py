"""
Tests for Fix A: meta filename stem sanitization.

The Sonarr webhook used to paste episode titles verbatim into the meta filename
stem. Titles with reserved characters like '/' (e.g. Barry S02E05 "ronny/lily")
caused pathlib to interpret them as path separators, the write failed, and the
.meta.json never landed on disk — kicking off a re-transcode loop.
"""
import pytest

from web.shared_state import _safe_stem, write_meta_json


@pytest.mark.parametrize("dirty,expected", [
    ("S02E05 - ronny/lily",       "S02E05 - ronny+lily"),
    ("S01E01 - foo:bar",          "S01E01 - foo+bar"),
    ("S01E01 - foo\\bar",         "S01E01 - foo+bar"),
    ('S01E01 - foo<>?*|"bar',     "S01E01 - foo++++++bar"),
    ("Plain Title",               "Plain Title"),
    ("Title with  spaces",        "Title with spaces"),
    ("Trailing dots.",            "Trailing dots"),
    ("",                          "untitled"),
])
def test_safe_stem_replaces_reserved(dirty, expected):
    assert _safe_stem(dirty) == expected


def test_safe_stem_is_idempotent():
    once = _safe_stem("S02E05 - ronny/lily")
    twice = _safe_stem(once)
    assert once == twice == "S02E05 - ronny+lily"


def test_safe_stem_bounds_length():
    long_title = "A" * 500
    out = _safe_stem(long_title)
    assert len(out) <= 200


def test_safe_stem_strips_control_chars():
    assert _safe_stem("foo\x00\x01bar") == "foo++bar"


def test_write_meta_json_with_slash_in_title(tmp_path):
    """Reproduces the Barry S02E05 bug: pre-fix this raised FileNotFoundError;
    post-fix the write succeeds and produces a file named with '+'."""
    out_file = write_meta_json(tmp_path, "S02E05 - ronny/lily", {"kind": "episode"})
    assert out_file.exists()
    assert out_file.name == "S02E05 - ronny+lily.meta.json"
    # And critically: no rogue subdirectory was created.
    assert not (tmp_path / "S02E05 - ronny").exists()


def test_write_meta_json_with_clean_stem_unchanged(tmp_path):
    """A stem with no reserved characters should round-trip exactly."""
    out_file = write_meta_json(tmp_path, "Plain Movie (2020)", {"kind": "movie"})
    assert out_file.exists()
    assert out_file.name == "Plain Movie (2020).meta.json"
