"""The suite never touches the developer's own Omnigent config or live runner."""

from __future__ import annotations

import os
from collections.abc import Generator
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


_FIXTURE_URI = "sqlite:////nonexistent/fixture-boundary-probe.db"
_KEPT_URI = "sqlite:////nonexistent/fixture-boundary-kept.db"
_REMOTE_URI = "postgresql://nonexistent/fixture-boundary-remote"
_probe_engine = _FakeEngine()


@pytest.fixture(scope="module")
def _seeded_caches() -> Generator[dict[str, _FakeEngine], None, None]:
    """Entries present before any test of this module runs, so the per-test snapshot keeps them."""
    import importlib

    from omnigent.db import utils as db_utils
    from tests.conftest import _SQLITE_STORE_MODULES

    kept, remote = _FakeEngine(), _FakeEngine()
    dicts = [importlib.import_module(name)._stores for name in _SQLITE_STORE_MODULES]
    db_utils._engine_cache[_KEPT_URI] = kept  # type: ignore[assignment]
    db_utils._engine_cache[_REMOTE_URI] = remote  # type: ignore[assignment]
    for stores in dicts:
        stores[_KEPT_URI] = "kept"  # type: ignore[assignment]
        stores[_REMOTE_URI] = "remote"  # type: ignore[assignment]
    yield {"kept": kept, "remote": remote}
    db_utils._engine_cache.pop(_KEPT_URI, None)
    db_utils._engine_cache.pop(_REMOTE_URI, None)
    for stores in dicts:
        stores.pop(_KEPT_URI, None)
        stores.pop(_REMOTE_URI, None)


def test_a_test_that_leaves_sqlite_caches_populated(
    _seeded_caches: dict[str, _FakeEngine],
) -> None:
    """Leaves a new engine and store entries behind; the next test sees the fixture's cleanup."""
    import importlib

    from omnigent.db import utils as db_utils
    from tests.conftest import _SQLITE_STORE_MODULES

    db_utils._engine_cache[_FIXTURE_URI] = _probe_engine  # type: ignore[assignment]
    for name in _SQLITE_STORE_MODULES:
        importlib.import_module(name)._stores[_FIXTURE_URI] = "leaked"  # type: ignore[assignment]


def test_the_autouse_fixture_cleaned_up_after_the_previous_test(
    _seeded_caches: dict[str, _FakeEngine],
) -> None:
    """Drives the autouse fixture across a test boundary; runs right after the test above."""
    import importlib

    from omnigent.db import utils as db_utils
    from tests.conftest import _SQLITE_STORE_MODULES

    assert _FIXTURE_URI not in db_utils._engine_cache
    assert _probe_engine.disposed
    for name in _SQLITE_STORE_MODULES:
        stores = importlib.import_module(name)._stores
        assert _FIXTURE_URI not in stores, name
        assert stores[_KEPT_URI] == "kept", name
        assert stores[_REMOTE_URI] == "remote", name
    assert db_utils._engine_cache[_KEPT_URI] is _seeded_caches["kept"]
    assert db_utils._engine_cache[_REMOTE_URI] is _seeded_caches["remote"]
    assert not _seeded_caches["kept"].disposed
    assert not _seeded_caches["remote"].disposed
