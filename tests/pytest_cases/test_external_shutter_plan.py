# SPDX-License-Identifier: Apache-2.0
"""Shutter discovery batches point and packed source states without host mutation."""

import importlib
import sys
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


@pytest.mark.parametrize(
    "settings", [None, (True, "deformation"), (False, "deformation"), (True, "transform"), (True, "velocity")]
)
def test_source_shutter_union_and_exact_default_frames(monkeypatch, settings):
    hou = SimpleNamespace(objNodeTypeCategory=lambda: "OBJ", timeToFrame=lambda time: time * 24 + 1, Error=RuntimeError)
    monkeypatch.setitem(sys.modules, "hou", hou)
    instancing = importlib.import_module("cycles_houdini.scene.instancing")
    monkeypatch.setattr(instancing, "hou", hou)
    owner = SimpleNamespace(type=lambda: SimpleNamespace(category=lambda: "OBJ"), parm=lambda name: None)
    sop = SimpleNamespace(parent=lambda: owner, sessionId=lambda: 17)
    point_ref, packed_ref = "point.blend#object=Shape", "packed.blend#collection=Group"
    monkeypatch.setattr(instancing, "is_instancer", lambda obj: False)
    monkeypatch.setattr(instancing, "referenced_values", lambda obj, sop, time: ((point_ref,), True))
    monkeypatch.setattr(
        instancing, "external_frames", lambda sop, time, values: ({point_ref: {hou.timeToFrame(time)}}, 0)
    )
    monkeypatch.setattr(instancing, "packed_references", lambda sop, time: ((packed_ref, hou.timeToFrame(time) + 0.5),))
    batches = []

    def prepare(specs, **options):
        batches.append((list(specs), options))
        return [dict(spec=spec) for spec in specs]

    assets = SimpleNamespace(
        prewarm_prototypes=lambda specs: SimpleNamespace(scheduled=False, close=lambda: None),
        prepare_prototypes=prepare,
    )
    package = importlib.import_module("cycles_houdini.blender")
    monkeypatch.setattr(package, "blender_assets", assets, raising=False)
    now, times = 1.25, (1.0, 1.25, 1.5)
    rop = None
    selected_times = times
    if settings is not None:
        rop = SimpleNamespace(parm=lambda name: SimpleNamespace(evalAtTime=lambda time: "deformation"))
        monkeypatch.setattr(
            instancing, "motion_settings", lambda *args, **kwargs: dict(motion_blur=settings[0], motion=settings[1])
        )
        if settings != (True, "deformation"):
            selected_times = (now,)
    with instancing.ExternalPlan([sop], now, times, rop=rop) as plan:
        expected = {(point_ref, hou.timeToFrame(t), True) for t in selected_times}
        expected |= {(packed_ref, hou.timeToFrame(t) + 0.5, True) for t in selected_times}
        assert set(plan.specs) == expected
        assert plan.observation(sop)[2] == {point_ref: {hou.timeToFrame(now)}}
        assert plan.sampled_point_frames(sop) == {point_ref: {hou.timeToFrame(t) for t in selected_times}}
        assert {sample["time"]: sample["frame"] for sample in plan.frame_samples()} == {
            t: hou.timeToFrame(t) for t in times
        }
        for spec in expected:
            assert plan.get(*spec)["spec"] == spec
        assert len(batches) == 1 and set(batches[0][0]) == expected
        assert batches[0][1]["bounds_specs"] == {(packed_ref, hou.timeToFrame(t) + 0.5, True) for t in selected_times}


@pytest.mark.parametrize(
    "override,point_mode,enabled,expected",
    [
        ("inherit", "deform", True, (True, "deformation")),
        ("inherit", "velocity", True, (True, "velocity")),
        ("inherit", "off", True, (False, "transform")),
        ("deformation", "off", True, (True, "deformation")),
        ("inherit", "deform", False, (False, "deformation")),
    ],
)
def test_instance_obj_motion_policy_reuses_object_overrides(monkeypatch, override, point_mode, enabled, expected):
    monkeypatch.setitem(sys.modules, "hou", SimpleNamespace())
    instancing = importlib.import_module("cycles_houdini.scene.instancing")
    package = importlib.import_module("cycles_houdini.scene")
    properties = SimpleNamespace(
        motion_enabled=lambda node, time: enabled,
        motion_mode=lambda node, time, inherited: inherited if override == "inherit" else override,
        _value=lambda *args: override,
    )
    monkeypatch.setattr(package, "object_properties", properties, raising=False)
    monkeypatch.setattr(instancing, "motion", lambda node, time: point_mode)
    result = instancing.motion_settings(object(), 0, "transform", point_instancer=True)
    assert (result["motion_blur"], result["motion"]) == expected


def test_deformation_discovery_reads_no_other_time_without_blender_references(monkeypatch):
    """Reading geometry at a shutter time cooks the SOP there (task M1): only the frame is read."""
    hou = SimpleNamespace(objNodeTypeCategory=lambda: "OBJ", timeToFrame=lambda time: time * 24 + 1, Error=RuntimeError)
    monkeypatch.setitem(sys.modules, "hou", hou)
    instancing = importlib.import_module("cycles_houdini.scene.instancing")
    monkeypatch.setattr(instancing, "hou", hou)
    owner = SimpleNamespace(type=lambda: SimpleNamespace(category=lambda: "OBJ"), parm=lambda name: None)
    sop = SimpleNamespace(parent=lambda: owner, sessionId=lambda: 17)
    read = []
    monkeypatch.setattr(instancing, "is_instancer", lambda obj: False)
    monkeypatch.setattr(instancing, "referenced_values", lambda obj, sop, time: read.append(time) or ((), False))
    monkeypatch.setattr(instancing, "external_frames", lambda sop, time, values: ({}, 0))
    monkeypatch.setattr(instancing, "packed_references", lambda sop, time: read.append(time) or ())
    monkeypatch.setattr(
        instancing, "motion_settings", lambda *args, **kwargs: dict(motion_blur=True, motion="deformation")
    )
    rop = SimpleNamespace(parm=lambda name: SimpleNamespace(evalAtTime=lambda time: "deformation"))
    now = 1.25
    with instancing.ExternalPlan([sop], now, (1.0, 1.25, 1.5), rop=rop) as plan:
        assert not plan.specs
    assert read == [now, now]
