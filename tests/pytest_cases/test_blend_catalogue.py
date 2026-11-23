# SPDX-License-Identifier: Apache-2.0
"""Metadata menu safety, shared revision cache and stale async publication."""

import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from cycles_houdini.blender import blend_catalogue as catalogue
from cycles_houdini.blender import blend_worker
from cycles_houdini.core import blender_bridge

pytestmark = [pytest.mark.pure, pytest.mark.cpu]
_REFERENCE_ROWS = catalogue.reference_rows


class Parm:
    def __init__(self, value=""):
        self.value = value

    def evalAsString(self):
        return self.value

    def eval(self):
        return self.value

    def unexpandedString(self):
        return self.value

    def set(self, value):
        self.value = value


class Node:
    def __init__(self, identity, source):
        self.identity, self.source = identity, str(source)
        self.parms = {name: Parm() for name in ("saved_catalogue", "catalogue_status")}

    def sessionId(self):
        return self.identity

    def parm(self, name):
        return self.parms[name]

    def evalParm(self, name):
        return {"file": self.source, "target_kind": "object"}[name]


@pytest.fixture
def host(monkeypatch, tmp_path):
    main = threading.get_ident()
    nodes, callbacks = {}, []

    def on_main(value):
        assert threading.get_ident() == main, "HOM was called from a helper thread"
        return value

    hou = SimpleNamespace(
        text=SimpleNamespace(expandString=lambda value: on_main(value)),
        nodeBySessionId=lambda key: on_main(nodes.get(key)),
        hipFile=SimpleNamespace(
            eventCallbacks=lambda: on_main(callbacks), addEventCallback=lambda callback: callbacks.append(callback)
        ),
        isUIAvailable=lambda: False,
    )
    monkeypatch.setitem(sys.modules, "hou", hou)
    monkeypatch.setattr(catalogue, "initialize_rows", lambda node: None)
    monkeypatch.setattr(catalogue, "reference_rows", lambda node: [dict(file=node.source)])
    executable = tmp_path / "synthetic-blender.exe"
    executable.touch()

    def discover(*args):
        assert threading.get_ident() != main, "Blender discovery ran on the host thread"
        return str(executable)

    monkeypatch.setattr(blender_bridge, "_discover", discover)
    monkeypatch.setattr(blender_bridge, "enabled", lambda: True)
    monkeypatch.setattr(blender_bridge, "_configured_executable", lambda: "")
    catalogue.invalidate()
    catalogue._cache.clear()
    yield nodes
    catalogue.invalidate()
    deadline = time.monotonic() + 5
    while catalogue._active and not catalogue._active["done"].is_set() and time.monotonic() < deadline:
        time.sleep(0.005)
    catalogue.drain_catalogues()


def pump(jobs):
    deadline = time.monotonic() + 5
    while not all(job["done"].is_set() for job in jobs):
        catalogue.drain_catalogues()
        assert time.monotonic() < deadline
        time.sleep(0.005)
    catalogue.drain_catalogues()


def test_menu_only_reads_saved_json_even_with_missing_source(host, monkeypatch, tmp_path):
    node = Node(1, tmp_path / "missing 片.blend")
    record = dict(
        source=node.source, signature=[1, 2, 3, 4], objects=["Object ü", 'Quoted "name"'], collections=["Collection"]
    )
    node.parm("saved_catalogue").set(json.dumps(dict(schema_version=1, files={catalogue._path(node.source): record})))
    monkeypatch.setattr(blend_worker, "_signature", lambda _: pytest.fail("Menu touched filesystem"))
    monkeypatch.setattr(blender_bridge, "status", lambda: pytest.fail("Menu discovered Blender"))
    assert catalogue.cached_menu(node) == record["objects"]


def test_shared_file_requests_are_fair_and_query_revision_once(host, monkeypatch, tmp_path):
    source = tmp_path / "source.blend"
    source.write_bytes(b"one")
    entered, release = threading.Event(), threading.Event()
    calls = []

    def query(sources, executable, **kwargs):
        calls.append(sources)
        entered.set()
        assert release.wait(5)
        return [
            dict(source=source, signature=blend_worker._signature(source), objects=["Cube"], collections=[])
            for source in sources
        ]

    monkeypatch.setattr(blend_worker, "catalogue_many", query)
    for i in range(3):
        host[i] = Node(i, source)
    first = catalogue.request_catalogues(host[0])
    assert entered.wait(5)
    remaining = [catalogue.request_catalogues(host[i], force=True) for i in (1, 2)]
    release.set()
    pump([first, *remaining])
    assert len(calls) == 1
    assert all(catalogue.cached_menu(node) == ["Cube"] for node in host.values())
    assert all(node.parm("catalogue_status").value == "" for node in host.values())
    source.write_bytes(b"revision two")
    pump([catalogue.request_catalogues(host[0])])
    assert len(calls) == 2


