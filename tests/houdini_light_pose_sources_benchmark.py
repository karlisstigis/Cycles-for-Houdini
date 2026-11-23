# SPDX-License-Identifier: Apache-2.0
"""Real 128-light OBJ rig: observer identities, bounded evaluation, fresh images."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import time
import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import device_selection, light_properties, native_scene, registry

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
p.add_argument("--lights", type=int, default=128)
p.add_argument("--report", type=Path, required=True)
p.add_argument("--expect-filtered", action="store_true")
p.add_argument("--probe", action="store_true")
args = p.parse_args()
args.report.parent.mkdir(parents=True, exist_ok=True)
assert args.lights >= 18, "The hierarchy fixture needs at least 18 lights"
geo = hou.node("/obj").createNode("geo", "source_receiver", run_init_scripts=False)
grid = geo.createNode("grid")
grid.setParms(dict(orient=0, rows=2, cols=2, sizex=6, sizey=6))
camera = hou.node("/obj").createNode("cam", "source_camera")
camera.setParms(dict(tz=7, projection=1, orthowidth=7))
parent = hou.node("/obj").createNode("null", "source_parent")
container = hou.node("/obj").createNode("subnet", "source_container")
lights = []
for i in range(args.lights):
    network = container if i == args.lights - 2 else hou.node("/obj")
    node = network.createNode(light_properties.LIGHT_TYPE, "source_light_" + str(i))
    if i < 16:
        node.setInput(0, parent)
    # Distinct projected surfaces: repeating positions makes coincident sphere
    # tie-breaking depend on renderer insertion order and hides strong emitters.
    columns = 16
    rows = (args.lights + columns - 1) // columns
    node.setParms(
        dict(
            tx=(i % columns - (columns - 1) / 2) * 0.35,
            ty=(i // columns - (rows - 1) / 2) * 0.35,
            tz=3,
            cycles_light_type="point",
            light_intensity=6 if i in (0, 16, args.lights - 1) else 0.02,
        )
    )
    lights.append(node)
rop = hou.node("/out").createNode("cycles_ipr", "source_pose_test")
device_selection.set_devices(rop, [args.device])
rop.setParms(
    dict(
        source_mode="objects",
        vobject=geo.path(),
        lights=" ".join(n.path() for n in lights),
        camera=camera.path(),
        resx=96,
        resy=72,
        samples=16,
        ipr_samples=16,
        resolution_percent=100,
        ipr_resolution_percent=100,
        use_material_assignments=0,
        material_kind="diffuse",
        use_denoise=0,
        ipr_use_denoise=0,
        allowmotionblur=0,
    )
)
rop.parmTuple("background").set((0, 0, 0))
runs = []


def pixels(frame):
    return np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).copy()


def preview_frame(session, generation):
    began = time.monotonic()
    while time.monotonic() - began < 30:
        status = session.status()
        frame = session.frame()
        assert status["state"] != "failed", status
        if frame and frame["complete"] and frame["generation"] == generation and frame.get("current_scene"):
            return frame
        time.sleep(0.005)
    raise TimeoutError(status)


with ch.Session(device=args.device, width=96, height=72, samples=16, interactive=False, execution="worker") as session:
    entry = registry.Entry("source-pose-benchmark", native_scene.configuration(rop), session, rop=rop)
    session.wait(entry.apply(native_scene.capture(rop), hou.time()))
    identity_callbacks = []

    def changed(node):
        identity_callbacks.append(node.sessionId())
        entry.changed(object_pose=True, source_node=node)

    observer = native_scene.SceneObserver(rop, entry.changed, object_pose_source_callback=changed)
    entry.observer = observer
    pid = session.status()["renderer_pid"]
    conversions = session.status()["geometry_conversions"]
    if args.probe:
        probes = []
        errors = []

        def exr_witness(renderer, path, expected):
            renderer.write_exr(
                path, dict(compression="zip", half_color=False, passes=[dict(source="combined", layer="")])
            )
            image = oiio.ImageInput.open(str(path))
            assert image, oiio.geterror()
            try:
                spec = image.spec()
                values = image.read_image(format=oiio.FLOAT)
                indices = [spec.channelnames.index(c) for c in "RGBA"]
                values = np.ascontiguousarray(values[:, :, indices][::-1]).reshape(-1)
                return dict(
                    bit_identical=np.array_equal(values.view(np.uint32), expected.view(np.uint32)),
                    mean_error=float(np.abs(values - expected).mean()),
                    rgb_max=float(values.reshape(-1, 4)[:, :3].max()),
                )
            finally:
                image.close()

        for radius, camera_visible in ((0.1, True), (0.1, False), (0, True)):
            case = f"radius-{radius}-camera-{int(camera_visible)}"
            for light in lights:
                light.parm("cycles_light_radius").set(radius)
                light.parm("cycles_light_visibility_camera").set(camera_visible)
            request = native_scene.capture(rop)
            retained = pixels(session.wait(entry.apply(deepcopy(request), hou.time())))
            retained_exr = exr_witness(session, args.report.parent / f"probe-{case}-retained.exr", retained)
            summaries = dict(
                count=len(request["lights"]),
                unique_centers=len({tuple(light["transform"][i] for i in (3, 7, 11)) for light in request["lights"]}),
                radii=sorted({light["radius"] for light in request["lights"]}),
                visibility=sorted({light["visibility"] for light in request["lights"]}),
            )
            assert summaries["unique_centers"] == args.lights, summaries
            for mode in ("sync", "worker"):
                with ch.Session(
                    device=args.device, width=96, height=72, samples=16, interactive=False, execution=mode
                ) as fresh:
                    frame = fresh.wait(fresh.apply(deepcopy(request), at_time=hou.time()))
                    reference = pixels(frame)
                    fresh_exr = exr_witness(fresh, args.report.parent / f"probe-{case}-{mode}.exr", reference)
                    error = float(np.abs(retained - reference).mean())
                    result = dict(
                        radius=radius,
                        camera_visible=camera_visible,
                        mode=mode,
                        error=error,
                        retained_max=float(retained.reshape(-1, 4)[:, :3].max()),
                        fresh_max=float(reference.reshape(-1, 4)[:, :3].max()),
                        fresh_lights=fresh.status()["light_resources"],
                        retained_lights=session.status()["light_resources"],
                        alpha_error=float(np.abs(retained[3::4] - reference[3::4]).mean()),
                        lights=summaries,
                        retained_exr=retained_exr,
                        fresh_exr=fresh_exr,
                    )
                    probes.append(result)
                    valid = (
                        error < 0.001
                        and result["alpha_error"] == 0
                        and retained_exr["bit_identical"]
                        and fresh_exr["bit_identical"]
                        and result["fresh_lights"] == result["retained_lights"] == args.lights
                    )
                    visible = radius > 0 and camera_visible
                    valid &= all(
                        (value > 1 if visible else value < 1) for value in (result["retained_max"], result["fresh_max"])
                    )
                    if not valid:
                        errors.append(dict(case=case, mode=mode, result=result))
                        np.savez_compressed(
                            args.report.parent / (f"probe-{case}-{mode}.npz"), retained=retained, reference=reference
                        )
        observer.close()
        entry.observer = None
        args.report.write_text(
            json.dumps(
                dict(
                    result="failed" if errors else "passed",
                    scope="Noncoincident finite point emitters; retained/fresh worker and synchronous parity; camera visibility on/off and punctual controls; decoded EXR/frame bits",
                    probes=probes,
                    errors=errors,
                ),
                indent=2,
            )
        )
        print(args.report)
        assert not errors, errors
        raise SystemExit(0)
    original_matrix = native_scene.matrix_from_houdini

    def measured_preview():
        evaluations = []

        def matrix(value):
            evaluations.append(1)
            return original_matrix(value)

        native_scene.matrix_from_houdini = matrix
        began = time.perf_counter()
        try:
            generation = entry.preview_object_poses()
        finally:
            elapsed = time.perf_counter() - began
            native_scene.matrix_from_houdini = original_matrix
        assert generation is not None, "No pose preview from actual observer event"
        return generation, len(evaluations), elapsed

    def compare(name, generation, count, elapsed, expected):
        preview_info = preview_frame(session, generation)
        preview = pixels(preview_info)
        assert session.status()["renderer_pid"] == pid
        assert session.status()["geometry_conversions"] == conversions
        authoritative = pixels(session.wait(entry.apply(native_scene.capture(rop), hou.time())))
        # Match the persistent IPR execution path; the separate --probe mode
        # additionally compares synchronous and worker finite-emitter output.
        with ch.Session(
            device=args.device, width=96, height=72, samples=16, interactive=False, execution="worker"
        ) as fresh:
            reference_info = fresh.wait(fresh.apply(native_scene.capture(rop), at_time=hou.time()))
            reference = pixels(reference_info)
        error = float(np.abs(preview - reference).mean())
        diagnostic = dict(
            case=name,
            preview_fresh_mean_error=error,
            preview_authoritative_mean_error=float(np.abs(preview - authoritative).mean()),
            authoritative_fresh_mean_error=float(np.abs(authoritative - reference).mean()),
            preview_alpha_fresh_error=float(np.abs(preview[3::4] - reference[3::4]).mean()),
            preview_mean=float(preview.mean()),
            reference_mean=float(reference.mean()),
            preview_samples=preview_info.get("samples"),
            reference_samples=reference_info.get("samples"),
            evaluations=count,
            expected=expected,
        )
        if error >= 0.001:
            np.savez_compressed(
                args.report.parent / (name + "-difference.npz"),
                preview=preview,
                authoritative=authoritative,
                reference=reference,
            )
            (args.report.parent / "discrepancy.json").write_text(json.dumps(diagnostic, indent=2))
        assert error < 0.001, diagnostic
        if args.expect_filtered:
            assert count == expected, (name, count, expected)
        runs.append(
            dict(
                case=name,
                light_transform_evaluations=count,
                expected_filtered=expected,
                host_preview_seconds=elapsed,
                fresh_mean_error=error,
                source_callbacks=len(identity_callbacks),
            )
        )
        identity_callbacks.clear()

    try:
        for name, node, parameter, value, expected in (
            ("unrelated_mesh", geo, "tx", 0.15, 0),
            ("one_light", lights[16], "tx", 1.1, 1),
            ("moved_parent", parent, "ry", 12, 16),
            ("moved_subnet_container", container, "tx", 0.2, 1),
        ):
            node.parm(parameter).set(value)
            assert node.sessionId() in identity_callbacks, (name, identity_callbacks)
            compare(name, *measured_preview(), expected)
        # Coalesced sources and a later preview preserve prior light poses.
        lights[0].parm("tx").set(0.8)
        generation, count, elapsed = measured_preview()
        preview_frame(session, generation)
        lights[16].parm("ty").set(0.7)
        generation, count, elapsed = measured_preview()
        compare("successive_different_sources", generation, count, elapsed, 1)
        lights[-1].parm("tx").setExpression('ch("../source_receiver/tx")')
        session.wait(entry.apply(native_scene.capture(rop), hou.time()))
        identity_callbacks.clear()
        geo.parm("tx").set(0.3)
        compare("expression_dependency", *measured_preview(), 1)
        # A missing changed-node identity retains complete fallback behavior.
        lights[16].parm("tx").set(0.5)
        entry.changed(object_pose=True)
        compare("unknown_source", *measured_preview(), args.lights)
        lights[-1].parm("tx").deleteAllKeyframes()
        lights[16].setInput(0, parent)
        assert not entry.defer_source_capture(), "Reparenting deferred its authoritative dependency refresh"
        session.wait(entry.apply(native_scene.capture(rop), hou.time()))
        identity_callbacks.clear()
        parent.parm("rx").set(5)
        compare("reparented_parent", *measured_preview(), 17)
        lights[0].parm("tx").set(0.9)
        entry.changed()
        assert not entry.defer_source_capture(), "Source edit was hidden by transform events"
    finally:
        observer.close()
        entry.observer = None
args.report.write_text(
    json.dumps(
        dict(
            result="passed",
            lights=args.lights,
            runs=runs,
            scope="Real OBJ notifications; unrelated mesh, one light, parent hierarchy, cumulative overrides, expressions, unknown-source fallback; fresh render comparisons",
        ),
        indent=2,
    )
)
print(args.report)
