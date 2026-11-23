# SPDX-License-Identifier: Apache-2.0
"""Worker and listing asset providers plus the background index job."""

import json
import os
from pathlib import Path
import shutil
import threading

import pytest

from cycles_houdini.assets import indexer, providers
from cycles_houdini.assets.cache import IndexCache
from cycles_houdini.assets.libraries import Library

pytestmark = [pytest.mark.pure, pytest.mark.cpu]

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/asset_browser/listing_v1"
METAL = "d7a2f3c1-0d5e-4c4b-9a8e-1f2e3d4c5b6a"


class FakeWorker:
    """Stand-in for blend_worker.assets_many; writes a preview per material."""

    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def __call__(self, sources, executable, thumbnail_dir, timeout=120, cancelled=None):
        assert len(sources) <= providers.BATCH_SIZE and hasattr(cancelled, "is_set")
        self.calls.append(list(sources))
        replies = []
        for source in sources:
            stem = Path(source).stem
            if stem in self.fail:
                replies.append({"source": source, "error": "cannot read " + stem})
                continue
            preview = stem + ".png"
            Path(thumbnail_dir, preview).write_bytes(b"png")
            replies.append(
                {
                    "source": source,
                    "signature": [0],
                    "assets": [
                        {
                            "name": "Mat " + stem,
                            "id_type": "MATERIAL",
                            "meta": {"catalog_id": METAL, "tags": ["t"]},
                            "catalog_simple_name": "Materials-Metal",
                            "object_type": None,
                            "preview": preview,
                            "preview_size": [4, 4],
                        },
                        {
                            "name": "Obj " + stem,
                            "id_type": "OBJECT",
                            "meta": {},
                            "object_type": "MESH",
                            "preview": None,
                        },
                        {"name": "", "id_type": "OBJECT"},
                    ],
                }
            )
        return replies


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "lib"
    for rel in ("a.blend", "sub/b.blend", "sub/b.blend1", ".hidden/c.blend", "_v1/d.blend", "deep/_v1/e.blend"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"BLENDER")
    return root


def worker_provider(root, tmp_path, worker, **options):
    executable = tmp_path / "blender"
    executable.write_text("")
    return providers.BlenderWorkerProvider(
        Library("L", str(root)),
        IndexCache(str(root), root=tmp_path / "cache"),
        assets_many=worker,
        executable=executable,
        **options,
    )


def test_find_blend_files(library):
    found = [os.path.relpath(p, library).replace("\\", "/") for p in providers.find_blend_files(library)]
    assert found == ["a.blend", "deep/_v1/e.blend", "sub/b.blend"]


def test_worker_scan_reuses_cache_and_prunes(library, tmp_path):
    worker = FakeWorker(fail={"e"})
    provider = worker_provider(library, tmp_path, worker)
    steps = []
    result = provider.scan(progress=lambda done, total: steps.append((done, total)))
    assert (result.scanned, result.reused, result.blender_missing) == (3, 0, False)
    assert steps[0] == (0, 3) and steps[-1] == (3, 3)
    assert sorted((e["file"], e["message"]) for e in result.errors) == [
        ("a.blend", "malformed asset entry"),
        ("deep/_v1/e.blend", "cannot read e"),
        ("sub/b.blend", "malformed asset entry"),
    ]
    names = sorted(r["name"] for r in result.records)
    assert names == ["Mat a", "Mat b", "Obj a", "Obj b"]
    material = next(r for r in result.records if r["name"] == "Mat b")
    assert material["files"] == ["sub/b.blend"] and material["ch"]["source"] == str(library / "sub/b.blend").replace(
        "\\", "/"
    )  # forward slashes, also on Windows
    assert material["ch"]["key"] == "sub/b.blend|MATERIAL|Mat b"
    assert material["meta"]["catalog_id"] == METAL and material["ch"]["catalog_simple_name"] == "Materials-Metal"
    assert Path(material["thumbnail"]["url"]).read_bytes() == b"png" and len(material["thumbnail"]["hash"]) == 64
    assert provider.items() == result.records

    again = provider.scan()
    assert (again.scanned, again.reused) == (0, 3) and len(worker.calls) == 1
    assert [e["message"] for e in again.errors] == ["cannot read e"]

    os.utime(library / "a.blend", ns=(1, 1))
    (library / "sub/b.blend").unlink()
    third = provider.scan()
    assert (third.scanned, third.reused) == (1, 1)
    assert worker.calls[-1] == [str(library / "a.blend")]
    assert sorted(r["name"] for r in third.records) == ["Mat a", "Obj a"]
    assert not (provider.cache.thumbnails_dir / "b.png").exists()
    assert provider.scan(force=True).scanned == 2


