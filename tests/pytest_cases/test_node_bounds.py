# SPDX-License-Identifier: Apache-2.0
"""Filesystem-only saved bounds validation and bounded persistence contracts."""

import importlib
import json
import math
import sys
import threading
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "houdini/python3.13libs"))
nb = importlib.import_module("cycles_houdini.blender.node_bounds")
bounds = importlib.import_module("cycles_houdini.blender.blend_bounds")
pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def record(tmp_path):
    source = tmp_path / "asset.blend"
    linked = tmp_path / "linked.blend"
    source.write_bytes(b"source")
    linked.write_bytes(b"linked")

    def version(path):
        stat = path.stat()
        return [stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino]

    from cycles_houdini.blender.file_signature import native_signature

    return dict(
        native_signatures={str(source): native_signature(str(source)), str(linked): native_signature(str(linked))},
        schema_version=1,
        known=True,
        reference="$HIP/asset.blend#object=Cube",
        frame=1.25,
        source=str(source),
        selector_type="OBJECT",
        selector="Cube",
        bounds=[-2, -3, -4, 5, 6, 7],
        source_identity="a" * 64,
        dependency_versions={str(source): version(source), str(linked): version(linked)},
    )


def test_roundtrip_is_versioned_bounded_and_rejects_ambiguous_keys(tmp_path):
    rec = record(tmp_path)
    encoded = nb.encode_records([rec])
    assert nb.decode_records(encoded) == [rec]
    assert len(encoded.encode()) <= nb.MAX_NODE_BYTES
    assert nb.decode_records(json.dumps(dict(schema_version=2, records=[rec]))) == []
    assert nb.decode_records(json.dumps(dict(schema_version=True, records=[rec]))) == []
    assert nb.decode_records(json.dumps(dict(schema_version=1, records=[rec, rec]))) == []
    assert nb.decode_records("x" * (nb.MAX_NODE_BYTES + 1)) == []


@pytest.mark.parametrize(
    "change",
    [
        dict(frame=float("nan")),
        dict(frame=True),
        dict(selector="Other"),
        dict(selector_type="INVALID"),
        dict(bounds=[0] * 5),
        dict(bounds=[1, 0, 0, -1, 0, 0]),
        dict(bounds=[0, 0, 0, math.inf, 1, 1]),
        dict(source_identity="z" * 64),
        dict(dependency_versions={}),
    ],
)
def test_bad_metadata_never_becomes_preview(change, tmp_path):
    rec = dict(record(tmp_path), **change)
    assert not nb.valid_record(rec)
    assert nb.decode_records(json.dumps(dict(schema_version=1, records=[rec]))) == []


def test_missing_or_changed_linked_dependency_clears_validation(tmp_path):
    rec = record(tmp_path)
    assert nb.validate_record(rec, rec["source"])
    linked = tmp_path / "linked.blend"
    linked.write_bytes(b"changed")
    assert not nb.validate_record(rec, rec["source"])
    linked.unlink()
    assert not nb.validate_record(rec, rec["source"])


def test_hip_save_as_resolves_different_source_even_when_old_file_exists(tmp_path):
    rec = record(tmp_path)
    moved = tmp_path / "moved" / "asset.blend"
    moved.parent.mkdir()
    moved.write_bytes(b"source")
    assert not nb.validate_record(rec, str(moved))


def test_validation_is_plain_filesystem_only_and_safe_on_helper_thread(tmp_path):
    rec = record(tmp_path)
    result = []
    with patch.dict(sys.modules, {"hou": None, "_cycles_houdini": None}):
        thread = threading.Thread(target=lambda: result.append(nb.validate_record(rec, rec["source"])))
        thread.start()
        thread.join(3)
    assert result == [True]


def test_native_publication_rejects_offthread_completion():
    job = bounds.BoundsRefresh()
    job.done.set()
    result = []

    def run():
        try:
            job.publish()
        except Exception as exc:
            result.append(str(exc))

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(3)
    assert len(result) == 1 and "main thread" in result[0]


