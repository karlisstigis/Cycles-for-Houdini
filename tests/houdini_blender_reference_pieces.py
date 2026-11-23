# SPDX-License-Identifier: Apache-2.0
"""Named Copy to Points pieces, file overrides and persistent final source samples.

The default gate compares packed and attribute routes at two fractional source
frames, warm captures and Houdini placement motion. --source-shutter additionally
requires animated source geometry at every explicit fractional shutter sample.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import time

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import blender_assets, native_scene
from cycles_houdini.blender import blend_catalogue, blend_bounds
from cycles_houdini.device_selection import set_devices

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--out", type=Path, required=True)
parser.add_argument("--device", default="CPU")
parser.add_argument("--blender", type=Path)
parser.add_argument("--source-shutter", action="store_true")
parser.add_argument("--static-only", action="store_true", help="Independent named-piece and persistence gate")
parser.add_argument(
    "--simplified-selection", action="store_true", help="Shared file, visible selectors and automatic piece names"
)
parser.add_argument(
    "--hull-preview", action="store_true", help="Evaluate real object and collection convex viewport previews"
)
parser.add_argument("--repeat-piece", action="store_true", help="Copy one source piece to more than one named target")
parser.add_argument("--no-point-id", action="store_true", help="Ordinary name-only target point workflow")
parser.add_argument("--string-point-id", action="store_true", help="String IDs on the packed target points")
parser.add_argument(
    "--reorder-shutter", action="store_true", help="Reverse creation order during the shutter with stable explicit IDs"
)
parser.add_argument("--source-shutter-probe", action="store_true", help="Capture-only gate for required source samples")
args = parser.parse_args()
if args.hull_preview and not args.simplified_selection:
    parser.error("--hull-preview requires --simplified-selection")
if args.no_point_id and args.reorder_shutter:
    parser.error("Reordering requires explicit IDs; the no-ID control retains point order")
requested_device = args.device
if args.device == "OPTIX":
    from support.devices import resolve_device_argument

    resolved, _ = resolve_device_argument(
        __file__, ["--device", args.device], ch.devices(), os.environ.get("CYCLES_HOUDINI_TEST_OPTIX_DEVICE")
    )
    args.device = resolved[1]
args.out = args.out.resolve()
args.out.mkdir(parents=True, exist_ok=True)
if args.blender:
    hou.putenv("CYCLES_HOUDINI_BLENDER", str(args.blender))
asset_dir = args.out / "assets"
manifest = asset_dir / "pieces-fixture.json"
cached_records = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else []
if (
    len(cached_records) < 3
    or args.simplified_selection
    and not all(record.get("shared_file") for record in cached_records[:2])
    or args.hull_preview
    and not all(record.get("shared_collection") for record in cached_records[:2])
):
    command = [
        str(args.blender or blender_assets._blender()),
        "--background",
        "--factory-startup",
        "--disable-autoexec",
        "--python-exit-code",
        "1",
        "--python",
        str(Path(__file__).with_name("blend_reference_pieces_fixture.py")),
        "--",
        "--out",
        str(asset_dir),
    ]
    env = {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "PYTHONHOME", "OCIO")}
    with (args.out / "fixture.log").open("w") as log:
        subprocess.run(
            command,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
records = json.loads(manifest.read_text(encoding="utf-8"))
fixture, topology = records[:2], records[2]
for record in records:
    record["file"] = Path(record["file"]).as_posix()
    if args.simplified_selection and record.get("shared_file"):
        record["file"] = Path(record["shared_file"]).as_posix()
refs = [record["file"] + "#object=" + record["name"] for record in fixture]
geo = hou.node("/obj").createNode("geo", "named_blender_pieces", run_init_scripts=False)
source = geo.createNode("cycles::blender_reference", "multiple_sources")
source.setParms(dict(file=fixture[0]["file"], target=fixture[0]["name"], multiple_references=1))
blend_catalogue.initialize_rows(source)
source.parm("references").set(3)
blend_catalogue.initialize_rows(source)
for i, piece in enumerate(("red_piece", "blue_piece"), 1):
    source.setParms({f"ref_name{i}": piece, f"ref_target{i}": fixture[i - 1]["name"], f"ref_target_kind{i}": "object"})
source.parm("ref_file2").set(fixture[1]["file"])
piece_names = ("red_piece", "blue_piece")
if args.simplified_selection:
    piece_names = tuple(record["name"].replace(" ", "_") for record in fixture)
    source.setParms(
        dict(
            ref_name1="",
            ref_name2="",
            ref_file1="",
            ref_file2="",
            ref_target1="Smooth_R* Smooth_Red",
            ref_target2="Smooth_Blue",
        )
    )
    job = blend_catalogue.request_catalogues(source)
    deadline = time.monotonic() + 60
    while not job["done"].is_set():
        blend_catalogue.drain_catalogues()
        assert time.monotonic() < deadline, "Shared library catalogue timed out"
        time.sleep(0.01)
    blend_catalogue.drain_catalogues()
    assert not job["error"], job["error"]
    blend_catalogue.bind_targets(source)
    source.cook(force=True)
    assert list(source.geometry().primStringAttribValues("name")) == list(piece_names)
    assert [p.intrinsicValue("cycles_blend_target") for p in source.geometry().prims()] == [
        record["name"] for record in fixture
    ]
source.setParms(dict(ref_enabled3=0, ref_file3="missing disabled.blend", ref_target3="Never Load"))
targets = geo.createNode("attribwrangle", "named_target_points")
targets.parm("class").set(0)
attribute = geo.createNode("attribwrangle", "attribute_reference_points")
attribute.parm("class").set(0)
copies = geo.createNode("copytopoints::2.0", "copy_named_pieces")
copies.setInput(0, source)
copies.setInput(1, targets)
copies.setParms(dict(useidattrib=1, idattrib="name", pack=0))
if not args.no_point_id:
    # Match the ordinary Copy to Points workflow for forwarding stable target
    # IDs; the no-ID control intentionally needs only the piece selector.
    copies.parm("targetattribs").set(1)
    copies.parm("applyattribs1").set("id")
camera = hou.node("/obj").createNode("cam", "pieces_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4, shutter=1))
rop = hou.node("/out").createNode("cycles_ipr", "pieces_final")
set_devices(rop, args.device)
rop.setParms(
    dict(
        source_mode="objects",
        objectroot="/obj",
        vobject=geo.name(),
        lights="",
        camera=camera.path(),
        resx=64,
        resy=64,
        samples=8,
        ipr_samples=8,
        use_denoise=0,
        ipr_use_denoise=0,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        transparent_background=1,
        pixel_filter="box",
        filter_width=1,
        use_material_assignments=1,
        allowmotionblur=0,
        motion_samples=3,
        geometry_motion="deformation",
        render_threads=2,
        seed=5,
        animate_seed=0,
    )
)
rop.parmTuple("background").set((0, 0, 0))
report = dict(
    result="running",
    device=args.device,
    requested_device=requested_device,
    simplified_selection=args.simplified_selection,
    checks=[],
    renders=[],
)
report_path = args.out / "pieces-report.json"


def refresh_hull(node):
    job = blend_bounds.refresh_node_bounds(node)
    deadline = time.monotonic() + 120
    while not job.done.is_set():
        assert time.monotonic() < deadline, "Hull refresh timed out"
        time.sleep(0.01)
    job.publish()
    node.cook(force=True)
    assert not node.errors(), node.errors()
    primitives = node.geometry().prims()
    diagnostic = dict(
        node=node.path(),
        saved_bounds_bytes=len(node.parm("saved_bounds").unexpandedString().encode()),
        saved_preview_bytes=len(node.parm("saved_viewport_hulls").unexpandedString().encode()),
        worker_hulls=[
            dict(has_hulls=bool(value.get("viewport_hulls")), reason=value.get("viewport_hull_reason"))
            for value in job.records.values()
        ],
        resolver_messages=blend_catalogue.resolve_node_references(node)[:2],
    )
    for primitive in primitives:
        assert primitive.intrinsicValue("cycles_blender_viewport_hull_points") > 3, diagnostic
        assert primitive.intrinsicValue("cycles_blender_viewport_hull_triangles") > 1, diagnostic
    assert len({p.points()[0].number() for p in primitives}) == len(primitives)
    # A queued saved-record validation must not retire a newer hull publication.
    from cycles_houdini.blender import node_bounds

    deadline = time.monotonic() + 10
    while any(state["pending"] for state in node_bounds._subscribers.values()):
        node_bounds.drain_pending_node_bounds()
        assert time.monotonic() < deadline, "Saved hull dependency validation timed out"
        time.sleep(0.01)
    assert all(p.intrinsicValue("cycles_blender_viewport_hull_points") > 3 for p in node.geometry().prims()), diagnostic
    return [json.loads(p.intrinsicValue("cycles_blender_viewport_hull"))["pieces"] for p in primitives]


if args.hull_preview:
    assert source.evalParm("shape_frame") == 1 and source.evalParm("viewport_wireframe") == 1
    source.parm("viewport_shape").set("hull")
    collection = geo.createNode("cycles::blender_reference", "whole_and_member_hulls")
    collection.setParms(
        dict(file=fixture[0]["file"], ref_target_kind1="collection", ref_target1="Shared_Pieces", viewport_shape="hull")
    )
    menu_job = blend_catalogue.request_catalogues(collection)
    deadline = time.monotonic() + 60
    while not menu_job["done"].is_set():
        blend_catalogue.drain_catalogues()
        assert time.monotonic() < deadline
        time.sleep(0.01)
    blend_catalogue.drain_catalogues()
    assert not menu_job["error"], menu_job["error"]
    whole = refresh_hull(collection)
    assert len(whole) == 1 and len(whole[0]) == 1
    collection.parm("ref_hull_per_member1").set(1)
    collection.cook(force=True)
    member = json.loads(collection.geometry().prim(0).intrinsicValue("cycles_blender_viewport_hull"))["pieces"]
    assert len(member) == 2
    assert collection.geometry().prim(0).intrinsicValue("cycles_blender_viewport_hull_points") == sum(
        len(p["points"]) // 3 for p in member
    )
    report["checks"].append(dict(real_collection_hull=True, whole_pieces=1, member_pieces=2))


def author(frame, *, placement=False, source_motion=False):
    hou.setFrame(frame)
    points, instances = [], []
    for index, piece_index in enumerate((0, 1, 0) if args.repeat_piece else (0, 1)):
        piece = piece_names[piece_index]
        offset = piece_index * 0.5
        selected = "@Frame + " + repr(offset) if source_motion else repr(frame + offset)
        x = str(-1 + 2 * piece_index) + (" + .3 * (@Frame - " + repr(frame) + ")" if placement else "")
        common = f"int p{index}=addpoint(0,set({x},{1 if index == 2 else 0},0));"
        target_common = instance_common = common
        if not args.no_point_id:
            identifier = json.dumps("point_" + str(index)) if args.string_point_id else str(index)
            target_common += f'setpointattrib(0,"id",p{index},{identifier});'
            instance_common += f'setpointattrib(0,"id",p{index},{index});'
        points.append(target_common + f'setpointattrib(0,"name",p{index},' + json.dumps(piece) + ");")
        instances.append(
            instance_common
            + f'setpointattrib(0,"instance",p{index},'
            + json.dumps(refs[piece_index])
            + ");"
            + f'setpointattrib(0,"index",p{index},float({selected}));'
        )
        parm = source.parm(f"ref_frame{piece_index + 1}")
        parm.deleteAllKeyframes()
        if source_motion:
            parm.setExpression("$FF + " + repr(offset), hou.exprLanguage.Hscript)
        else:
            parm.set(frame + offset)

    def ordered(code):
        if args.reorder_shutter:
            return (
                "if (@Frame <= " + repr(frame) + ") {" + "\n".join(code) + "} else {" + "\n".join(reversed(code)) + "}"
            )
        return "\n".join(code)

    targets.parm("snippet").set(ordered(points))
    attribute.parm("snippet").set(ordered(instances))


def activate(node):
    node.setDisplayFlag(True)
    node.setRenderFlag(True)


def counts():
    return blender_assets.cache_diagnostics()["runtime"]


def sample_keys(update):
    return {
        (prototype["source_reference"], float(prototype["source_frame"]))
        for request in update["geometry"]
        for role in ("packed_blender", "point_instancing")
        for prototype in request.get(role, {}).get("prototypes", ())
        if "source_reference" in prototype
    }


def render(session, route, label, expected=None, allowed_issues=()):
    activate(copies if route == "packed" else attribute)
    if route == "packed":
        primitives = copies.geometry().prims()
        assert len(primitives) == (3 if args.repeat_piece else 2), (copies.errors(), len(primitives))
        id_attribute = copies.geometry().findPointAttrib("id")
        if not args.no_point_id:
            assert id_attribute is not None, "Copy to Points dropped explicit target IDs"
        else:
            assert id_attribute is None
        observed = {p.intrinsicValue("cycles_blender_reference") for p in primitives}
        assert observed == set(refs), (observed, refs)
    before = counts()
    update = native_scene.capture(rop)
    if expected is not None:
        missing = expected - sample_keys(update)
        report["source_shutter_missing"] = sorted(missing)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        assert not missing, "Missing explicit Blender source shutter prototypes: " + repr(sorted(missing))
    frame = session.wait(session.apply(update), timeout=120)
    status = session.status()
    assert not status["error"], status
    assert status["render_device_ids"] == [args.device], status
    issues = [
        issue
        for issue in status["source_diagnostics"]
        if issue.get("severity") in ("warning", "error")
        or any(value in issue["code"] for value in ("unavailable", "unprepared", "membership", "motion", "ambiguous"))
    ]
    assert not [issue for issue in issues if issue["code"] not in allowed_issues], issues
    image = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(64, 64, 4).copy()
    assert np.isfinite(image).all()
    red = (image[:, :, 0] > 0.2) & (image[:, :, 0] > image[:, :, 2] * 4)
    blue = (image[:, :, 2] > 0.2) & (image[:, :, 2] > image[:, :, 0] * 4)
    assert red[:, :32].sum() > 20 and blue[:, 32:].sum() > 20
    assert red[:, 32:].sum() == 0 and blue[:, :32].sum() == 0, "Named pieces were copied to the wrong target"
    after = counts()
    delta = {
        key: after[key] - before[key] for key in ("evaluation_batches", "evaluated_prototypes", "native_conversions")
    }
    row = dict(
        label=label,
        route=route,
        delta=delta,
        renderer_pid=status["renderer_pid"],
        red_pixels=int(red.sum()),
        blue_pixels=int(blue.sum()),
        source_samples=sorted(sample_keys(update)),
        source_diagnostics=status["source_diagnostics"],
    )
    report["renders"].append(row)
    session.write_exr(args.out / (label + "-" + route + ".exr"))
    return image, row


if args.source_shutter_probe:
    author(3.25, source_motion=True)
    rop.parm("allowmotionblur").set(1)
    activate(attribute)
    times = native_scene.motion_times(native_scene.motion_configuration(rop, hou.time()), hou.time())
    expected = {
        (reference, hou.timeToFrame(time) + index * 0.5) for index, reference in enumerate(refs) for time in times
    }
    update = native_scene.capture(rop)
    observed = sample_keys(update)
    missing = expected - observed
    report["probe_diagnostics"] = update.get("source_diagnostics", [])
    report["probe_geometry"] = [
        {"keys": list(request), "path": request.get("sop_path")} for request in update["geometry"]
    ]
    report["probe_points"] = attribute.geometry().pointStringAttribValues("instance")
    report.update(
        result="failed" if missing else "passed",
        source_shutter_missing=sorted(missing),
        source_shutter_expected=sorted(expected),
        source_shutter_observed=sorted(observed),
    )
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    assert not missing, "Missing explicit Blender source shutter prototypes: " + repr(sorted(missing))
    print(json.dumps(report), flush=True)
    raise SystemExit(0)

try:
    with ch.Session(
        device=args.device,
        width=64,
        height=64,
        samples=8,
        execution="worker",
        interactive=False,
        threads=2,
        denoising={"enabled": False},
    ) as session:
        previous_image = None
        frozen_hulls = None
        for frame in (2.25, 5.75):
            before_author = counts()
            author(frame)
            if args.hull_preview:
                assert source.evalParm("shape_frame") == 1 and source.evalParm("viewport_wireframe") == 1
                if frozen_hulls is None:
                    hulls = refresh_hull(source)
                    frozen_hulls = hulls
                else:
                    assert counts()["evaluated_prototypes"] == before_author["evaluated_prototypes"], (
                        "Source Frame-only edits evaluated the frozen Shape Frame preview"
                    )
                    source.cook(force=True)
                    hulls = [
                        json.loads(p.intrinsicValue("cycles_blender_viewport_hull"))["pieces"]
                        for p in source.geometry().prims()
                    ]
                    assert hulls == frozen_hulls, "Source Frame changed the frozen viewport hull"
                assert len(hulls) == 2 and all(len(pieces) == 1 for pieces in hulls)
            packed, cold = render(session, "packed", f"frame-{frame:g}")
            assert set(map(tuple, cold["source_samples"])) == {
                (reference, frame + index * 0.5) for index, reference in enumerate(refs)
            }, cold["source_samples"]
            if previous_image is not None:
                difference = float(np.abs(packed - previous_image).sum())
                assert difference > 0.1, "Source animation did not change between fractional frames"
                report["animation_difference"] = difference
            previous_image = packed
            warm, repeated = render(session, "packed", f"frame-{frame:g}-warm")
            assert repeated["delta"]["evaluated_prototypes"] == repeated["delta"]["native_conversions"] == 0
            assert np.max(np.abs(warm - packed)) < 1e-5
            direct, shared = render(session, "attribute", f"frame-{frame:g}")
            error = float(np.sqrt(np.mean((direct - packed) ** 2)))
            assert error < 0.008, ("Attribute/packed named-piece mismatch", error)
            assert shared["delta"]["evaluated_prototypes"] == shared["delta"]["native_conversions"] == 0
            if args.hull_preview:
                source.parm("viewport_wireframe").set(0)
                shaded, shaded_row = render(session, "packed", f"frame-{frame:g}-shaded-preview")
                assert np.max(np.abs(shaded - packed)) < 1e-5, "Wireframe altered render geometry"
                assert shaded_row["delta"]["evaluated_prototypes"] == 0
                source.parm("viewport_wireframe").set(1)
                source.parm("viewport_shape").set("bounds")
                boxed, boxed_row = render(session, "packed", f"frame-{frame:g}-box-preview")
                assert np.max(np.abs(boxed - packed)) < 1e-5, "Viewport hull altered render geometry"
                assert boxed_row["delta"]["evaluated_prototypes"] == 0
                source.parm("viewport_shape").set("hull")
            report["checks"].append(dict(frame=frame, attribute_packed_rmse=error))
        if not args.static_only:
            author(3.25, placement=True)
            rop.parm("allowmotionblur").set(1)
            packed, _ = render(session, "packed", "placement-motion")
            direct, _ = render(session, "attribute", "placement-motion")
            error = float(np.sqrt(np.mean((direct - packed) ** 2)))
            assert error < 0.008, ("Houdini placement shutter mismatch", error)
            report["checks"].append(dict(placement_motion_attribute_packed_rmse=error))
            if args.source_shutter:
                author(3.25, source_motion=True)
                times = native_scene.motion_times(native_scene.motion_configuration(rop, hou.time()), hou.time())
                expected = {
                    (reference, hou.timeToFrame(time) + index * 0.5)
                    for index, reference in enumerate(refs)
                    for time in times
                }
                packed, _ = render(session, "packed", "source-motion", expected)
                direct, _ = render(session, "attribute", "source-motion", expected)
                error = float(np.sqrt(np.mean((direct - packed) ** 2)))
                assert error < 0.008, ("Animated source shutter route mismatch", error)
                rop.parm("allowmotionblur").set(0)
                nominal, _ = render(session, "packed", "source-motion-disabled")
                difference = float(np.abs(packed - nominal).sum())
                assert difference > 0.1, "Animated source motion blur froze the center source geometry"
                report["checks"].append(
                    dict(
                        source_shutter_attribute_packed_rmse=error,
                        source_motion_difference=difference,
                        required_samples=sorted(expected),
                    )
                )
                # The requested pose is still required when an offset shutter's
                # midpoint does not coincide with the nominal source frame.
                rop.setParms(dict(allowmotionblur=1, shutteroffset=0.75))
                times = native_scene.motion_times(native_scene.motion_configuration(rop, hou.time()), hou.time())
                expected = {
                    (reference, hou.timeToFrame(time) + index * 0.5)
                    for index, reference in enumerate(refs)
                    for time in [hou.time(), *times]
                }
                offset_packed, _ = render(session, "packed", "offset-source-motion", expected)
                offset_direct, _ = render(session, "attribute", "offset-source-motion", expected)
                offset_error = float(np.sqrt(np.mean((offset_direct - offset_packed) ** 2)))
                assert offset_error < 0.008, ("Offset source shutter route mismatch", offset_error)
                report["checks"].append(
                    dict(
                        offset_shutter_attribute_packed_rmse=offset_error,
                        nominal_source_frame=3.25,
                        required_samples=sorted(expected),
                    )
                )
                # A changing control topology must visibly fall back with an exact
                # correspondence diagnostic, never silently fabricate motion.
                rop.parm("shutteroffset").set(0)
                source.setParms(dict(file=topology["file"], ref_target1=topology["name"]))
                refs[0] = topology["file"] + "#object=" + topology["name"]
                author(3.25, source_motion=True)
                times = native_scene.motion_times(native_scene.motion_configuration(rop, hou.time()), hou.time())
                expected = {
                    (reference, hou.timeToFrame(time) + index * 0.5)
                    for index, reference in enumerate(refs)
                    for time in times
                }
                _, changed = render(
                    session,
                    "packed",
                    "topology-change",
                    expected,
                    allowed_issues=("motion.external_mesh_correspondence",),
                )
                assert any(
                    issue["code"] == "motion.external_mesh_correspondence" for issue in changed["source_diagnostics"]
                ), changed
                report["checks"].append(dict(topology_correspondence_fallback=True))
        pids = {row["renderer_pid"] for row in report["renders"]}
        assert len(pids) == 1, "Persistent final renderer restarted between captures"
        report["persistent_renderer_pid"] = pids.pop()
        hou.hipFile.save(str(args.out / "named-pieces.hip"))
        report["result"] = "passed"
finally:
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(
    json.dumps(
        dict(
            result=report["result"],
            device=report["device"],
            checks=len(report["checks"]),
            renders=len(report["renders"]),
            renderer_pid=report.get("persistent_renderer_pid"),
            report=str(report_path),
        )
    ),
    flush=True,
)