def test_worker_scan_batches_and_cancellation(tmp_path):
    root = tmp_path / "many"
    root.mkdir()
    for index in range(70):
        (root / f"f{index:02}.blend").write_bytes(b"x")
    stop = threading.Event()
    worker = FakeWorker()

    def cancelling(*args, **kwargs):
        stop.set()
        return worker(*args, **kwargs)

    provider = worker_provider(root, tmp_path, cancelling)
    result = provider.scan(cancelled=stop)
    assert result.cancelled and [len(call) for call in worker.calls] == [32]
    assert len(result.records) == 64
    result = provider.scan(cancelled=lambda: False)
    assert [len(call) for call in worker.calls] == [32, 32, 6] and result.reused == 32


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_linked_duplicate_is_read_once(library, tmp_path):
    os.symlink(library / "a.blend", library / "z.blend")
    worker = FakeWorker(fail={"e"})
    result = worker_provider(library, tmp_path, worker).scan()
    assert str(library / "z.blend") not in worker.calls[0]
    assert any(e["file"] == "z.blend" and "Same file" in e["message"] for e in result.errors)


def test_worker_crash_is_reported_not_cached(library, tmp_path):
    def broken(*args, **kwargs):
        raise RuntimeError("worker died")

    provider = worker_provider(library, tmp_path, broken)
    result = provider.scan()
    assert len(result.errors) == 3 and all(e["message"] == "worker died" for e in result.errors)
    assert provider.cache.files() == []


@pytest.mark.parametrize("previous_success", [False, True])
def test_transient_per_file_failure_repairs_on_normal_scan(tmp_path, previous_success):
    root = tmp_path / "lib"
    root.mkdir()
    source = root / "a.blend"
    source.write_bytes(b"BLENDER")
    now = [100.0]
    worker = FakeWorker()
    provider = worker_provider(root, tmp_path, worker, clock=lambda: now[0])
    previous = provider.scan().records if previous_success else []
    if previous_success:
        source.write_bytes(b"BLENDER changed")
    worker.fail.add("a")  # An old/unknown worker error is transient by default.
    failed = provider.scan()
    assert failed.records == previous and len(failed.errors) == 1
    entry = provider.cache.entry(source)
    assert entry["error_kind"] == "transient" and entry["retry_after"] == 105.0
    calls = len(worker.calls)
    for _ in range(20):
        assert provider.scan().reused == 1
    assert len(worker.calls) == calls
    # Retry state survives provider/cache reconstruction; no forced refresh.
    provider = providers.BlenderWorkerProvider(
        str(root),
        IndexCache(str(root), root=tmp_path / "cache"),
        assets_many=worker,
        executable=tmp_path / "blender",
        clock=lambda: now[0],
    )
    now[0] = 105.0
    worker.fail.clear()
    repaired = provider.scan()
    assert repaired.scanned == 1 and not provider.cache.entry(source)["error"]
    assert sorted(r["name"] for r in repaired.records) == ["Mat a", "Obj a"]