def test_successful_refresh_does_not_publish_or_convert_on_helper_thread(monkeypatch):
    import numpy as np

    package = importlib.import_module("cycles_houdini.blender")
    manifest = dict(
        source="asset.blend",
        selector_type="OBJECT",
        selector="Cube",
        frame=1.0,
        dependency_versions={"asset.blend": [1, 2, 3, 4]},
        materials=[],
        meshes=[dict(key="mesh", arrays=dict(positions="P"))],
        instances=[dict(mesh_key="mesh", transform=np.eye(4).reshape(-1).tolist())],
    )
    key = ("blender.exe", "asset.blend", "OBJECT", "Cube", 1.0, None, "working", True)
    calls = []

    def accept(*args):
        calls.append(("accept", threading.get_ident()))
        return {}

    fake_assets = SimpleNamespace(
        _resolve_spec=lambda spec: (key, {}),
        _hip_event=object(),
        accept_evaluation=accept,
        _trim_prototypes=lambda: None,
    )
    fake_worker = SimpleNamespace(
        _epoch=lambda source: 0,
        launch_signature=lambda *args: 0,
        evaluate_many=lambda *args, **kwargs: [(manifest, {"P": np.array([[0, 0, 0], [1, 1, 1]])})],
        _signature=lambda path: (1, 2, 3, 4),
        StaleEvaluationError=RuntimeError,
    )
    fake_hou = SimpleNamespace(hipFile=SimpleNamespace(eventCallbacks=lambda: [fake_assets._hip_event]))
    monkeypatch.setattr(package, "blender_assets", fake_assets, raising=False)
    monkeypatch.setattr(package, "blend_worker", fake_worker, raising=False)
    monkeypatch.setitem(sys.modules, "hou", fake_hou)
    monkeypatch.setattr(bounds, "_refresh_job", None)
    monkeypatch.setattr(
        bounds, "publish_record", lambda *args, **kwargs: calls.append(("publish", threading.get_ident()))
    )
    monkeypatch.setattr(nb, "drain_pending_node_bounds", lambda: None)
    job = bounds.refresh_bounds("asset.blend#object=Cube", 1.0)
    assert job.done.wait(3)
    assert job.error is None and calls == []
    job.publish()
    assert calls == [("accept", threading.main_thread().ident), ("publish", threading.main_thread().ident)]
    job.publish()
    assert len(calls) == 2


def test_changed_hip_context_invalidates_leases_before_cook_hook_returns(monkeypatch):
    package = importlib.import_module("cycles_houdini")
    invalidations = []
    monkeypatch.setattr(
        package, "_native", SimpleNamespace(clear_blender_reference_bounds=lambda: invalidations.append(True))
    )
    monkeypatch.setattr(nb, "_relocated", False)
    monkeypatch.setattr(nb, "_hip_root", "C:/old_project")
    monkeypatch.setattr(nb, "_epoch", 7)
    state = dict(pending=True, fingerprint="old")
    monkeypatch.setattr(nb, "_subscribers", {1: state})
    monkeypatch.setattr(nb, "_publications", {("$HIP/asset.blend#object=Cube", 1.0): 7})
    fake_hou = SimpleNamespace(expandString=lambda expression: "C:/new_project")
    assert nb._synchronize_context(fake_hou)
    assert invalidations == [True] and nb.context_epoch() == 8
    assert state == dict(pending=False, fingerprint="")
    assert nb._publications == {}


def test_stale_render_publication_cannot_enter_native_cache(monkeypatch):
    package = importlib.import_module("cycles_houdini")
    publications = []
    monkeypatch.setattr(
        package, "_native", SimpleNamespace(publish_blender_reference_bounds=lambda *args: publications.append(args))
    )
    monkeypatch.setattr(nb, "_epoch", 7)
    captured = bounds.publication_generation()
    monkeypatch.setattr(nb, "_epoch", 8)
    with pytest.raises(RuntimeError, match="context changed"):
        bounds.publish_record("asset.blend#object=Cube", 1.0, dict(known=False), generation=captured)
    assert publications == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native ChangeTime contract")