def test_replaced_file_generation_never_publishes_old_catalogue(host, monkeypatch, tmp_path):
    a, b = tmp_path / "a.blend", tmp_path / "b.blend"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    entered, release = threading.Event(), threading.Event()

    def query(sources, executable, **kwargs):
        if sources == [os.path.normcase(str(a.resolve()))]:
            entered.set()
            assert release.wait(5)
        return [
            dict(source=source, signature=blend_worker._signature(source), objects=[Path(source).stem], collections=[])
            for source in sources
        ]

    monkeypatch.setattr(blend_worker, "catalogue_many", query)
    host[1] = node = Node(1, a)
    old = catalogue.request_catalogues(node)
    assert entered.wait(5)
    node.source = str(b)
    latest = catalogue.request_catalogues(node)
    release.set()
    pump([old, latest])
    assert old["cancelled"].is_set()
    assert catalogue.cached_menu(node) == ["b"]
    assert set(catalogue._read(node)) == {catalogue._path(str(b))}


@pytest.mark.parametrize("files", [[], ["bad"], {"bad": "value"}, {"bad": {}}, {"bad": {"objects": []}}])
def test_corrupt_saved_catalogue_is_empty(host, files, tmp_path):
    node = Node(1, tmp_path / "source.blend")
    node.parm("saved_catalogue").set(json.dumps(dict(schema_version=1, files=files)))
    assert catalogue.cached_menu(node) == []


def test_catalogue_worker_rejects_more_than_128_files_without_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(
        blend_worker, "_Worker", lambda *args, **kwargs: pytest.fail("oversized batch launched Blender")
    )
    with pytest.raises(ValueError, match="bounds"):
        blend_worker.catalogue_many([tmp_path / f"{i}.blend" for i in range(129)], "unused")


def test_relative_catalogue_paths_resolve_against_hip(host, monkeypatch, tmp_path):
    hip = tmp_path / "project"
    monkeypatch.setattr(sys.modules["hou"].text, "expandString", lambda value: str(hip) if value == "$HIP" else value)
    assert catalogue._path("assets/relative.blend") == os.path.normcase(str(hip / "assets/relative.blend"))


def test_deleted_node_cancels_active_catalogue_and_allows_other_consumer(host, monkeypatch, tmp_path):
    source = tmp_path / "source.blend"
    source.write_bytes(b"one")
    entered = threading.Event()
    calls = []

    def query(sources, executable, cancelled=None):
        calls.append(sources)
        if len(calls) == 1:
            entered.set()
            assert cancelled.wait(5)
            raise RuntimeError("cancelled")
        return [
            dict(source=path, signature=blend_worker._signature(path), objects=["Cube"], collections=[])
            for path in sources
        ]

    monkeypatch.setattr(blend_worker, "catalogue_many", query)
    host[1] = node = Node(1, source)
    job = catalogue.request_catalogues(node)
    assert entered.wait(5)
    host[2] = other = Node(2, source)
    surviving = catalogue.request_catalogues(other)
    host.pop(1)
    catalogue.drain_catalogues()
    pump([job, surviving])
    assert job["cancelled"].is_set()
    assert catalogue._active is None
    assert catalogue.cached_menu(other) == ["Cube"]


def test_catalogue_cache_and_each_node_have_aggregate_byte_budgets(host, monkeypatch, tmp_path):
    # Each file is valid individually; their union cannot exceed one HIP payload.
    record = dict(
        source=str(tmp_path / "one.blend"),
        signature=[1, 2, 3, 4],
        objects=["x" * 80 + str(i) for i in range(7000)],
        collections=[],
    )
    source = os.path.normcase(record["source"])
    assert catalogue._valid_record(record, source)
    job = dict(records={}, error=None)
    assert catalogue._stage_record(job, source, record)
    second = dict(record, source=str(tmp_path / "two.blend"))
    assert not catalogue._stage_record(job, os.path.normcase(second["source"]), second)
    assert "aggregate" in job["error"]
    with catalogue._lock:
        for i in range(20):
            value = dict(record, source=str(tmp_path / f"{i}.blend"))
            catalogue._cache_record(os.path.normcase(value["source"]), value)
    assert sum(catalogue._cache_sizes[key] for key in catalogue._cache) <= catalogue._MAX_CACHE_BYTES


