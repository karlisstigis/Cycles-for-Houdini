# SPDX-License-Identifier: Apache-2.0
"""Packed payload discovery, mixed native capture and shared immutable resources.

No Blender installation is needed: a small native mesh stands in for the evaluator.
The real exporter/render comparisons live in houdini_blend_instances_smoke.py.
"""

import argparse
import json
from pathlib import Path

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import blender_assets, instancing, native_scene
from blend_direct_fixture import mesh, prototype

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--out", required=True, type=Path)
parser.add_argument("--device", default="CPU")
args = parser.parse_args()
args.out.mkdir(parents=True, exist_ok=True)
hou.setFrame(4.5)
now = hou.time()
obj = hou.node("/obj").createNode("geo", "packed_adapter", run_init_scripts=False)
packed = obj.createNode("cycles::blender_reference")
packed.setParms(dict(file="$HIP/adapter fixture.blend", ref_target1="Cube", ref_frame1=1.25))
packed.parm("ref_frame1").deleteAllKeyframes()
packed.parm("ref_frame1").set(1.25)
reference = packed.geometry().prim(0).intrinsicValue("cycles_blender_reference")
saved = args.out / "serialized-reference.bgeo.sc"
packed.geometry().saveToFile(str(saved))
reopened = obj.createNode("file", "serialized_payload")
reopened.parm("file").set(str(saved))
left = obj.createNode("xform", "negative_instance")
left.setInput(0, reopened)
left.setParms(dict(tx=-1, sx=-1))
right = obj.createNode("xform", "positive_instance")
right.setInput(0, reopened)
right.parm("tx").set(1)
joined = obj.createNode("merge", "native_instances")
joined.setInput(0, left)
joined.setInput(1, right)
nested = obj.createNode("pack", "native_wrapper")
nested.setInput(0, joined)
nested.parm("pivot").set("origin")
missing = obj.createNode("cycles::blender_reference", "missing_blender")
missing.setParms(dict(file="$HIP/adapter fixture.blend", ref_target1="Unavailable", ref_frame1=1.25))
missing.parm("ref_frame1").deleteAllKeyframes()
missing.parm("ref_frame1").set(1.25)
box = obj.createNode("box", "ordinary_polygons")
box.parmTuple("size").set((0.4, 0.4, 0.2))
box.parmTuple("t").set((0, 1, 0))
points = obj.createNode("attribwrangle", "point_shortcut")
points.parm("class").set(0)
points.parm("snippet").set(
    'int p=addpoint(0,{0,-1,0}); setpointattrib(0,"instance",p,'
    + json.dumps(reference)
    + '); setpointattrib(0,"index",p,1.25);'
)
merge = obj.createNode("merge", "all_geometry")
for index, source in enumerate((nested, box, points, missing)):
    merge.setInput(index, source)
merge.setDisplayFlag(True)
merge.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam", "adapter_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
rop = hou.node("/out").createNode("cycles_ipr", "adapter_rop")
rop.setParms(dict(use_material_assignments=0, allowmotionblur=0))
prepared = prototype("adapter/shared", mesh("adapter/shared", size=(0.5, 0.5, 0.1)), (0.125, 0, 0))
calls, checks = [], []
original_many, original_warm = blender_assets.prepare_prototypes, blender_assets.prewarm_prototypes


def evaluate(specs, **options):
    calls.append(tuple(specs))
    return [
        RuntimeError("Blender not found at fixture path") if "Unavailable" in ref else prepared
        for ref, frame, native in specs
    ]


def requests():
    diagnostics = []
    with instancing.ExternalPlan([merge], now) as plan:
        result = native_scene.geometry_requests_for_rop(rop, merge, now, diagnostics=diagnostics, _external_plan=plan)
    return result, diagnostics


blender_assets.prepare_prototypes = evaluate
blender_assets.prewarm_prototypes = lambda specs: None
try:
    refs = ch.load().packed_blender_references(merge.path(), now)
    assert sorted(refs) == sorted([(reference, 1.25), (reference.replace("Cube", "Unavailable"), 1.25)]), refs
    checks.append("Serialized primitive survives file SOP and native pack wrappers")
    geometry, diagnostics = requests()
    assert (
        len(calls) == 1 and len({(hou.text.expandString(ref), frame, native) for ref, frame, native in calls[0]}) == 2
    ), calls
    assert any("Blender not found" in issue["message"] for issue in diagnostics), diagnostics
    from cycles_houdini.ipr.queued import _copy_update

    copied = _copy_update(dict(geometry=geometry))["geometry"]
    assert copied[0] is not geometry[0]
    assert (
        copied[0]["packed_blender"]["prototypes"][0]["direct_instances"][0]["mesh"] is prepared["instances"][0]["mesh"]
    )
    assert list(instancing.nested_requests(copied))
    checks.append(
        "Packed and point references share one evaluator batch and preserve targeted missing-Blender diagnostic"
    )
    with ch.Session(
        device=args.device,
        width=128,
        height=128,
        samples=4,
        threads=2,
        execution="worker",
        denoising={"enabled": False},
    ) as session:

        def render():
            geometry, diagnostics = requests()
            frame = session.wait(
                session.apply(
                    dict(
                        geometry=geometry,
                        source_diagnostics=diagnostics,
                        camera_path=camera.path(),
                        background=(0, 0, 0),
                        materials=[dict(key=f"rop:{rop.sessionId()}/surface", kind="emission", color=(0.8, 0.2, 0.1))],
                    ),
                    at_time=now,
                ),
                timeout=90,
            )
            status = session.status()
            assert not status["error"], status
            assert status["object_resources"] == 4, status
            image = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(128, 128, 4)
            return image.copy(), status, session.native_object_keys()

        image, status, identities = render()
        # The point's authored index adds an instance-field layout variant;
        # both entry points still share one immutable external mesh family.
        assert status["geometry_resources"] == 3 and status["mesh_family_resources"] == 2, status
        for y, x in ((64, 28), (64, 100), (32, 64), (96, 68)):
            assert image[y, x, 0] > 0.5, (y, x, image[y, x])
        assert image[64, 64, 0] < 0.01, "Missing reference rendered its viewport unit box"
        checks.append(
            "Mixed polygons, point shortcut and signed packed transforms render; unavailable proxy stays absent"
        )
        warm, warm_status, warm_ids = render()
        assert warm_ids == identities and np.max(np.abs(image - warm)) < 1e-5
        assert warm_status["extractions"] == status["extractions"], (status, warm_status)
        checks.append("Repeated capture preserves identities and geometry resources")
        right.parm("tx").set(1.125)
        moved, moved_status, moved_ids = render()
        assert moved_ids == identities and moved_status["geometry_resources"] == 3
        assert np.abs(moved - image).sum() > 10
        checks.append("Packed transform edit retains shared resources and stable identities")
    before = len(calls)
    assert ch.load().packed_blender_references(box.path(), now) == []
    with instancing.ExternalPlan([box], now) as plan:
        assert not plan.specs
    assert len(calls) == before
    checks.append("Ordinary polygon SOP never requests the Blender evaluator")
finally:
    blender_assets.prepare_prototypes, blender_assets.prewarm_prototypes = original_many, original_warm

report = dict(result="passed", checks=checks)
(args.out / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
