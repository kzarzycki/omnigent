"""The suite never touches the developer's own Omnigent config or live runner."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omnigent.config import global_config_path


def test_user_config_path_is_inside_the_test_data_dir() -> None:
    data_dir = Path(os.environ["OMNIGENT_DATA_DIR"])
    assert global_config_path().is_relative_to(data_dir)


def test_no_inherited_runner_identity() -> None:
    inherited = [
        name
        for name in os.environ
        if name.startswith(("OMNIGENT_RUNNER_", "OMNIGENT_TERMINAL_"))
        or name == "OMNIGENT_PROCESS_LOG_FILE"
    ]
    assert inherited == []


def test_harness_root_is_owned_by_the_test_suite() -> None:
    from tests import conftest

    assert Path(os.environ["OMNIGENT_HARNESS_TMP_PARENT"]) == conftest._TEST_HARNESS_TMP_PARENT


class _FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


def test_sqlite_cache_eviction_drops_new_entries_and_keeps_the_rest() -> None:
    """The per-test cleanup of cached engines and stores (#790) evicts what the test added only."""
    import threading

    from tests.conftest import _evict_sqlite_entries_added_since

    old, added, remote = _FakeEngine(), _FakeEngine(), _FakeEngine()
    engines = {"sqlite:///old.db": old}
    engines_before = frozenset(engines)
    stores = {"sqlite:///old.db": "old-store"}
    stores_before = frozenset(stores)
    engines.update({"sqlite:///new.db": added, "postgresql://host/db": remote})
    stores.update({"sqlite:///new.db": "new-store", "postgresql://host/db": "remote-store"})

    _evict_sqlite_entries_added_since(
        engines, threading.Lock(), engines_before, [stores], [stores_before]
    )

    assert set(engines) == {"sqlite:///old.db", "postgresql://host/db"}
    assert set(stores) == {"sqlite:///old.db", "postgresql://host/db"}
    assert added.disposed
    assert not old.disposed
    assert not remote.disposed


def test_sqlite_cache_cleanup_covers_all_four_per_uri_store_dicts() -> None:
    """The fixture evicts from each module that memoises a store per URI, none left out."""
    import importlib

    from tests.conftest import _SQLITE_STORE_MODULES

    assert set(_SQLITE_STORE_MODULES) == {
        "omnigent.stores.user_settings_store",
        "omnigent.server.workflows",
        "omnigent.server.session_journal",
        "omnigent.server.session_pull_requests",
    }
    for name in _SQLITE_STORE_MODULES:
        assert isinstance(importlib.import_module(name)._stores, dict)


def test_sqlite_cleanup_fixture_teardown_evicts_new_entries_from_the_real_caches() -> None:
    """Drive the autouse fixture's own generator across setup and teardown, on the real caches."""
    import importlib

    from omnigent.db import utils as db_utils
    from tests import conftest

    stores = [
        importlib.import_module(n)._stores
        for n in (
            "omnigent.stores.user_settings_store",
            "omnigent.server.workflows",
            "omnigent.server.session_journal",
            "omnigent.server.session_pull_requests",
        )
    ]
    old_uri, new_uri, remote_uri = (
        "sqlite:///isolation-test-old.db",
        "sqlite:///isolation-test-new.db",
        "postgresql://isolation-test/db",
    )
    old, added, remote = _FakeEngine(), _FakeEngine(), _FakeEngine()
    uris = (old_uri, new_uri, remote_uri)
    try:
        with db_utils._engine_lock:
            db_utils._engine_cache[old_uri] = old
        for d in stores:
            d[old_uri] = "old-store"

        generator = conftest._dispose_sqlite_engines_created_by_test.__wrapped__()
        next(generator)  # setup: snapshots the caches

        with db_utils._engine_lock:
            db_utils._engine_cache[new_uri] = added
            db_utils._engine_cache[remote_uri] = remote
        for d in stores:
            d[new_uri] = "new-store"
            d[remote_uri] = "remote-store"

        with pytest.raises(StopIteration):
            next(generator)  # teardown

        assert new_uri not in db_utils._engine_cache
        assert added.disposed
        assert db_utils._engine_cache[old_uri] is old
        assert db_utils._engine_cache[remote_uri] is remote
        assert not old.disposed
        assert not remote.disposed
        for d in stores:
            assert new_uri not in d
            assert d[old_uri] == "old-store"
            assert d[remote_uri] == "remote-store"
    finally:
        with db_utils._engine_lock:
            for uri in uris:
                db_utils._engine_cache.pop(uri, None)
        for d in stores:
            for uri in uris:
                d.pop(uri, None)