def test_unchanged_refresh_does_not_write_saved_parameters(host, monkeypatch, tmp_path):
    source = tmp_path / "source.blend"
    source.write_bytes(b"one")

    def query(sources, executable, **kwargs):
        return [
            dict(source=path, signature=blend_worker._signature(path), objects=["Cube"], collections=[])
            for path in sources
        ]

    monkeypatch.setattr(blend_worker, "catalogue_many", query)
    host[1] = node = Node(1, source)
    pump([catalogue.request_catalogues(node)])
    writes = []
    for name, parm in node.parms.items():
        original = parm.set

        def set_value(value, name=name, original=original):
            writes.append(name)
            original(value)

        monkeypatch.setattr(parm, "set", set_value)
    pump([catalogue.request_catalogues(node, force=True)])
    assert writes == []


def test_unified_refresh_publishes_new_names_before_requesting_enabled_bounds(host, monkeypatch, tmp_path):
    from cycles_houdini.blender import blend_bounds

    source = tmp_path / "source.blend"
    source.write_bytes(b"one")
    host[1] = node = Node(1, source)
    bounds_calls = []
    sentinel = object()

    def query(sources, executable, **kwargs):
        return [
            dict(source=path, signature=blend_worker._signature(path), objects=["Rock Large"], collections=[])
            for path in sources
        ]

    def refresh(current):
        assert threading.current_thread() is threading.main_thread()
        assert current is node
        assert catalogue.cached_menu(current) == ["Rock Large"]
        bounds_calls.append(current)
        return sentinel

    monkeypatch.setattr(blend_worker, "catalogue_many", query)
    monkeypatch.setattr(catalogue, "resolved_reference_rows", lambda current: ([dict(target="Rock Large")], []))
    monkeypatch.setattr(blend_bounds, "refresh_node_bounds", refresh)
    job = catalogue.refresh_node(node)
    pump([job])
    assert bounds_calls == [node]
    assert job["bounds_job"] is sentinel
    assert node.parm("catalogue_status").value == ""


def test_unified_refresh_does_not_evaluate_bounds_after_catalogue_failure(host, monkeypatch, tmp_path):
    from cycles_houdini.blender import blend_bounds

    source = tmp_path / "source.blend"
    source.write_bytes(b"one")
    host[1] = node = Node(1, source)

    def query(*args, **kwargs):
        raise ValueError("synthetic metadata failure")

    monkeypatch.setattr(blend_worker, "catalogue_many", query)
    monkeypatch.setattr(
        blend_bounds, "refresh_node_bounds", lambda current: pytest.fail("failed catalogue evaluated bounds")
    )
    job = catalogue.refresh_node(node)
    pump([job])
    assert "synthetic metadata failure" in job["error"]
    assert "synthetic metadata failure" in node.parm("catalogue_status").value


def test_disabled_rows_never_evaluate_their_target_file_or_frame(host):
    class DisabledNode:
        values = dict(
            file="shared.blend",
            multiple_references=1,
            references=3,
            ref_enabled1=1,
            ref_enabled2=0,
            ref_file1="",
            ref_target_kind1="object",
            ref_target1="Cube",
            ref_frame1=1.25,
            ref_name1="",
            ref_hull_per_member1=0,
            # An object row never reads Include Disabled Collections.
            ref_enabled3=1,
            ref_file3="",
            ref_target_kind3="collection",
            ref_target3="Rig",
            ref_frame3=2.0,
            ref_name3="",
            ref_hull_per_member3=0,
            ref_include_disabled_collections3=1,
        )

        def parm(self, name):
            assert name in self.values, "Disabled row parameter evaluated: " + name
            return Parm(self.values[name])

        def evalParm(self, name):
            assert name in self.values, "Disabled row parameter evaluated: " + name
            return self.values[name]

    rows = _REFERENCE_ROWS(DisabledNode())
    assert rows == [
        dict(
            file="shared.blend",
            kind="object",
            target="Cube",
            frame=1.25,
            name="",
            hull_per_member=False,
            include_disabled_collections=False,
        ),
        dict(
            file="shared.blend",
            kind="collection",
            target="Rig",
            frame=2.0,
            name="",
            hull_per_member=False,
            include_disabled_collections=True,
        ),
    ]


def test_saved_alias_binding_is_scoped_by_file_and_survives_catalogue_collision(host, monkeypatch, tmp_path):
    node = Node(1, tmp_path / "source.blend")
    node.parms["saved_target_bindings"] = Parm()
    row = dict(file=node.source, kind="object", target="Wine_Bottle", frame=1.25, name="")
    monkeypatch.setattr(catalogue, "reference_rows", lambda current: [row])

    def save(names):
        record = dict(source=node.source, signature=[1, 2, 3, 4], objects=names, collections=[])
        node.parm("saved_catalogue").set(
            json.dumps(dict(schema_version=1, files={catalogue._path(node.source): record}))
        )

    save(["Wine Bottle"])
    catalogue.bind_targets(node)
    mapping = catalogue._bindings(node)
    key = catalogue._binding_key(node.source, "object")
    assert mapping[key] == {"Wine_Bottle": "Wine Bottle"}
    encoded = node.parm("saved_target_bindings").unexpandedString()
    save(["Wine Bottle", "Wine_Bottle"])
    catalogue.bind_targets(node)
    assert node.parm("saved_target_bindings").unexpandedString() == encoded
    assert catalogue._binding_key(str(tmp_path / "other.blend"), "object") not in mapping
    assert catalogue._binding_key(node.source, "collection") not in mapping