def test_preserved_mtime_and_size_still_invalidate_saved_bounds(tmp_path):
    import os

    rec = record(tmp_path)
    source = Path(rec["source"])
    before = source.stat()
    # Consecutive Windows writes can share a ChangeTime tick. This contract
    # tests native ChangeTime invalidation, not content hashing within one tick.
    import time
    from cycles_houdini.blender.file_signature import native_signature

    time.sleep(0.03)
    source.write_bytes(b"SOURCE")  # exactly the same byte count
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    # Windows st_ctime is creation time; the native kernel ChangeTime catches it.
    after = source.stat()
    assert after.st_mtime_ns == before.st_mtime_ns and after.st_size == before.st_size
    assert native_signature(str(source)) != rec["native_signatures"][str(source)]
    assert not nb.validate_record(rec, rec["source"])


@pytest.fixture
def batch_host(monkeypatch):
    import numpy as np

    package = importlib.import_module("cycles_houdini.blender")
    evaluation, adoption, publication, resident = [], [], [], {}

    def resolve(spec):
        source, selector = spec[0].split("#object=")
        source = source.replace("./", "")
        key = ("blender.exe", source, "OBJECT", selector, spec[1], None, "working", spec[2])
        return key, dict(source=source, selector=selector, frame=spec[1])

    def evaluate(source, executable, requests, directory, **kwargs):
        evaluation.append((source, requests))
        return [
            (
                dict(
                    source=source,
                    selector_type="OBJECT",
                    selector=request["selector"],
                    frame=request["frame"],
                    dependency_versions={source: [1, 2, 3, 4]},
                    materials=[],
                    meshes=[dict(key="mesh", arrays=dict(positions="P"))],
                    instances=[dict(mesh_key="mesh", transform=np.eye(4).reshape(-1).tolist())],
                ),
                {"P": np.array([[0, 0, 0], [request["frame"], 1, 1]])},
            )
            for request in requests
        ]

    def adopt(key, *args):
        adoption.append(key)
        resident[key] = dict(key=key)
        return resident[key]

    assets = SimpleNamespace(
        _resolve_spec=resolve,
        _hip_event=object(),
        accept_evaluation=adopt,
        resident_evaluation=resident.get,
        _trim_prototypes=lambda: None,
    )
    worker = SimpleNamespace(
        evaluate_many=evaluate,
        _signature=lambda path: (1, 2, 3, 4),
        _epoch=lambda source: 0,
        launch_signature=lambda *args: 0,
        StaleEvaluationError=RuntimeError,
    )
    fake_hou = SimpleNamespace(hipFile=SimpleNamespace(eventCallbacks=lambda: [assets._hip_event]))
    monkeypatch.setattr(package, "blender_assets", assets, raising=False)
    monkeypatch.setattr(package, "blend_worker", worker, raising=False)
    monkeypatch.setitem(sys.modules, "hou", fake_hou)
    monkeypatch.setattr(bounds, "_refresh_job", None)
    monkeypatch.setattr(bounds, "_refresh_running", None)
    monkeypatch.setattr(bounds, "publish_record", lambda *args, **kwargs: publication.append(args))
    monkeypatch.setattr(nb, "drain_pending_node_bounds", lambda: None)
    yield SimpleNamespace(evaluation=evaluation, adoption=adoption, publication=publication, worker=worker)
    job = bounds._refresh_job
    if job is not None:
        job.cancel()
        assert job.done.wait(3)
        job.close()


def test_batch_two_frames_of_one_file_use_one_evaluation(batch_host):
    job = bounds.refresh_many([("asset.blend#object=Cube", 1, True), ("asset.blend#object=Cube", 2, True)])
    assert job.done.wait(3) and job.error is None
    assert len(batch_host.evaluation) == 1 and len(batch_host.evaluation[0][1]) == 2
    job.publish()
    assert len(batch_host.adoption) == len(batch_host.publication) == 2
    assert set(job.records) == {("asset.blend#object=Cube", 1.0, True), ("asset.blend#object=Cube", 2.0, True)}
    assert job.directories == {} and job.raw == {}


def test_batch_aliases_share_one_evaluation_and_conversion(batch_host):
    job = bounds.refresh_many([("asset.blend#object=Cube", 1, True), ("./asset.blend#object=Cube", 1, True)])
    assert job.done.wait(3) and job.error is None
    job.publish()
    assert len(batch_host.evaluation) == len(batch_host.evaluation[0][1]) == len(batch_host.adoption) == 1
    assert {item[0] for item in batch_host.publication} == {"asset.blend#object=Cube", "./asset.blend#object=Cube"}


