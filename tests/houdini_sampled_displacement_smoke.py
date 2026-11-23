# SPDX-License-Identifier: Apache-2.0
"""Actual per-shutter displacement against independently positioned references."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
from subdivision_test_setup import configure_render, set_subdivision
import argparse
import json
import os
import subprocess
from pathlib import Path
import hou
import numpy as np
import OpenImageIO as oiio
from cycles_houdini import native_scene, registry
from sampled_displacement_fixture import Fixture, CASES

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.add_argument("--subdivision-normals-only", action="store_true")
parser.add_argument("--report", type=Path)
args = parser.parse_args()
device = args.device
f = Fixture(device)
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
references = f.dest / ("reference-cpu" if device == "CPU" else "reference-optix")
run = subprocess.run(
    [
        str(runtime_executable("ch_harness.exe")),
        "--resources",
        os.environ["CYCLES_HOUDINI_RESOURCES"],
        "--device",
        device,
        "--displacement-motion-reference",
        *(["--subdivision-normals-only"] if args.subdivision_normals_only else []),
        "--output",
        str(references),
    ],
    capture_output=True,
    text=True,
    timeout=180,
)
assert run.returncode == 0, (run.stdout, run.stderr)
reference_count = json.loads(run.stdout.strip().splitlines()[-1])["independent_motion_references"]
assert reference_count == (2 if args.subdivision_normals_only else 16), reference_count
entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
checks = []
subdivision_normals = []


def check_subdivision_normals():
    # Changing authored corner normals under linear subdivision must use the
    # diced triangle count for each normal sample, including after retessellation.
    for steps in (3, 5):
        f.configure("authored_normal")
        f.edit(True)
        configure_render(f.rop, dict(motion_samples=steps, subdivision=dict(type="linear", max_level=3)))
        frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
        status = entry.session.status()
        assert status["subdivision_triangles"] > 2 and status["render_device_ids"] == [device], status
        planes = f.check(frame, status, True)
        reader = oiio.ImageInput.open(str(references / ("authored_normal-" + str(steps) + ".exr")))
        spec = reader.spec()
        expected = np.asarray(reader.read_image(), np.float32)
        reader.close()
        indices = [list(spec.channelnames).index(channel) for channel in ("R", "G", "B")]
        delta = np.abs(planes["combined"][:, :, :3] - np.flipud(expected[:, :, indices]))
        entry.session.write_exr(f.dest / ("authored_normal-linear-" + str(steps) + "-actual.exr"))
        assert float(delta.mean()) < 0.0008, (
            "authored_normal",
            "linear",
            steps,
            float(delta.mean()),
            float(delta.max()),
        )
        subdivision_normals.append(
            dict(
                samples=steps,
                triangles=status["subdivision_triangles"],
                mean_difference=float(delta.mean()),
                maximum_difference=float(delta.max()),
            )
        )


def check_full():
    for case in CASES:
        f.configure(case)
        for steps in (3, 5):
            f.rop.parm("motion_samples").set(steps)
            baseline = None
            for enabled in (False, True, False):
                f.edit(enabled)
                frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
                status = entry.session.status()
                planes = f.check(frame, status, enabled, baseline)
                if baseline is None:
                    baseline = planes
                if enabled:
                    entry.session.write_exr(f.dest / (case + "-" + str(steps) + "-actual.exr"))
                    reader = oiio.ImageInput.open(str(references / (case + "-" + str(steps) + ".exr")))
                    assert reader
                    spec = reader.spec()
                    reference = np.asarray(reader.read_image(), np.float32)
                    reader.close()
                    indices = [list(spec.channelnames).index(channel) for channel in ("R", "G", "B")]
                    expected = np.flipud(reference[:, :, indices])
                    actual = planes["combined"][:, :, :3]
                    delta = np.abs(actual - expected)
                    assert (
                        float(delta.mean()) < 0.0008
                        and abs(float(actual.sum() - expected.sum())) < float(expected.sum()) * 0.01
                    ), (case, steps, float(delta.mean()), float(delta.max()))
                    z = list(spec.channelnames).index("position.Z")
                    expected_z = np.flipud(reference[:, :, z])
                    depth_actual = planes["position"][30:60, 73:87, 2]
                    depth_reference = expected_z[30:60, 73:87]
                    depth_error = abs(float(depth_actual.mean() - depth_reference.mean()))
                    bound = 0.0005 + 5 * float(
                        np.sqrt((depth_actual.var() + depth_reference.var()) / depth_actual.size)
                    )
                    assert depth_error < bound, (
                        case,
                        steps,
                        "shutter displacement depth",
                        depth_error,
                        bound,
                        float(depth_actual.mean()),
                        float(depth_reference.mean()),
                    )
                    checks.append(
                        dict(
                            case=case,
                            samples=steps,
                            mean_difference=float(delta.mean()),
                            maximum_difference=float(delta.max()),
                            interior_depth_difference=depth_error,
                            interior_depth_tolerance=bound,
                        )
                    )
            conversions = entry.session.status()["geometry_conversions"]
            camera = hou.node(f.rop.evalParm("camera"))
            camera.parm("tx").set(0.1)
            entry.session.wait(entry.apply(dict(camera_path=camera.path()), hou.time()))
            assert entry.session.status()["geometry_conversions"] == conversions
            camera.parm("tx").set(0)
    # Host animation is not silently treated as a frozen displacement graph.
    f.configure("normal")
    f.edit(True)
    f.scalar.parm("height").setExpression(".5+$T", hou.exprLanguage.Hscript)
    graph = __import__("cycles_houdini").material_graph(f.material)
    assert graph["displacement_time_dependent"]
    entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
    assert not entry.session.status()["source_diagnostics"]
    f.scalar.parm("height").deleteAllKeyframes()
    f.scalar.parm("height").set(0.5)
    entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
    assert not entry.session.status()["source_diagnostics"]
    f.source.parm("strength").setExpression("1+$T", hou.exprLanguage.Hscript)
    assert not __import__("cycles_houdini").material_graph(f.material)["displacement_time_dependent"]
    f.source.parm("strength").deleteAllKeyframes()
    f.source.parm("strength").set(1)
    # The tessellated mesh must keep corresponding shutter poses too.
    f.configure("normal")
    f.edit(True)
    f.rop.parm("motion_samples").set(3)
    configure_render(f.rop, dict(subdivision=dict(type="catmull_clark", space="object", dicing_rate=0.1, max_level=2)))
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
    planes = f.check(frame, entry.session.status(), True)
    assert entry.session.status()["subdivision_triangles"] > 2, entry.session.status()
    reader = oiio.ImageInput.open(str(references / "normal-3.exr"))
    spec = reader.spec()
    expected = np.asarray(reader.read_image(), np.float32)
    reader.close()
    expected = np.flipud(expected[:, :, :3])
    assert np.mean(np.abs(planes["combined"][:, :, :3] - expected)) < 0.0008
    check_subdivision_normals()
    set_subdivision(f.rop, type="none")
    # Final rendering uses the same actual sampled evaluator. Houdini's ordered
    # batch path also has to keep the feature active across five animated frames.
    import cycles_houdini as ch

    with ch.Session(
        device=device,
        width=160,
        height=90,
        samples=1024,
        execution="sync",
        interactive=False,
        passes=["combined", "position", "normal", "depth"],
    ) as final:
        for case in CASES:
            f.configure(case)
            f.edit(True)
            frame = final.wait(final.apply(native_scene.capture(f.rop), at_time=hou.time()))
            f.check(frame, final.status(), True)
    f.configure("normal")
    f.edit(True)
    f.rop.parm("picture").set(str(f.dest / "sampled.$F4.exr"))
    f.rop.render(frame_range=(1, 5, 1))
    for number in range(1, 6):
        reader = oiio.ImageInput.open(str(f.dest / f"sampled.{number:04d}.exr"))
        assert reader
        assert not json.loads(reader.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        reader.close()
    hou.setFrame(1)
    f.configure("normal")
    f.edit(True)
    hou.hipFile.save(str(f.dest / "sampled-displacement.hiplc"))


try:
    if args.subdivision_normals_only:
        check_subdivision_normals()
    else:
        check_full()
finally:
    registry.close_all()
if args.subdivision_normals_only:
    report = dict(
        result="passed",
        device=device,
        mode="subdivision_normals_only",
        references=reference_count,
        subdivision_normals=subdivision_normals,
    )
else:
    report = dict(
        result="passed",
        device=device,
        references=reference_count,
        checks=checks,
        subdivision=True,
        final_states=4,
        native_batch_frames=5,
        subdivision_normals=subdivision_normals,
    )
path = args.report or artifacts_root() / "stage2/displacement/houdini-sampled-displacement-smoke.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(report, indent=2))
print(json.dumps(report))
