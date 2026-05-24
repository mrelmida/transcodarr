"""
Tests for Fix B: meta loader hardening.

B.1 — load_unified_meta() now logs at ERROR for permission / FileNotFound /
JSONDecodeError instead of silently returning {} at DEBUG.

B.2 — find_meta_json() and find_unified_meta() refuse to guess when there are
multiple unmatched candidates in the folder.
"""
import json
import logging
import os
import stat

import pytest

from transcodarr_core.meta import (
    find_meta_json,
    find_unified_meta,
    load_unified_meta,
)


def test_load_unified_meta_logs_on_malformed_json(tmp_path, caplog):
    # Sidecar exists but is invalid JSON — must surface at ERROR
    (tmp_path / "X.meta.json").write_text("{ not valid json", encoding="utf-8")
    with caplog.at_level(logging.ERROR, logger="root"):
        out = load_unified_meta(str(tmp_path / "X.mkv"))
    assert out == {}
    assert any("malformed JSON" in r.message for r in caplog.records), (
        "Expected loud ERROR log for malformed JSON, got: "
        + repr([r.message for r in caplog.records])
    )


def test_load_unified_meta_logs_on_permission_error(tmp_path, caplog):
    # Skip on platforms where chmod doesn't enforce read perms (e.g. running as root)
    if os.geteuid() == 0:
        pytest.skip("permission test requires non-root user")
    meta = tmp_path / "X.meta.json"
    meta.write_text(json.dumps({"kind": "movie"}), encoding="utf-8")
    os.chmod(meta, 0)  # unreadable
    try:
        with caplog.at_level(logging.ERROR, logger="root"):
            out = load_unified_meta(str(tmp_path / "X.mkv"))
        assert out == {}
        assert any("permission denied" in r.message.lower() for r in caplog.records)
    finally:
        os.chmod(meta, stat.S_IRUSR | stat.S_IWUSR)


def test_find_meta_json_refuses_to_guess_among_multiple_movies(tmp_path):
    """No episode code + multiple candidates ⇒ refuse to return any."""
    (tmp_path / "Movie A.meta.json").write_text("{}", encoding="utf-8")
    (tmp_path / "Movie B.meta.json").write_text("{}", encoding="utf-8")
    # Video filename has no SxxExx pattern
    assert find_meta_json(str(tmp_path / "Some Movie.mp4")) is None
    assert find_unified_meta(str(tmp_path / "Some Movie.mp4")) is None


def test_find_meta_json_single_candidate_still_returned(tmp_path):
    """The single-candidate fast path keeps working — only multi-candidate refusal changed."""
    sidecar = tmp_path / "The Movie.meta.json"
    sidecar.write_text("{}", encoding="utf-8")
    result = find_meta_json(str(tmp_path / "different name.mp4"))
    assert result == sidecar


def test_find_meta_json_episode_code_match_still_works(tmp_path):
    """Episode-code matching unchanged: S02E05 video pairs with S02E05 meta."""
    (tmp_path / "Show - S02E05 - title.meta.json").write_text("{}", encoding="utf-8")
    (tmp_path / "Show - S02E06 - other.meta.json").write_text("{}", encoding="utf-8")
    result = find_meta_json(str(tmp_path / "Show - S02E05 - title.mkv"))
    assert result is not None
    assert "S02E05" in result.name