def test_cancelled_batch_stops_remaining_source_groups_and_publishes_none(batch_host):
    entered, release = threading.Event(), threading.Event()
    evaluate = batch_host.worker.evaluate_many

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return evaluate(*args, **kwargs)

    batch_host.worker.evaluate_many = blocked
    job = bounds.refresh_many([("asset.blend#object=Cube", 1, True), ("other.blend#object=Cube", 1, True)])
    assert entered.wait(3)
    job.cancel("node deleted")
    release.set()
    assert job.done.wait(3) and isinstance(job.error, RuntimeError)
    assert len(batch_host.evaluation) == 1
    assert not job.raw and not job.directories
    with pytest.raises(RuntimeError):
        job.publish()
    assert not batch_host.adoption and not batch_host.publication


def test_error_in_one_frame_rejects_whole_source_transaction(batch_host):
    evaluate = batch_host.worker.evaluate_many

    def mixed(*args, **kwargs):
        result = evaluate(*args, **kwargs)
        result[1] = RuntimeError("bad second frame")
        return result

    batch_host.worker.evaluate_many = mixed
    job = bounds.refresh_many([("asset.blend#object=Cube", 1, True), ("asset.blend#object=Cube", 2, True)])
    assert job.done.wait(3) and isinstance(job.error, RuntimeError)
    assert not job.raw and not job.directories
    with pytest.raises(RuntimeError, match="bad second frame"):
        job.publish()
    assert not batch_host.adoption and not batch_host.publication


def test_headless_deleted_owner_cannot_adopt_or_publish_finished_batch(batch_host, monkeypatch):
    job = bounds.refresh_many([("asset.blend#object=Cube", 1, True)])
    assert job.done.wait(3) and job.error is None
    owner = object()
    job.owner = (42, owner)
    monkeypatch.setattr(sys.modules["hou"], "nodeBySessionId", lambda ident: None, raising=False)
    with pytest.raises(RuntimeError, match="node was deleted"):
        job.publish()
    assert not batch_host.adoption and not batch_host.publication
    assert not job.directories and not job.raw


def test_changed_owner_rows_cannot_publish_finished_batch(batch_host, monkeypatch):
    job = bounds.refresh_many([("asset.blend#object=Cube", 1, True)])
    assert job.done.wait(3) and job.error is None
    owner = object()
    job.owner = (42, owner)
    monkeypatch.setattr(sys.modules["hou"], "nodeBySessionId", lambda ident: owner, raising=False)
    monkeypatch.setattr(nb, "_rows", lambda node: [("asset.blend#object=Cube", 2.0, "asset.blend")])
    with pytest.raises(RuntimeError, match="inputs changed"):
        job.publish()
    assert not batch_host.adoption and not batch_host.publication


@pytest.mark.parametrize("stale_reason", ["token", "hip_epoch", "deleted_owner"])
def test_stale_validation_completion_cannot_consume_new_subscription(monkeypatch, stale_reason):
    import queue
    import time

    package = importlib.import_module("cycles_houdini")
    owner, replacement = object(), object()
    current_token, old_token = object(), object()
    state = dict(
        node=owner,
        token=current_token,
        fingerprint="same",
        pending=True,
        keys=set(),
        records=[],
        resolved={},
        last_checked=time.monotonic(),
    )
    completed = queue.Queue()
    completed.put(
        (
            42,
            6 if stale_reason == "hip_epoch" else 7,
            "same",
            old_token if stale_reason == "token" else current_token,
            [],
        )
    )
    monkeypatch.setattr(nb, "_completed", completed)
    monkeypatch.setattr(nb, "_epoch", 7)
    monkeypatch.setattr(nb, "_subscribers", {42: state})
    monkeypatch.setattr(nb, "_publications", {})
    fake_hou = SimpleNamespace(nodeBySessionId=lambda ident: replacement if stale_reason == "deleted_owner" else owner)
    monkeypatch.setitem(sys.modules, "hou", fake_hou)
    monkeypatch.setattr(package, "_native", SimpleNamespace())
    nb.drain_pending_node_bounds()
    if stale_reason == "deleted_owner":
        assert nb._subscribers == {}
    else:
        assert state["pending"] is True and nb._subscribers[42] is state


