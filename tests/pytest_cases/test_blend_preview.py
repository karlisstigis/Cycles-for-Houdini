# SPDX-License-Identifier: Apache-2.0
"""Shape Frame and hull detail remain node-owned and independent of render samples."""

from collections import OrderedDict
from contextlib import nullcontext
import json
from types import SimpleNamespace
import sys

import numpy as np
import pytest

from cycles_houdini.blender import blend_preview as preview, blend_bounds as bounds, blend_catalogue as catalogue
from cycles_houdini.blender import blend_worker
from cycles_houdini import blender as blender_package
import cycles_houdini

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


class Parm:
    def __init__(self, value):
        self.value = value

    def eval(self):
        return self.value

    def evalAtTime(self, _):
        return self.value

    def unexpandedString(self):
        return self.value

    def set(self, value):
        self.value = value


class Node:
    def __init__(self, ident, source):
        self.ident, self.source = ident, source
        self.values = {
            name: Parm(value)
            for name, value in dict(
                shape_frame=1.0,
                max_hull_vertices=64,
                viewport_shape="bounds",
                source_frame=1.0,
                saved_viewport_hulls="",
            ).items()
        }

    def parm(self, name):
        return self.values.get(name)

    def sessionId(self):
        return self.ident


@pytest.fixture
def host(monkeypatch, tmp_path):
    node = Node(1, str(tmp_path / "asset.blend"))
    monkeypatch.setitem(
        sys.modules,
        "hou",
        SimpleNamespace(
            text=SimpleNamespace(expandString=lambda text: text),
            nodeBySessionId=lambda ident: node if ident == 1 else None,
        ),
    )
    monkeypatch.setattr(cycles_houdini, "_native", SimpleNamespace(blender_reference_preview_valid=lambda *args: True))
    monkeypatch.setattr(
        catalogue,
        "resolved_reference_rows",
        lambda current, time=None: (
            [
                dict(
                    file=current.source,
                    kind="object",
                    target="Cube",
                    frame=current.parm("source_frame").eval(),
                )
            ],
            [],
        ),
    )
    return node


def record(node, cap=64):
    return dict(
        schema_version=1,
        known=True,
        reference=node.source + "#object=Cube",
        frame=1.0,
        source=node.source,
        selector_type="OBJECT",
        selector="Cube",
        source_identity="a" * 64,
        bounds=[0, 0, 0, 1, 1, 1],
        dependency_versions={node.source: [1, 2, 3, 4]},
        max_hull_vertices=cap,
    )


def test_distinct_node_limits_and_frozen_shape_sample_do_not_follow_source_frame(host):
    host.parm("saved_viewport_hulls").set(preview.encode_records([record(host, 32), record(host, 128)]))
    reference = host.source + "#object=Cube"
    for cap in (32, 128, 32):
        host.parm("max_hull_vertices").set(cap)
        host.parm("source_frame").set(987.25)
        assert preview.preview_rows(host) == [(reference, 1.0, host.source)]
        assert preview.matching_record(host, reference)["max_hull_vertices"] == cap
        assert preview.has_current(host)


def test_invalidated_or_differently_resolved_snapshot_is_never_reused(host, monkeypatch):
    host.parm("saved_viewport_hulls").set(preview.encode_records([record(host)]))
    monkeypatch.setattr(cycles_houdini._native, "blender_reference_preview_valid", lambda *args: False)
    assert not preview.has_current(host)
    monkeypatch.setattr(cycles_houdini._native, "blender_reference_preview_valid", lambda *args: True)
    host.source = host.source.replace("asset.blend", "different.blend")
    assert not preview.has_current(host)


def test_late_preview_job_ignores_source_frame_but_rejects_detail_change(host):
    job = bounds.BoundsRefresh()
    job.preview_mode, job.owner = True, (1, host)
    job.hull_vertices = 64
    job.specs = ((host.source + "#object=Cube", 1.0, True),)
    host.parm("source_frame").set(987.25)
    job.check_owner()
    host.parm("max_hull_vertices").set(128)
    with pytest.raises(RuntimeError, match="inputs changed"):
        job.check_owner()