def test_cached_selection_edit_refreshes_new_bounds_without_reading_library(host, monkeypatch, tmp_path):
    from cycles_houdini.blender import blend_bounds

    class SelectionNode(Node):
        def evalParm(self, name):
            return 1 if name == "multiple_references" else super().evalParm(name)

    node = SelectionNode(1, tmp_path / "source.blend")
    node.parms["saved_reference_migration"] = Parm()
    node.parms["saved_target_bindings"] = Parm()
    record = dict(source=node.source, signature=[1, 2, 3, 4], objects=["Rock Large", "Rock Small"], collections=[])
    node.parm("saved_catalogue").set(json.dumps(dict(schema_version=1, files={catalogue._path(node.source): record})))
    row = dict(file=node.source, kind="object", target="Rock_Large", frame=1.25, name="")
    monkeypatch.setattr(catalogue, "reference_rows", lambda current, time=None: [row])
    monkeypatch.setattr(
        catalogue, "request_catalogues", lambda *args, **kwargs: pytest.fail("cached edit reread library")
    )
    monkeypatch.setattr(blend_worker, "_signature", lambda *args: pytest.fail("cached edit probed filesystem"))
    refreshed = []
    monkeypatch.setattr(
        blend_bounds,
        "refresh_node_bounds",
        lambda current: refreshed.append([item["target"] for item in catalogue.resolved_reference_rows(current)[0]]),
    )
    catalogue.selection_changed(node)
    row["target"] = "Rock*"
    catalogue.selection_changed(node)
    assert refreshed == [["Rock Large"], ["Rock Large", "Rock Small"]]


def test_edited_legacy_target_retires_override_but_retains_migration_audit(host, tmp_path):
    class LegacyNode(Node):
        def evalParm(self, name):
            return 1 if name == "references" else self.parm(name).evalAsString()

    node = LegacyNode(1, tmp_path / "source.blend")
    node.parms["ref_enabled1"] = Parm(1)
    node.parms["ref_name1"] = Parm("authored_piece")
    node.parms["ref_target1"] = Parm("Original with spaces")
    original = catalogue._channel_fingerprint(node.parm("ref_target1"))
    node.parms["saved_reference_migration"] = Parm(
        json.dumps(
            dict(
                schema_version=1,
                legacy_names=["authored_piece"],
                legacy_target_fingerprints={"authored_piece": original},
            )
        )
    )
    catalogue._edited_legacy_rows(node)
    assert node.parm("ref_name1").value == "authored_piece"
    node.parm("ref_target1").set("New*")
    catalogue._edited_legacy_rows(node)
    assert node.parm("ref_name1").value == ""
    audit = json.loads(node.parm("saved_reference_migration").value)
    assert audit["edited_legacy_names"] == ["authored_piece"]
    assert audit["legacy_target_fingerprints"]["authored_piece"] == original


def test_disabled_legacy_migration_edit_never_reads_name_or_target(host, tmp_path):
    class DisabledLegacyNode(Node):
        def evalParm(self, name):
            if name == "references":
                return 1
            if name == "ref_enabled1":
                return 0
            pytest.fail("Disabled legacy channel evaluated: " + name)

        def parm(self, name):
            assert name == "saved_reference_migration", "Disabled legacy channel read: " + name
            return self.parms[name]

    node = DisabledLegacyNode(1, tmp_path / "source.blend")
    metadata = json.dumps(dict(schema_version=1, legacy_target_fingerprints={"piece": "old"}))
    node.parms["saved_reference_migration"] = Parm(metadata)
    catalogue._edited_legacy_rows(node)
    assert node.parms["saved_reference_migration"].value == metadata


def test_active_legacy_override_does_not_discover_unused_missing_shared_file(host, monkeypatch, tmp_path):
    node = Node(1, tmp_path / "missing-shared.blend")
    override = tmp_path / "valid-override.blend"
    override.write_bytes(b"valid override")
    monkeypatch.setattr(catalogue, "reference_rows", lambda current: [dict(file=str(override))])
    assert catalogue._catalogue_sources(node) == (catalogue._path(str(override)),)
    monkeypatch.setattr(catalogue, "reference_rows", lambda current: [])
    assert catalogue._catalogue_sources(node) == (catalogue._path(node.source),)