def test_refresh_attempt_metrics_include_cancelled_running_batch(batch_host):
    entered, release = threading.Event(), threading.Event()
    before = bounds.refresh_diagnostics()
    evaluate = batch_host.worker.evaluate_many

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return evaluate(*args, **kwargs)

    batch_host.worker.evaluate_many = blocked
    job = bounds.refresh_many([("asset.blend#object=Cube", 1, True), ("asset.blend#object=Cube", 2, True)])
    assert entered.wait(3)
    job.cancel("cancelled artist request")
    release.set()
    assert job.done.wait(3)
    after = bounds.refresh_diagnostics()
    assert after["refresh_evaluation_batches"] - before["refresh_evaluation_batches"] == 1
    assert after["refresh_evaluated_prototypes"] - before["refresh_evaluated_prototypes"] == 2
    assert after["refresh_cancelled_jobs"] - before["refresh_cancelled_jobs"] == 1
    assert job.metrics == dict(evaluation_batches=1, evaluated_prototypes=2)


@pytest.fixture
def node_publications(monkeypatch, tmp_path):
    from contextlib import nullcontext

    package = importlib.import_module("cycles_houdini")
    rec = record(tmp_path)
    key = rec["reference"], rec["frame"]

    class Parm:
        def __init__(self, value):
            self.value, self.writes = value, 0

        def unexpandedString(self):
            return self.value

        def set(self, value):
            self.value = value
            self.writes += 1

    class Node:
        def __init__(self):
            self.parms = {"saved_bounds": Parm(""), "saved_bounds_status": Parm("unknown")}
            self.cooks = []

        def parm(self, name):
            return self.parms[name]

        def cook(self, **kwargs):
            self.cooks.append(kwargs)

    node = Node()
    state = dict(node=node, keys={key}, records=[], pending=False, last_checked=0)
    native = SimpleNamespace(current=rec)

    def serialized(*args):
        return json.dumps(native.current) if native.current is not None else ""

    monkeypatch.setattr(package, "_native", SimpleNamespace(serialized_blender_reference_bounds=serialized))
    monkeypatch.setattr(nb, "_subscribers", {42: state})
    monkeypatch.setattr(nb, "_publications", {})
    monkeypatch.setattr(nb, "_rows", lambda owner: [(key[0], key[1], rec["source"])])
    monkeypatch.setitem(
        sys.modules,
        "hou",
        SimpleNamespace(
            nodeBySessionId=lambda ident: node,
            expandString=lambda value: value,
            undos=SimpleNamespace(disabler=nullcontext),
        ),
    )

    def publish():
        nb.notify_publication(*key)
        nb.drain_pending_node_bounds(publications_only=True)

    def initial(records, status):
        state["records"] = deepcopy(records)
        node.parm("saved_bounds").value = nb.encode_records(records)
        node.parm("saved_bounds_status").value = status

    return SimpleNamespace(record=rec, node=node, state=state, native=native, publish=publish, initial=initial)


def test_identical_render_publications_never_force_another_sop_cook(node_publications):
    host = node_publications
    host.initial([host.record], "validated")
    for _ in range(5):
        host.publish()
    assert host.node.cooks == []
    assert all(parm.writes == 0 for parm in host.node.parms.values())


@pytest.mark.parametrize("new_identity", [False, True])
def test_late_saved_bounds_validation_keeps_newer_resident_hull(node_publications, monkeypatch, new_identity):
    import queue
    import time

    package = importlib.import_module("cycles_houdini")
    host = node_publications
    old = deepcopy(host.record)
    hull = dict(points=[0, 0, 0, 1, 0, 0, 0, 1, 0], triangles=[0, 1, 2])
    host.native.current = dict(old, viewport_hulls=dict(whole=hull, members=[hull]))
    if new_identity:
        host.native.current["source_identity"] = "b" * 64
    host.initial([old], "validated")
    token = object()
    host.state.update(token=token, fingerprint="unchanged", pending=True, last_checked=time.monotonic())
    completed = queue.Queue()
    completed.put((42, nb._epoch, "unchanged", token, [(old, True)]))
    monkeypatch.setattr(nb, "_completed", completed)
    monkeypatch.setattr(nb, "cook_node", lambda node: None)
    publications = []
    monkeypatch.setattr(
        package._native, "publish_blender_reference_bounds", lambda *args: publications.append(args), raising=False
    )
    nb.drain_pending_node_bounds()
    assert not publications, "Late metadata validation replaced the completed viewport hull"
    assert nb.decode_records(host.node.parm("saved_bounds").value) == [host.native.current]