def test_transient_backoff_is_bounded_and_signature_change_retries(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    source = root / "a.blend"
    source.write_bytes(b"BLENDER")
    now = [0.0]
    worker = FakeWorker(fail={"a"})
    provider = worker_provider(root, tmp_path, worker, clock=lambda: now[0])
    delays = []
    for _ in range(12):
        assert provider.scan().scanned == 1
        retry = provider.cache.entry(source)["retry_after"]
        delays.append(retry - now[0])
        now[0] = retry
    assert delays[:4] == [5, 10, 20, 40] and delays[-1] == 300
    provider.scan()
    worker.fail.clear()
    source.write_bytes(b"changed")
    assert provider.scan().scanned == 1  # New content bypasses its old backoff.


def test_invalid_file_negative_cache_and_legacy_error_recovery(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    source = root / "a.blend"
    source.write_bytes(b"junk")
    calls = []

    def invalid(sources, *args, **kwargs):
        calls.append(sources)
        return [{"error": "Not a Blender file", "error_kind": "invalid_file"} for _ in sources]

    provider = worker_provider(root, tmp_path, invalid, clock=lambda: 1000000)
    assert provider.scan().scanned == 1
    for _ in range(20):
        assert provider.scan().reused == 1
    assert len(calls) == 1 and provider.items() == []
    # The old format cached every error as empty success: upgrade must repair it.
    provider.cache.put(source, providers.file_signature(source), [], "old temporary read error")
    provider._assets_many = FakeWorker()
    assert provider.scan().scanned == 1 and len(provider.items()) == 2


def test_healthy_scan_classifies_without_copying_records(library, tmp_path, monkeypatch):
    provider = worker_provider(library, tmp_path, FakeWorker())
    provider.scan()
    copies = []
    overlay = provider.cache._overlay

    def record_copy(entry):
        copies.append(entry["file"])
        return overlay(entry)

    monkeypatch.setattr(provider.cache, "_overlay", record_copy)
    monkeypatch.setattr(provider.cache, "get", lambda *a: pytest.fail("Scan decision materialized records"))
    monkeypatch.setattr(provider.cache, "entry", lambda *a: pytest.fail("Scan decision deep-copied records"))
    result = provider.scan()
    assert result.reused == 3 and len(result.records) == 6
    assert len(copies) == 3  # Exactly the final outward ScanResult; no per-file classification copy.


@pytest.mark.parametrize(
    "retry_after, attempts",
    [
        ("never", "forever"),
        ([], {}),
        (float("inf"), float("inf")),
        (float("nan"), -10),
        (10**1000, 10**1000),
        (1e30, 1e30),
    ],
)
def test_malformed_or_absurd_persisted_retry_state_cannot_wedge_scan(tmp_path, retry_after, attempts):
    root = tmp_path / "lib"
    root.mkdir()
    source = root / "a.blend"
    source.write_bytes(b"BLENDER")
    worker = FakeWorker(fail={"a"})
    provider = worker_provider(root, tmp_path, worker, clock=lambda: 100)
    provider.scan()
    entry = provider.cache.entry(source)
    entry.update(retry_after=retry_after, failure_attempts=attempts, error_kind="unknown-old-worker")
    provider.cache._entry_path("a.blend").write_text(json.dumps(entry), encoding="utf-8")
    provider.cache = IndexCache(str(root), root=tmp_path / "cache")
    # Exercise sanitization both while reading eligibility and writing the next failed attempt.
    assert provider.scan().scanned == 1
    worker.fail.clear()
    provider._clock = lambda: 1000
    result = provider.scan()
    assert result.scanned == 1 and len(result.records) == 2


def test_blender_missing_keeps_cache(library, tmp_path, monkeypatch):
    provider = worker_provider(library, tmp_path, FakeWorker())
    provider.scan()
    os.utime(library / "a.blend", ns=(5, 5))
    missing = providers.BlenderWorkerProvider(
        str(library), provider.cache, assets_many=FakeWorker(), executable=tmp_path / "nope"
    )
    result = missing.scan()
    assert result.blender_missing and "Blender not found" in result.message
    assert len(result.records) == 6  # stale entries stay visible
    monkeypatch.setenv("CYCLES_HOUDINI_BLENDER_BRIDGE", "0")
    disabled = providers.BlenderWorkerProvider(str(library), provider.cache, assets_many=FakeWorker())
    result = disabled.scan()
    assert result.blender_missing and "disabled" in result.message


def test_missing_library_folder_keeps_cache(library, tmp_path):
    provider = worker_provider(library, tmp_path, FakeWorker())
    provider.scan()
    shutil.rmtree(library)
    result = provider.scan()
    assert "not found" in result.message and len(result.records) == 6


def test_listing_reader_on_blender_generated_fixture(tmp_path):
    root = tmp_path / "lib with space"
    shutil.copytree(FIXTURE, root)
    listing = providers.read_listing(root)
    assert listing.warnings == [] and listing.complete and listing.schema_version == "1.0.0"
    records = {record["name"]: (record, signature) for record, signature in listing.assets}
    material, signature = records["Metal - Galvanized"]
    assert material["id_type"] == "MATERIAL" and material["files"] == ["materials/metal.blend"]
    assert material["ch"]["source"] == str(root / "materials/metal.blend").replace("\\", "/")  # also on Windows
    assert material["ch"]["provider"] == "listing" and material["ch"]["catalog_simple_name"] == "Materials-Metal"
    assert material["meta"]["tags"] == ["metal", "shiny"] and material["meta"]["license"] == "CC0"
    assert material["thumbnail"]["url"].endswith("metal-galvanized.webp")
    assert Path(material["thumbnail"]["url"]).is_file() and len(material["thumbnail"]["hash"]) == 64
    assert signature[0] == "listing" and signature[1].startswith("SHA256:")
    prop, _ = records["Tri Prop"]
    assert prop["thumbnail"] is None and prop["meta"]["catalog_id"] == "a1b2c3d4-e5f6-4a5b-8c7d-9e0f1a2b3c4d"


def test_listing_provider_scan_items_catalogs(tmp_path):
    root = tmp_path / "lib"
    shutil.copytree(FIXTURE, root)
    provider = providers.make_provider(
        Library("L", str(root), provider="listing"),
        cache=IndexCache(str(root), root=tmp_path / "c", namespace="listing"),
    )
    assert isinstance(provider, providers.BlenderListingProvider)
    assert len(provider.items()) == 2  # direct read before the first scan
    result = provider.scan()
    assert (result.scanned, len(result.records)) == (1, 2)
    assert provider.scan().reused == 1
    tree = provider.catalogs()
    assert tree.node_for_uuid(METAL).path == "Materials/Metal"
    index = root / "_v1/asset-index.json"
    data = json.loads(index.read_text())
    del data["catalogs"]
    index.write_text(json.dumps(data))
    assert provider.catalogs().node_for_uuid(METAL).path == "Materials/Metal"  # cats.txt fallback


def test_listing_rejects_paths_outside_library(tmp_path):
    root = tmp_path / "lib"
    (root / "_v1").mkdir(parents=True)
    (tmp_path / "secret.png").write_bytes(b"x")
    (root / providers.META_FILENAME).write_text(json.dumps({"api_versions": {"v1": {"url": "_v1/asset-index.json"}}}))
    pages = [{"url": "_v1/p.json"}, {"url": "../outside.json"}, {"url": "https://example.org/p.json"}, "junk"]
    (root / "_v1/asset-index.json").write_text(json.dumps({"schema_version": "1.0.0", "pages": pages}))
    assets = [
        {"name": "ok", "id_type": "MATERIAL", "files": ["a.blend"], "thumbnail": {"url": "../secret.png", "hash": "x"}},
        {"name": "escape", "id_type": "MATERIAL", "files": ["../x.blend"]},
        {"name": "abs", "id_type": "MATERIAL", "files": ["/etc/x.blend"]},
        {"name": "notype", "files": ["a.blend"]},
        {"name": "ok", "id_type": "MATERIAL", "files": ["a.blend"]},
        42,
    ]
    (root / "_v1/p.json").write_text(json.dumps({"assets": assets, "files": "bad"}))
    listing = providers.read_listing(root)
    assert [record["name"] for record, _ in listing.assets] == ["ok"]
    assert listing.assets[0][0]["thumbnail"] is None
    assert not listing.complete and len(listing.warnings) == 7
    with pytest.raises(ValueError):
        providers.read_listing(tmp_path)


def test_listing_scan_without_listing(tmp_path):
    provider = providers.BlenderListingProvider(str(tmp_path), IndexCache(str(tmp_path), root=tmp_path / "c"))
    result = provider.scan()
    assert "No Blender asset listing" in result.message and result.records == []
    assert provider.items() == []


def test_generate_listing_command(tmp_path, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        (tmp_path / providers.META_FILENAME).write_text("{}")
        return type("Completed", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr(providers.subprocess, "run", run)
    provider = providers.BlenderListingProvider(str(tmp_path), IndexCache(str(tmp_path), root=tmp_path / "c"))
    provider.generate_listing("/bin/blender")
    assert calls == [["/bin/blender", "-b", "--factory-startup", "-c", "asset_listing", "generate", str(tmp_path)]]
    monkeypatch.setattr(
        providers.subprocess, "run", lambda c, **k: type("C", (), {"returncode": 1, "stderr": "boom", "stdout": ""})()
    )
    with pytest.raises(RuntimeError, match="boom"):
        provider.generate_listing("/bin/blender")


def test_make_provider_kinds(tmp_path):
    assert providers.make_provider(str(tmp_path), "worker").kind == "worker"
    with pytest.raises(ValueError):
        providers.make_provider(str(tmp_path), "other")


def test_index_job(library, tmp_path):
    job = indexer.IndexJob(worker_provider(library, tmp_path, FakeWorker()))
    assert job.poll() is None
    job.start()
    state = job.join(30)
    assert state["state"] == "done" and state["progress"] == (3, 3) and len(state["result"].records) == 6

    gate, release = threading.Event(), threading.Event()

    class Slow:
        def scan(self, *, cancelled, progress):
            gate.set()
            release.wait(10)
            assert cancelled()
            raise RuntimeError("stopped")

    job = indexer.IndexJob(Slow())
    job.start()
    gate.wait(10)
    assert job.poll()["state"] == "running"
    job.cancel()
    release.set()
    assert job.join(10)["state"] == "cancelled"

    class Broken:
        def scan(self, **kwargs):
            raise ValueError("bad")

    job = indexer.IndexJob(Broken())
    job.start()
    assert job.join(10) == {"state": "failed", "progress": (0, 0), "result": None, "error": "bad"}
