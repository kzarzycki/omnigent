from omnigent.db.utils import WriteInvalidatedCache


def test_serves_cached_value_until_a_write_invalidates_it():
    cache = WriteInvalidatedCache(ttl_s=60)
    loads = []

    def load(value):
        loads.append(value)
        return value

    assert cache.get_or_load(("u",), lambda: load("g1")) == "g1"
    assert cache.get_or_load(("u",), lambda: load("g2")) == "g1"
    cache.invalidate()
    assert cache.get_or_load(("u",), lambda: load("g2")) == "g2"
    assert loads == ["g1", "g2"]


def test_read_overlapping_a_write_is_not_cached():
    cache = WriteInvalidatedCache(ttl_s=60)

    def stale_load():
        cache.invalidate()  # a write commits while this read is in flight
        return "old"

    assert cache.get_or_load(("u",), stale_load) == "old"
    assert cache.get_or_load(("u",), lambda: "new") == "new"


def test_expires_after_ttl():
    cache = WriteInvalidatedCache(ttl_s=0)
    cache.get_or_load(("u",), lambda: "a")
    assert cache.get_or_load(("u",), lambda: "b") == "b"