def test_failed_old_validation_never_retires_newer_published_source(node_publications, monkeypatch):
    import queue
    import time

    package = importlib.import_module("cycles_houdini")
    host = node_publications
    old = deepcopy(host.record)
    host.native.current = dict(old, source_identity="b" * 64, bounds=[-9, -3, -4, 5, 6, 7])
    host.initial([old], "validated")
    token = object()
    host.state.update(token=token, fingerprint="unchanged", pending=True, last_checked=time.monotonic())
    completed = queue.Queue()
    completed.put((42, nb._epoch, "unchanged", token, [(old, False)]))
    monkeypatch.setattr(nb, "_completed", completed)
    monkeypatch.setattr(nb, "cook_node", lambda node: None)
    publications = []
    monkeypatch.setattr(
        package._native, "publish_blender_reference_bounds", lambda *a: publications.append(a), raising=False
    )
    nb.drain_pending_node_bounds()
    assert not publications, "A failed stale check retired a newer authoritative source revision"
    assert nb.decode_records(host.node.parm("saved_bounds").value) == [host.native.current]


def test_shape_validation_retires_only_obsolete_identity_without_render_publication(node_publications, monkeypatch):
    import queue
    import time

    package = importlib.import_module("cycles_houdini")
    host = node_publications
    old = dict(host.record, max_hull_vertices=64)
    fresh = dict(old, source_identity="b" * 64, max_hull_vertices=128)
    token = object()
    state = dict(node=host.node, token=token, records=[old, fresh], pending=True, last_checked=time.monotonic())
    completed = queue.Queue()
    completed.put((42, nb._epoch, token, [((old["reference"], old["frame"]), old["source_identity"], False)]))
    monkeypatch.setattr(nb, "_shape_subscribers", {42: state})
    monkeypatch.setattr(nb, "_shape_completed", completed)
    monkeypatch.setattr(nb, "_cook_shape_node", lambda node: None)
    host.node.parms["saved_viewport_hulls"] = host.node.parms["saved_bounds"].__class__("")
    invalidated = []
    monkeypatch.setattr(
        package._native, "invalidate_blender_reference_preview", lambda *a: invalidated.append(a), raising=False
    )
    monkeypatch.setattr(
        package._native,
        "publish_blender_reference_bounds",
        lambda *a: pytest.fail("Cosmetic validation published render authority"),
        raising=False,
    )
    nb._drain_shape_validation(recook=False)
    assert invalidated == [(old["reference"], old["frame"], old["source_identity"])]
    assert state["records"] == [fresh]


def test_changed_bounds_cook_once_then_cached_publications_are_noops(node_publications):
    host = node_publications
    host.initial([host.record], "validated")
    host.native.current = dict(host.record, bounds=[-9, -3, -4, 5, 6, 7])
    host.publish()
    assert len(host.node.cooks) == 1
    assert nb.decode_records(host.node.parm("saved_bounds").value)[0]["bounds"][0] == -9
    host.publish()
    assert len(host.node.cooks) == 1


def test_previously_unknown_bounds_cook_once_when_published(node_publications):
    host = node_publications
    host.initial([], "unknown")
    host.publish()
    assert len(host.node.cooks) == 1
    assert host.node.parm("saved_bounds_status").value == "validated"
    host.publish()
    assert len(host.node.cooks) == 1


def test_unverified_preview_authority_transition_still_cooks(node_publications):
    host = node_publications
    host.initial([host.record], "unverified")
    host.publish()
    assert len(host.node.cooks) == 1
    host.publish()
    assert len(host.node.cooks) == 1


def test_unknown_publication_removes_old_record_without_repeated_cooks(node_publications):
    host = node_publications
    host.initial([host.record], "validated")
    host.native.current = None
    host.publish()
    assert len(host.node.cooks) == 1
    assert nb.decode_records(host.node.parm("saved_bounds").value) == []
    assert host.node.parm("saved_bounds_status").value == "unknown"
    host.publish()
    assert len(host.node.cooks) == 1
