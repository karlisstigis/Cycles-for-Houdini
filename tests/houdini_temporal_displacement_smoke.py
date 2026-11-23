# SPDX-License-Identifier: Apache-2.0
"""Owned animated displacement against independently positioned motion meshes."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
from subdivision_test_setup import configure_render, set_subdivision
import argparse, json, os, subprocess
from copy import deepcopy
from functools import partial
from pathlib import Path
import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene, registry
from temporal_displacement_fixture import Fixture, CASES

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
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
        "--output",
        str(references),
    ],
    capture_output=True,
    text=True,
    timeout=180,
)
assert run.returncode == 0, (run.stdout, run.stderr)
entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
checks = []
try:
    print("temporal: image comparisons", flush=True)
    for case in CASES:
        f.configure(case)
        for steps in (3, 5):
            f.rop.parm("motion_samples").set(steps)
            baseline = None
            for broken in (False, True, False):
                f.edit(broken)
                frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
                status = entry.session.status()
                planes = f.check(frame, status, broken, baseline)
                if baseline is None:
                    baseline = planes
                if not broken:
                    reader = oiio.ImageInput.open(str(references / f"position-{steps}.exr"))
                    assert reader
                    spec = reader.spec()
                    pixels = np.asarray(reader.read_image(), np.float32)
                    reader.close()
                    indices = [list(spec.channelnames).index(c) for c in ("R", "G", "B")]
                    expected = np.flipud(pixels[:, :, indices])
                    actual = planes["combined"][:, :, :3]
                    error = np.abs(expected - actual)
                    assert (
                        error.mean() < 0.0008
                        and abs(float(expected.sum() - actual.sum())) < float(expected.sum()) * 0.01
                    ), (case, steps, error.mean(), error.max())
                else:
                    assert np.mean(np.abs(planes["combined"] - baseline["combined"])) > 0.0001, (
                        case,
                        "Motion was not actually toggled/recovered",
                    )
            checks.append(
                dict(
                    case=case, samples=steps, mean_difference=float(error.mean()), maximum_difference=float(error.max())
                )
            )
            conversions = status["geometry_conversions"]
            camera = hou.node(f.rop.evalParm("camera"))
            camera.parm("tx").set(0.05)
            entry.session.wait(entry.apply(dict(camera_path=camera.path()), hou.time()))
            assert entry.session.status()["geometry_conversions"] == conversions
            camera.parm("tx").set(0)
    print("temporal: offset shutters", flush=True)
    # Offset shutters use their own absolute graph times. Recenter the camera
    # by the independently specified 1.5 * shutter-centre displacement, making
    # the same reference image applicable without replaying converted geometry.
    f.rop.parm("motion_samples").set(3)
    for case in CASES:
        f.configure(case)
        for offset in (-1.0, 1.0):
            f.rop.parm("shutteroffset").set(offset)
            camera.parm("tx").set(offset * 0.75)
            frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), 0))
            planes = f.check(frame, entry.session.status(), False)
            reader = oiio.ImageInput.open(str(references / "position-3.exr"))
            spec = reader.spec()
            ref = np.asarray(reader.read_image(), np.float32)
            reader.close()
            ix = [list(spec.channelnames).index(c) for c in ("R", "G", "B")]
            difference = float(np.mean(np.abs(planes["combined"][:, :, :3] - np.flipud(ref[:, :, ix]))))
            assert difference < 0.0008, (case, offset, difference)
            checks.append(dict(case=case, offset=offset, samples=3, mean_difference=difference))
    f.rop.parm("shutteroffset").set(0)
    camera.parm("tx").set(0)
    print("temporal: off-frame texture edits", flush=True)
    # Changing a valid off-frame texture leaves the nominal graph unchanged.
    # Its displacement must rebuild from retained source, without accumulation.
    f.configure("image")
    f.rop.parm("motion_samples").set(3)
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), 0))
    original = f.check(frame, entry.session.status(), False)
    conversions = entry.session.status()["geometry_conversions"]
    for value in (0.9, -0.25):
        output = oiio.ImageOutput.create(str(f.file))
        assert output.open(str(f.file), oiio.ImageSpec(2, 2, 3, oiio.FLOAT))
        assert output.write_image(np.full((2, 2, 3), value, np.float32))
        assert output.close()
        frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), 0))
        planes = f.check(frame, entry.session.status(), False)
        assert entry.session.status()["geometry_conversions"] == conversions
        difference = float(np.mean(np.abs(planes["combined"] - original["combined"])))
        assert difference > 0.0001 if value == 0.9 else difference < 1e-6, (value, difference)
    print("temporal: independent surface color", flush=True)
    f.configure("parameter")
    color_file = f.dest / "surface-color.exr"

    def write_color(value):
        output = oiio.ImageOutput.create(str(color_file))
        assert output.open(str(color_file), oiio.ImageSpec(2, 2, 3, oiio.FLOAT))
        assert output.write_image(np.broadcast_to(np.asarray(value, np.float32), (2, 2, 3)).copy())
        assert output.close()

    write_color((0.2, 0.4, 0.8))
    color_image = f.material.createNode("cycles::image_texture::1", "independent_surface_color")
    color_image.setParms(dict(filename=str(color_file), colorspace="data"))
    color_image.setNamedInput("vector", f.geometry, "position")
    f.source.setNamedInput("color", color_image, "color")
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), 0))
    original = f.check(frame, entry.session.status(), False)
    rebuilds = entry.session.status()["displacement_rebuilds"]
    conversions = entry.session.status()["geometry_conversions"]
    for filename in (str(f.dest / "missing-surface-color.exr"), str(color_file)):
        color_image.parm("filename").set(filename)
        frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), 0))
        status = entry.session.status()
        issues = status["source_diagnostics"]
        assert bool(issues) == ("missing-" in filename) and all(d["code"] == "texture.unavailable" for d in issues), (
            issues
        )
        assert len(issues) <= 1, issues
        p = frame["planes"]["position"]
        position = np.frombuffer(p["pixels"], np.float32).reshape(90, 160, 3)
        assert np.allclose(position, original["position"], atol=1e-5)
        assert status["displacement_rebuilds"] == rebuilds and status["geometry_conversions"] == conversions, status
    write_color((0.8, 0.4, 0.2))
    entry.session.wait(entry.apply(native_scene.capture(f.rop), 0))
    status = entry.session.status()
    assert (
        not status["source_diagnostics"]
        and status["displacement_rebuilds"] == rebuilds
        and status["geometry_conversions"] == conversions
    ), status
    f.source.setInput(f.source.inputIndex("color"), None)
    color_image.destroy()
    print("temporal: subdivision", flush=True)
    # Generated subdivision vertices receive the same temporal shader states.
    f.configure("parameter")
    configure_render(f.rop, dict(subdivision=dict(type="linear", space="object", dicing_rate=0.1, max_level=2)))
    for level in (2, 3, 2):
        set_subdivision(f.rop, max_level=level)
        frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), 0))
        planes = f.check(frame, entry.session.status(), False)
        assert entry.session.status()["subdivision_triangles"] > 2
        reader = oiio.ImageInput.open(str(references / "position-3.exr"))
        spec = reader.spec()
        ref = np.asarray(reader.read_image(), np.float32)
        reader.close()
        ix = [list(spec.channelnames).index(c) for c in ("R", "G", "B")]
        difference = float(np.mean(np.abs(planes["combined"][:, :, :3] - np.flipud(ref[:, :, ix]))))
        assert difference < 0.0008, (level, difference)
        normal = planes["normal"][45, 80]
        assert normal[0] > 0.95 and np.max(np.abs(normal[1:])) < 0.001, (level, normal)
    set_subdivision(f.rop, type="none")
    print("temporal: packed sharing", flush=True)
    # Packed copies retain one object-space prototype, including across repairs.
    # This fixture tests a static prototype; native prototype deformation remains
    # a separate adapter gate, so choose supported packed transform sampling.
    f.rop.parm("geometry_motion").set("transform")
    targets = f.obj.createNode("grid", "instance_targets")
    targets.setParms(dict(type=0, orient=0, rows=2, cols=2))
    targets.parmTuple("size").set((1.8, 1.2))
    copies = f.obj.createNode("copytopoints", "temporal_instances")
    copies.setInput(0, f.sop)
    copies.setInput(1, targets)
    copies.parm("pack").set(1)
    copies.setDisplayFlag(True)
    copies.setRenderFlag(True)
    for broken in (False, True, False):
        f.edit(broken)
        frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), 0))
        status = entry.session.status()
        assert (
            not status["source_diagnostics"]
            and status["geometry_resources"] == 1
            and status["object_resources"] == 4
            and status["mesh_variant_resources"] == 1
        ), status
        assert np.isfinite(np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32)).all()
    f.sop.setDisplayFlag(True)
    f.sop.setRenderFlag(True)
    copies.destroy()
    targets.destroy()
    f.rop.parm("geometry_motion").set("deformation")
    print("temporal: object opt-out", flush=True)
    # The native object opt-out disables generated displacement motion too.
    from cycles_houdini import object_properties

    object_properties.install(f.obj)
    f.configure("image")
    f.obj.parm("cycles_motion_blur").set("off")
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), 0))
    still = f.check(frame, entry.session.status(), False)
    f.edit(True)  # The missing off-frame map is unused while object blur is off.
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), 0))
    ignored = f.check(frame, entry.session.status(), False)
    assert np.allclose(ignored["combined"], still["combined"], atol=1e-6)
    f.edit(False)
    f.obj.parm("cycles_motion_blur").set("inherit")
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), 0))
    blurred = f.check(frame, entry.session.status(), False)
    assert np.mean(np.abs(blurred["combined"] - still["combined"])) > 0.0001
    print("temporal: ownership", flush=True)
    # Frozen samples must survive parameter mutation and cross the worker wire.
    f.configure("parameter")
    f.rop.parm("motion_samples").set(3)
    times = native_scene.motion_times(native_scene.motion_configuration(f.rop, 0), 0)
    resolver = partial(native_scene.resolve_materials, shutter_times=times)
    lease = ch._native.preserve_material_graphs([f.material.path()], resolver, 0)
    frozen = ch._native.convert_preserved_material_graphs(lease, [f.material.path()])
    assert len(frozen["materials"][0]["displacement_samples"]) == 3
    f.scalar.parm("height").deleteAllKeyframes()
    f.scalar.parm("height").set(42)
    assert ch._native.convert_preserved_material_graphs(lease, [f.material.path()])["wire"] == frozen["wire"]
    del lease
    f.configure("parameter")
    print("temporal: final renderer and strict counts", flush=True)
    # Strict malformed internal samples remain failures before scene mutation.
    with ch.Session(
        device=device,
        width=160,
        height=90,
        samples=1024,
        execution="sync",
        interactive=False,
        passes=["combined", "position", "normal", "depth"],
    ) as final:
        final.wait(final.apply(native_scene.capture(f.rop), at_time=0))
        generation = final.status()["generation"]
        original = final.frame_info()
        for count in (1, 2, 4):
            bad = deepcopy(frozen["materials"][0])
            bad["key"] = "invalid-shutter-test"
            bad["displacement_samples"] = (bad["displacement_samples"] * 2)[:count]
            try:
                final.apply(dict(materials=[bad]))
            except RuntimeError as error:
                assert "odd 3..129" in str(error), str(error)
            else:
                raise AssertionError("Malformed shutter count accepted")
            assert final.status()["generation"] == generation and final.frame_info() == original
        for case in CASES:
            print("temporal: final case " + case, flush=True)
            f.configure(case)
            frame = final.wait(final.apply(native_scene.capture(f.rop), at_time=0))
            f.check(frame, final.status(), False)
    print("temporal: retirement", flush=True)
    # Repeated animation toggles retire/reuse auxiliary shaders without a
    # continuing high-water increase. These are tracked bytes, not physical VRAM.
    f.configure("parameter")
    allocations = []
    resources = []
    for n in range(16):
        f.edit(bool(n % 2))
        entry.session.wait(entry.apply(native_scene.capture(f.rop), 0))
        status = entry.session.status()
        allocations.append(status["device_allocation_bytes"])
        resources.append(status["engine_shader_resources"])
    assert len(set(resources[6:])) <= 2, resources
    for parity in (0, 1):
        assert len(set(allocations[8 + parity :: 2])) == 1, allocations
    f.edit(False)
    print("temporal: batch", flush=True)
    # Five real Houdini/SOHO frames retain animated displacement and write every
    # image. Bounded periodic input keeps the entire sequence in this camera.
    f.scalar.parm("height").setExpression(".5+.5*sin($T*72)", hou.exprLanguage.Hscript)
    f.rop.parm("picture").set(str(f.dest / "temporal.$F4.exr"))
    f.rop.render(frame_range=(1, 5, 1))
    for n in range(1, 6):
        reader = oiio.ImageInput.open(str(f.dest / f"temporal.{n:04d}.exr"))
        assert reader
        assert not json.loads(reader.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        reader.close()
    hou.setFrame(1)
    f.configure("parameter")
    hou.hipFile.save(str(f.dest / "temporal-displacement.hiplc"))
finally:
    f.edit(False)
    registry.close_all()
report = dict(
    result="passed",
    device=device,
    checks=checks,
    owned_samples=True,
    malformed_counts=3,
    final_states=len(CASES),
    native_batch_frames=5,
    subdivision=True,
    packed_sharing=True,
    object_motion_opt_out=True,
    independent_color_inputs=True,
    warm_shader_resources=resources,
    warm_tracked_bytes=allocations,
)
path = artifacts_root() / "stage2/displacement/houdini-temporal-displacement-smoke.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(report, indent=2))
print(json.dumps(report))