def test_two_nodes_queue_different_details_without_cancelling_each_other(monkeypatch):
    source = "asset.blend#object=Cube"
    key = ("blender", "asset.blend", "OBJECT", "Cube", 1.0, None, "working", True)
    event = object()
    monkeypatch.setattr(bounds, "_preview_jobs", OrderedDict())
    monkeypatch.setattr(bounds, "_refresh_job", None)
    monkeypatch.setattr(bounds, "_refresh_running", object())
    monkeypatch.setattr(
        blender_package,
        "blender_assets",
        SimpleNamespace(
            _resolve_spec=lambda spec: (key, {}),
            _hip_event=event,
        ),
        raising=False,
    )
    monkeypatch.setattr(blend_worker, "_epoch", lambda _: 0)
    monkeypatch.setattr(blend_worker, "launch_signature", lambda *args: 0)
    monkeypatch.setitem(
        sys.modules,
        "hou",
        SimpleNamespace(
            isUIAvailable=lambda: False,
            hipFile=SimpleNamespace(eventCallbacks=lambda: [event]),
        ),
    )
    first = bounds.refresh_many([(source, 1.0, True)], hulls=True, hull_vertices=32, preview_owner=(1, object()))
    second = bounds.refresh_many([(source, 1.0, True)], hulls=True, hull_vertices=128, preview_owner=(2, object()))
    assert not first.cancelled.is_set() and not second.cancelled.is_set()
    assert [job.hull_vertices for job in bounds._preview_jobs.values()] == [32, 128]
    assert first.groups[0]["requests"][0]["max_hull_vertices"] == 32
    assert second.groups[0]["requests"][0]["max_hull_vertices"] == 128
    first.cancel()
    assert 1 not in bounds._preview_jobs and 2 in bounds._preview_jobs


def test_preview_publication_prewarms_actual_frame_without_publishing_render_bounds(host, monkeypatch):
    events = []
    reference = host.source + "#object=Cube"
    job = bounds.BoundsRefresh()
    job.preview_mode, job.owner = True, (1, host)
    job.hull_vertices = 64
    job.specs = ((reference, 1.0, True),)
    job.groups = [object()]
    job.records = {job.specs[0]: record(host)}
    job.done.set()
    monkeypatch.setattr(job, "check_owner", lambda: events.append("owner"))
    monkeypatch.setattr(job, "check_group", lambda index: events.append("check"))
    monkeypatch.setattr(job, "accept", lambda: events.append("adopt"))
    monkeypatch.setattr(preview, "persist", lambda node, records: events.append("persist"))
    monkeypatch.setattr(
        cycles_houdini._native,
        "restore_blender_reference_preview",
        lambda *args: events.append("restore"),
        raising=False,
    )
    monkeypatch.setattr(
        blender_package,
        "blender_assets",
        SimpleNamespace(
            _trim_prototypes=lambda: events.append("trim"),
        ),
        raising=False,
    )
    monkeypatch.setattr(host, "cook", lambda **kwargs: events.append("cook"), raising=False)
    monkeypatch.setattr(
        bounds,
        "publish_record",
        lambda *args, **kwargs: pytest.fail("Shape Frame preview published shared render/culling bounds"),
    )
    result = job.publish()
    assert result["reference"] == reference and result["frame"] == 1.0
    assert events == ["owner", "check", "adopt", "check", "restore", "persist", "trim", "cook"]
    assert job.published


def test_real_numpy_evaluation_persists_raw_preview_without_json_normalization(host, monkeypatch):
    manifest = dict(
        source=host.source,
        selector_type="OBJECT",
        selector="Cube",
        frame=1.0,
        dependency_versions={host.source: [1, 2, 3, 4]},
        materials=[],
        meshes=[dict(key="mesh", arrays=dict(positions="P"))],
        instances=[dict(mesh_key="mesh", transform=np.eye(4).reshape(-1).tolist())],
    )
    evaluated = bounds.evaluated_record(manifest, {"P": np.array([[-2, -1, 0], [3, 4, 5]], dtype=np.float32)})
    evaluated.update(reference=host.source + "#object=Cube", max_hull_vertices=64)
    assert all(type(value) is float for value in evaluated["bounds"])
    assert preview.valid_record(evaluated)
    monkeypatch.setattr(sys.modules["hou"], "undos", SimpleNamespace(disabler=nullcontext), raising=False)
    preview.persist(host, [evaluated])
    assert preview.saved_records(host) == [evaluated]
    assert preview.matching_record(host, evaluated["reference"]) == evaluated


def test_render_bounds_strip_large_node_owned_preview_before_byte_guard(host, monkeypatch):
    from cycles_houdini.blender import node_bounds

    published = []
    monkeypatch.setattr(
        cycles_houdini,
        "_native",
        SimpleNamespace(
            publish_blender_reference_bounds=lambda reference, frame, text: published.append(json.loads(text)),
            serialized_blender_reference_bounds=lambda *args: pytest.fail("Shared bounds read cosmetic preview state"),
        ),
    )
    monkeypatch.setattr(node_bounds, "notify_publication", lambda *args: None)
    payload = dict(
        record(host),
        viewport_hulls={"large_preview": "x" * (bounds.MAX_RECORD_BYTES * 2)},
        viewport_hull_reason="cosmetic-only",
    )
    bounds.publish_record(payload["reference"], payload["frame"], payload)
    assert len(published) == 1
    assert published[0]["source_identity"] == payload["source_identity"]
    assert published[0]["bounds"] == payload["bounds"]
    assert not {"viewport_hulls", "viewport_hull_reason", "max_hull_vertices"} & published[0].keys()
    assert len(json.dumps(published[0]).encode()) < bounds.MAX_RECORD_BYTES
