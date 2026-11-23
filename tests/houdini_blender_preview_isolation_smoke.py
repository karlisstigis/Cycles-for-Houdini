# SPDX-License-Identifier: Apache-2.0
"""Node-local Shape Frame/detail, shared copies and render-source isolation."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import hou
import cycles_houdini as ch
from cycles_houdini.blender import blend_bounds, blend_catalogue, blend_preview, blender_assets, node_bounds
from cycles_houdini.core.blender_bridge import require_executable


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--out", type=Path, required=True)
parser.add_argument("--reopen", type=Path)
parser.add_argument("--expected", type=Path)
args = parser.parse_args()
args.out.mkdir(parents=True, exist_ok=True)

if args.reopen:
    assert args.expected is not None
    expected = json.loads(args.expected.read_text(encoding="utf-8"))
    forbidden = []
    popen = subprocess.Popen

    def no_external_evaluation(*values, **options):
        forbidden.append(str(values[:1]))
        raise AssertionError("Displaying a persisted Shape Frame preview must not start Blender")

    subprocess.Popen = no_external_evaluation
    try:
        hou.hipFile.load(str(args.reopen), suppress_save_prompt=True)
        reopened = []
        for saved in expected:
            node = hou.node(saved["node"])
            assert node is not None, saved["node"]
            assert node.evalParm("shape_frame") == saved["shape_frame"]
            assert node.evalParm("ref_frame1") == saved["source_frame"]
            assert node.evalParm("max_hull_vertices") == saved["cap"]
            assert not node.parm("shape_frame").keyframes() and not node.parm("ref_frame1").keyframes()
            assert blend_preview.saved_records(node), node.path()
            for timeline in (1, 27, -3, 9):
                hou.setFrame(timeline)
                node.cook(force=True)
                primitive = node.geometry().prims()[0]
                assert not node.errors(), node.errors()
                assert primitive.intrinsicValue("cycles_blender_frame") == saved["source_frame"]
                assert primitive.intrinsicValue("cycles_blender_shape_frame") == saved["shape_frame"]
                hull = primitive.intrinsicValue("cycles_blender_viewport_hull")
                assert hull and json.loads(hull) == json.loads(saved["hull"]), (timeline, node.path(), node.warnings())
                assert not primitive.intrinsicValue("cycles_blender_bounds_known"), (
                    "Saved cosmetic preview became authoritative render bounds",
                    node.path(),
                )
                box = node.geometry().boundingBox()
                observed = [box.minvec()[0], box.maxvec()[0]]
                assert all(abs(left - right) < 1e-5 for left, right in zip(observed, saved["bounds_x"])), observed
                node_bounds.drain_pending_node_bounds()
            reopened.append(
                dict(
                    node=node.path(),
                    shape_frame=saved["shape_frame"],
                    source_frame=saved["source_frame"],
                    cap=saved["cap"],
                    render_bounds_known=False,
                )
            )
        assert not forbidden, forbidden
        assert not blender_assets.cache_diagnostics(workers=True)["workers"]
        (args.out / "report.json").write_text(
            json.dumps(dict(result="passed", checks=reopened, external_evaluations=forbidden), indent=2),
            encoding="utf-8",
        )
    finally:
        subprocess.Popen = popen
    raise SystemExit(0)

asset = args.out / "animated-preview.blend"
generator = args.out / "make-preview.py"
generator.write_text(
    "import bpy\n"
    "bpy.ops.wm.read_factory_settings(use_empty=True)\n"
    "bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=4, radius=1)\n"
    "obj = bpy.context.object\n"
    "obj.name = 'PreviewDense'\n"
    "for frame, x, width in ((1,-3,.5),(9,3,1.25)):\n"
    "    obj.location.x = x\n"
    "    obj.scale = (width,.75,1.0)\n"
    "    obj.keyframe_insert(data_path='location',frame=frame)\n"
    "    obj.keyframe_insert(data_path='scale',frame=frame)\n"
    "bpy.context.scene.frame_set(1)\n"
    f"bpy.ops.wm.save_as_mainfile(filepath={str(asset.resolve())!r}, relative_remap=False)\n",
    encoding="utf-8",
)
env = {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "PYTHONHOME", "OCIO")}
with (args.out / "fixture.log").open("w", encoding="utf-8") as log:
    subprocess.run(
        [
            require_executable(),
            "--background",
            "--factory-startup",
            "--disable-autoexec",
            "--python-exit-code",
            "1",
            "--python",
            str(generator),
        ],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        check=True,
        timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

reference = asset.resolve().as_posix() + "#object=PreviewDense"
geo = hou.node("/obj").createNode("geo", "preview_isolation", run_init_scripts=False)
report = dict(result="running", cases=[], fixture=str(asset), runtime=str(ch.__file__))
report_path = args.out / "report.json"


def pump(predicate, message, timeout=30):
    deadline = time.monotonic() + timeout
    while not predicate():
        node_bounds.drain_pending_node_bounds()
        blend_catalogue.drain_catalogues()
        if time.monotonic() >= deadline:
            raise TimeoutError(message)
        time.sleep(0.01)


def make_node(name, source_frame, shape_frame, cap):
    node = geo.createNode("cycles::blender_reference", name)
    for token in ("ref_frame1", "shape_frame"):
        node.parm(token).deleteAllKeyframes()
    node.setParms(
        dict(
            file=asset.resolve().as_posix(),
            ref_target_kind1="object",
            ref_target1="PreviewDense",
            ref_frame1=source_frame,
            shape_frame=shape_frame,
            max_hull_vertices=cap,
            viewport_shape="hull",
        )
    )
    # Seed the ID catalogue once so a later Source Frame edit is an ordinary
    # same-source edit, rather than first-time source discovery.
    job = blend_catalogue.request_catalogues(node, refresh_bounds=False)
    pump(lambda: job["done"].is_set(), "Fixture ID catalogue timed out", 120)
    blend_catalogue.drain_catalogues()
    assert not job["error"], job["error"]
    return node


def refresh(node):
    job = blend_bounds.refresh_node_bounds(node)
    assert job is not None, node.path()
    pump(job.done.is_set, "Preview refresh timed out", 120)
    diagnostic = []
    for spec, record in job.records.items():
        value = dict(record, reference=spec[0], frame=spec[1], max_hull_vertices=job.hull_vertices)
        diagnostic.append(
            dict(
                known=value.get("known"),
                raw_valid=blend_preview.valid_record(value),
                json_valid=blend_preview.valid_record(json.loads(json.dumps(value))),
                bound_types=[type(item).__name__ for item in value.get("bounds", [])],
                reason=value.get("viewport_hull_reason"),
            )
        )
    (args.out / "raw-record-diagnostic.json").write_text(json.dumps(diagnostic, indent=2), encoding="utf-8")
    job.publish()
    node.cook(force=True)
    assert not node.errors(), node.errors()
    return sample(node)


def sample(node, expected_shape=None, expected_source=None):
    node.cook(force=True)
    primitives = node.geometry().prims()
    assert len(primitives) == 1 and not node.errors(), (node.errors(), len(primitives))
    primitive = primitives[0]
    text = primitive.intrinsicValue("cycles_blender_viewport_hull")
    if not text:
        raw = node.parm("saved_viewport_hulls").unexpandedString()
        records = json.loads(raw).get("records", []) if raw else []
        diagnostic = dict(
            node=node.path(),
            warnings=node.warnings(),
            saved_bytes=len(raw),
            decoded_count=len(blend_preview.saved_records(node)),
            records=[
                dict(
                    reference=value.get("reference"),
                    frame=value.get("frame"),
                    known=value.get("known"),
                    cap=value.get("max_hull_vertices"),
                    valid=blend_preview.valid_record(value),
                    hull_keys=list(value.get("viewport_hulls", {})),
                    reason=value.get("viewport_hull_reason"),
                )
                for value in records
            ],
            options={
                key: primitive.intrinsicValue(key)
                for key in ("cycles_blender_frame", "cycles_blender_shape_frame", "cycles_blender_shape_bounds")
            },
        )
        (args.out / "empty-preview-diagnostic.json").write_text(json.dumps(diagnostic, indent=2), encoding="utf-8")
    assert text, (node.path(), node.warnings())
    hull = json.loads(text)
    counts = [len(piece["points"]) // 3 for piece in hull["pieces"]]
    assert counts and all(3 <= count <= node.evalParm("max_hull_vertices") for count in counts), counts
    shape = primitive.intrinsicValue("cycles_blender_shape_frame")
    source = primitive.intrinsicValue("cycles_blender_frame")
    assert shape == node.evalParm("shape_frame") and source == node.evalParm("ref_frame1"), (shape, source)
    if expected_shape is not None:
        assert shape == expected_shape, (shape, expected_shape)
    if expected_source is not None:
        assert source == expected_source, (source, expected_source)
    box = node.geometry().boundingBox()
    center = (box.minvec()[0] + box.maxvec()[0]) / 2
    if shape in (1, 9):
        assert abs(center - (-3 if shape == 1 else 3)) < 0.1, (node.path(), shape, box)
    record = blend_preview.matching_record(node, reference)
    assert record is not None and record["frame"] == shape, record
    assert record["max_hull_vertices"] == node.evalParm("max_hull_vertices"), record
    return dict(
        node=node.path(),
        source_frame=source,
        shape_frame=shape,
        counts=counts,
        detail_id=primitive.intrinsicValue("cycles_blender_viewport_detail_id"),
        hull=text,
        bounds_x=[box.minvec()[0], box.maxvec()[0]],
        source_identity=record["source_identity"],
    )


def evaluation_counters():
    diagnostics = blender_assets.cache_diagnostics(workers=True)
    runtime = diagnostics["runtime"]
    return dict(
        evaluation_batches=runtime.get("evaluation_batches", 0),
        evaluated_prototypes=runtime.get("evaluated_prototypes", 0),
        refresh_evaluation_batches=runtime["refresh_evaluation_batches"],
        refresh_evaluated_prototypes=runtime["refresh_evaluated_prototypes"],
        worker_batches=sum(worker["transfer"].get("batches", 0) for worker in diagnostics["workers"]),
    )


try:
    low = make_node("low_detail_shape1_source9", 9, 1, 32)
    high = make_node("high_detail_shape1_source1", 1, 1, 128)
    first_low = refresh(low)
    first_high = refresh(high)
    assert first_high["counts"][0] > first_low["counts"][0], (first_low, first_high)
    assert first_low["detail_id"] != first_high["detail_id"], (first_low, first_high)
    # Both nodes refer to exactly the same shape sample and dependency identity,
    # while their independently selected detail stays intact in either cook order.
    before = evaluation_counters()
    for timeline in (1, 37, -2, 9, 0.5, 1):
        hou.setFrame(timeline)
        for node, original in ((low, first_low), (high, first_high), (low, first_low)):
            current = sample(node, 1, node.evalParm("ref_frame1"))
            assert current["hull"] == original["hull"], (timeline, current, original)
    assert evaluation_counters() == before
    report["cases"].append(dict(check="cross-node detail and fixed Shape Frame", low=first_low, high=first_high))

    high.parm("shape_frame").set(9)
    opposite = refresh(high)
    assert opposite["shape_frame"] == 9 and opposite["source_frame"] == 1, opposite
    assert sample(low, 1, 9)["hull"] == first_low["hull"]
    report["cases"].append(dict(check="opposite Shape and Source Frames", low=sample(low, 1, 9), high=opposite))

    # Publishing actual render-source bounds must not replace the independently
    # selected viewport position. This is render preflight, without rendering.
    prepared = blender_assets.prepare_prototypes([(reference, frame, True) for frame in (9, 1)], publish_bounds=True)
    (args.out / "source-bounds-diagnostic.json").write_text(
        json.dumps(
            [
                dict(
                    frame=frame,
                    bounds_record=entry.get("bounds_record"),
                    diagnostics=entry.get("diagnostics"),
                    published=ch.load().serialized_blender_reference_bounds(reference, frame),
                )
                for frame, entry in zip((9, 1), prepared)
            ],
            indent=2,
        ),
        encoding="utf-8",
    )
    low.cook(force=True)
    high.cook(force=True)
    for node, render_frame, shape_frame, expected_x in ((low, 9, 1, 3), (high, 1, 9, -3)):
        primitive = node.geometry().prims()[0]
        render_record = json.loads(primitive.intrinsicValue("cycles_blender_bounds"))
        assert primitive.intrinsicValue("cycles_blender_bounds_known"), render_record
        assert render_record["frame"] == render_frame, render_record
        render_center = (render_record["bounds"][0] + render_record["bounds"][3]) / 2
        assert abs(render_center - expected_x) < 0.1, render_record
        assert "viewport_hulls" not in render_record, render_record
        sample(node, shape_frame, render_frame)
    report["cases"].append(dict(check="viewport bounds independent of authoritative render bounds"))

    before = evaluation_counters()
    for node, shape_center in ((low, -3), (high, 3)):
        node.parm("viewport_shape").set("bounds")
        blend_catalogue.viewport_changed(node)
        node.cook(force=True)
        box = node.geometry().boundingBox()
        assert abs((box.minvec()[0] + box.maxvec()[0]) / 2 - shape_center) < 0.1, box
        node.parm("viewport_shape").set("hull")
        blend_catalogue.viewport_changed(node)
        sample(node)
    assert evaluation_counters() == before
    report["cases"].append(dict(check="Bounding Box mode also uses Shape Frame without reevaluation"))

    before = evaluation_counters()
    low.parm("ref_frame1").set(5)
    source_edit_job = blend_catalogue.selection_changed(low)
    if source_edit_job is not None:
        if isinstance(source_edit_job, dict):
            pump(lambda: source_edit_job["done"].is_set(), "Source edit callback did not finish", 120)
        else:
            pump(source_edit_job.done.is_set, "Source edit callback did not finish", 120)
            source_edit_job.publish()
    assert sample(low, 1, 5)["hull"] == first_low["hull"]
    pump(
        lambda: not any(state["pending"] for state in node_bounds._subscribers.values()),
        "Saved preview validation timed out",
    )
    assert evaluation_counters() == before, (before, evaluation_counters())
    report["cases"].append(dict(check="Source Frame-only edits do not evaluate Blender previews"))

    before = evaluation_counters()
    low.parm("ref_frame1").setExpression("$FF+.25", hou.exprLanguage.Hscript)
    expression_samples = []
    # Deliberately fetch public geometry directly after changing Houdini time.
    # A forced cook or Python evalAtTime here could hide a missing HDK time
    # dependency on the Source Frame channel.
    for timeline in (1, 9, 1):
        hou.setFrame(timeline)
        primitive = low.geometry().prims()[0]
        actual_source = primitive.intrinsicValue("cycles_blender_frame")
        assert actual_source == timeline + 0.25, (timeline, actual_source)
        assert primitive.intrinsicValue("cycles_blender_shape_frame") == 1
        assert primitive.intrinsicValue("cycles_blender_viewport_hull") == first_low["hull"]
        assert primitive.intrinsicValue("cycles_blender_viewport_hull_points") == sum(first_low["counts"])
        references = ch.load().packed_blender_references(low.path(), hou.time())
        assert references == [(reference, timeline + 0.25)], references
        expression_samples.append(
            dict(
                timeline=timeline,
                source_frame=actual_source,
                shape_frame=1,
                detail_id=primitive.intrinsicValue("cycles_blender_viewport_detail_id"),
            )
        )
    assert evaluation_counters() == before, (before, evaluation_counters())
    low.parm("ref_frame1").deleteAllKeyframes()
    low.parm("ref_frame1").set(5)
    report["cases"].append(
        dict(check="live Source Frame expression follows unforced timeline cooks", samples=expression_samples)
    )

    points = geo.createNode("attribwrangle", "copy_positions")
    points.parm("class").set(0)
    points.parm("snippet").set("for(int i=0;i<32;i++)addpoint(0,set(i*.25,0,0));")
    copies = geo.createNode("copytopoints::2.0", "shared_preview_copies")
    copies.setInput(0, low)
    copies.setInput(1, points)
    copies.parm("pack").set(0)
    frozen = copies.geometry().freeze()
    primitives = frozen.prims()
    assert len(primitives) == 32, len(primitives)
    detail_ids = {primitive.intrinsicValue("cycles_blender_viewport_detail_id") for primitive in primitives}
    assert len(detail_ids) == 1 and next(iter(detail_ids)) > 0, detail_ids
    assert all(primitive.intrinsicValue("cycles_blender_viewport_hull_points") > 3 for primitive in primitives)
    # A recook/refresh of another detail must not invalidate held packed copies.
    refresh(high)
    assert all(primitive.intrinsicValue("cycles_blender_viewport_hull_points") > 3 for primitive in primitives)
    report["cases"].append(dict(check="32 packed copies share one viewport detail", detail_ids=list(detail_ids)))

    # Unprepared render samples ensure a fresh process can only obtain its
    # cosmetic preview from persistence, never from render-source authority.
    source_frames = [node.evalParm("ref_frame1") for node in (low, high)]
    persisted = []
    before = evaluation_counters()
    for node, render_frame in ((low, 10001), (high, 10009)):
        node.parm("ref_frame1").set(render_frame)
        value = sample(node)
        assert not node.geometry().prims()[0].intrinsicValue("cycles_blender_bounds_known")
        persisted.append(dict(value, cap=node.evalParm("max_hull_vertices")))
    assert evaluation_counters() == before
    saved_hip = args.out / "persisted-previews.hiplc"
    expected_path = args.out / "reopen-expected.json"
    expected_path.write_text(json.dumps(persisted, indent=2), encoding="utf-8")
    hou.hipFile.save(str(saved_hip))
    assert evaluation_counters() == before, (before, evaluation_counters())
    child = args.out / "fresh-reopen"
    child.mkdir(exist_ok=True)
    child_env = dict(
        os.environ,
        CYCLES_HOUDINI_TEST_ARTIFACTS=str((child / "artifacts").resolve()),
        CYCLES_HOUDINI_TEST_IDENTITY_REPORT=str((child / "identity.json").resolve()),
        HOUDINI_USER_PREF_DIR=str((child / "prefs/houdini__HVER__").resolve()),
    )
    with (child / "process.log").open("w", encoding="utf-8") as log:
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("native_test_entry.py").resolve()),
                str(Path(__file__).resolve()),
                "--out",
                str(child.resolve()),
                "--reopen",
                str(saved_hip.resolve()),
                "--expected",
                str(expected_path.resolve()),
            ],
            env=child_env,
            cwd=child,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    reopened = json.loads((child / "report.json").read_text(encoding="utf-8"))
    assert reopened["result"] == "passed", reopened
    for node, source_frame in zip((low, high), source_frames):
        node.parm("ref_frame1").set(source_frame)
        sample(node)
    report["cases"].append(
        dict(
            check="fresh HIP reopen preserves settings and previews without evaluation",
            child_report=str(child / "report.json"),
            reopened=reopened,
        )
    )

    before = evaluation_counters()
    # Save As may invalidate the previous HIP context's tokens. Acquire current
    # copies again so the dependency-change test starts with live previews.
    frozen = copies.geometry().freeze()
    primitives = frozen.prims()
    assert len(primitives) == 32
    assert all(primitive.intrinsicValue("cycles_blender_viewport_hull_points") > 3 for primitive in primitives)
    stamp = asset.stat()
    os.utime(asset, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 2_000_000_000))
    for node in (low, high):
        node.cook(force=True)

    def invalidated():
        return all(primitive.intrinsicValue("cycles_blender_viewport_hull_points") == 0 for primitive in primitives)

    pump(invalidated, "Changed asset did not invalidate every held preview variant", 30)
    for node in (low, high):
        node.cook(force=True)
        assert node.geometry().prims()[0].intrinsicValue("cycles_blender_viewport_hull_points") == 0
    assert evaluation_counters() == before, (before, evaluation_counters())
    report["cases"].append(
        dict(check="source signature change invalidates all detail/frame variants without evaluation")
    )
    report["result"] = "passed"
finally:
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
