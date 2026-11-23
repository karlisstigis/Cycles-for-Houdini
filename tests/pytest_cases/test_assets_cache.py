# SPDX-License-Identifier: Apache-2.0
"""Houdini-side asset index cache: signatures, importability, pruning."""

import pytest

from cycles_houdini.assets import cache as cache_module
from cycles_houdini.assets import records
from cycles_houdini.assets.cache import IndexCache
from cycles_houdini.assets.libraries import Library

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def _record(library, rel, name, id_type="MATERIAL", thumbnail=None):
    return records.make_record(name, id_type, rel, source=str(library / rel), thumbnail=thumbnail)


@pytest.fixture
def setup(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    return library, IndexCache(Library("L", str(library)), root=tmp_path / "cache")


def test_cache_root(monkeypatch, tmp_path):
    monkeypatch.setenv("CYCLES_HOUDINI_ASSET_CACHE", str(tmp_path / "c"))
    assert cache_module.cache_root() == tmp_path / "c"
    monkeypatch.delenv("CYCLES_HOUDINI_ASSET_CACHE")
    monkeypatch.setenv("HOUDINI_USER_PREF_DIR", str(tmp_path / "p"))
    assert cache_module.cache_root() == tmp_path / "p/cycles_houdini/asset_cache"


def test_signature_round_trips_through_json(setup, tmp_path):
    library, cache = setup
    blend = library / "sub/a.blend"
    signature = (1, 2, (3, 4), "x")
    cache.put(blend, signature, [_record(library, "sub/a.blend", "M")])
    fresh = IndexCache(str(library), root=tmp_path / "cache")
    assert [r["name"] for r in fresh.get(blend, signature)] == ["M"]
    assert fresh.get(str(blend), [1, 2, [3, 4], "x"]) is not None
    assert fresh.get("sub/a.blend", list(signature)) is not None  # relative lookup
    assert fresh.get(blend, (1, 2, (3, 5), "x")) is None
    assert fresh.get(blend, None) is None
    assert fresh.get(library / "other.blend", signature) is None
    assert fresh.files() == ["sub/a.blend"]


def test_same_library_path_shares_directory_different_libraries_do_not(tmp_path):
    a = IndexCache(str(tmp_path / "x"), root=tmp_path / "c")
    b = IndexCache(str(tmp_path / "x" / "."), root=tmp_path / "c")
    c = IndexCache(str(tmp_path / "y"), root=tmp_path / "c")
    assert a.directory == b.directory != c.directory


def test_errors_and_invalid_records(setup):
    library, cache = setup
    cache.put(library / "bad.blend", [1], [], "cannot open")
    cache.put(library / "mixed.blend", [2], [_record(library, "mixed.blend", "ok"), {"name": "broken"}])
    assert cache.get(library / "bad.blend", [1]) == []
    assert cache.errors() == {"bad.blend": "cannot open"}
    assert [r["name"] for r in cache.all_records()] == ["ok"]


def test_importability_survives_until_signature_changes(setup, tmp_path):
    library, cache = setup
    record = _record(library, "a.blend", "Steel")
    key = records.record_key(record)
    cache.put(library / "a.blend", [1], [record])
    cache.set_importability(key, "no", "uses an unsupported node")
    reopened = IndexCache(str(library), root=tmp_path / "cache")
    assert reopened.get(library / "a.blend", [1])[0]["ch"]["importable"] == {
        "state": "no",
        "reason": "uses an unsupported node",
    }
    # Same signature re-put (e.g. forced rescan) keeps the verdict.
    reopened.put(library / "a.blend", [1], [record])
    assert reopened.all_records()[0]["ch"]["importable"]["state"] == "no"
    reopened.put(library / "a.blend", [2], [record])
    assert reopened.all_records()[0]["ch"]["importable"]["state"] == "unknown"
    with pytest.raises(KeyError):
        reopened.set_importability("missing|MATERIAL|x", "yes", "")
    with pytest.raises(ValueError):
        reopened.set_importability(key, "maybe", "")


def test_prune_removes_entries_and_owned_thumbnails(setup):
    library, cache = setup
    thumbs = cache.thumbnails_dir
    shared, own = thumbs / "shared.png", thumbs / "own.png"
    shared.write_bytes(b"x")
    own.write_bytes(b"y")
    outside = library / "keep.png"
    outside.write_bytes(b"z")
    cache.put(library / "a.blend", [1], [_record(library, "a.blend", "A", thumbnail=str(shared))])
    cache.put(
        library / "b.blend",
        [1],
        [
            _record(library, "b.blend", "B", thumbnail=str(shared)),
            _record(library, "b.blend", "C", thumbnail=str(own)),
            _record(library, "b.blend", "D", thumbnail=str(outside)),
        ],
    )
    cache.prune([library / "a.blend"])
    assert cache.files() == ["a.blend"]
    assert shared.exists() and not own.exists() and outside.exists()
    cache.put(library / "a.blend", [2], [_record(library, "a.blend", "A")])
    assert not shared.exists()
    cache.clear()
    assert cache.all_records() == []


def test_records_helpers(tmp_path):
    record = records.make_record(
        "Thing",
        "object",
        "dir\\f.blend",
        source="/abs/f.blend",
        meta={"catalog_id": "00000000-0000-0000-0000-000000000000", "tags": ["a", 3], "author": None},
        object_type="curve",
    )
    assert records.validate_record(record)
    assert record["files"] == ["dir/f.blend"]
    assert record["meta"]["catalog_id"] == "" and record["meta"]["tags"] == ["a"]
    assert records.record_key(record) == "dir/f.blend|OBJECT|Thing"
    assert record["ch"]["object_type"] == "CURVE" and record["ch"]["importable"]["state"] == "unknown"
    world = records.make_record("W", "WORLD", "w.blend", source="/w.blend")
    assert world["ch"]["importable"]["state"] == "no"
    assert not records.validate_record(dict(record, ch=dict(record["ch"], provider="other")))
    assert not records.validate_record({"name": "x"})
