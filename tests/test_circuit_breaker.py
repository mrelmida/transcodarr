"""
Tests for Fix C: re-transcode loop circuit breaker.

The full integration scenario (walk a fake watch tree, observe ignore-add
on the second pass) requires a real Postgres + real worker pool, which the
existing test harness explicitly mocks out (see conftest.py). These tests
cover the smaller, in-process pieces:

  - get_transcode_history_by_source is exported from transcodarr_core
  - The migration adds idx_transcode_source for both fresh and existing DBs
  - The kill-switch setting key is checked correctly

The live-verification step in the deployment plan covers the end-to-end
behavior that needs a real DB.
"""


def test_get_transcode_history_by_source_is_exported():
    """The circuit breaker depends on this helper being importable from the
    top-level core package — guards against accidental __init__.py removal."""
    from transcodarr_core import get_transcode_history_by_source
    assert callable(get_transcode_history_by_source)


def test_circuit_breaker_kill_switch_key():
    """The walk loop reads DEDUP_BY_TRANSCODE_HISTORY; document the key
    here so a rename in pipeline.py also requires updating this test."""
    expected_key = "DEDUP_BY_TRANSCODE_HISTORY"
    # If this string changes in pipeline.py, the kill switch documented in
    # the rollback plan stops working — fail loudly and force the operator
    # to update both places.
    import inspect
    from transcodarr_core import pipeline
    src = inspect.getsource(pipeline)
    assert expected_key in src, (
        f"{expected_key!r} not found in pipeline.py — kill switch is broken"
    )


def test_migration_creates_source_index_idempotently():
    """The migration uses CREATE INDEX IF NOT EXISTS; check the literal SQL
    so an accidental edit that drops the IF NOT EXISTS guard is caught."""
    import inspect
    from transcodarr_core import database
    src = inspect.getsource(database)
    assert "CREATE INDEX IF NOT EXISTS idx_transcode_source" in src, (
        "idx_transcode_source migration is missing or no longer idempotent"
    )
