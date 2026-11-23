# SPDX-License-Identifier: Apache-2.0
"""Shutter UV/Cd displacement compared with independent pre-displaced meshes."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
from subdivision_test_setup import configure_render, set_subdivision
import argparse, json, os, subprocess
from pathlib import Path
import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene, registry
from sampled_attribute_displacement_fixture import Fixture, CASES

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
f = Fixture(device)
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
refs = f.dest / ("reference-cpu" if device == "CPU" else "reference-optix")
run = subprocess.run(
    [
        str(runtime_executable("ch_harness.exe")),
        "--resources",
        os.environ["CYCLES_HOUDINI_RESOURCES"],
        "--device",
        device,
        "--displacement-motion-reference",
        "--output",
        str(refs),
    ],
    capture_output=True,
    text=True,
    timeout=180,
)
assert run.returncode == 0, (run.stdout, run.stderr)
entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
checks = []


def apply():
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
    return frame, entry.session.status()


def reference(steps, kind="position"):
    image = oiio.ImageInput.open(str(refs / f"{kind}-{steps}.exr"))
    assert image
    spec = image.spec()
    pixels = np.asarray(image.read_image(), np.float32)
    image.close()
    return np.flipud(pixels[:, :, [list(spec.channelnames).index(c) for c in ("R", "G", "B")]])


try:
    for case in CASES:
        print("sampled attributes: " + case, flush=True)
        f.configure(case)
        for steps in (3, 5):
            f.rop.parm("motion_samples").set(steps)
            baseline = None
            for broken in (0, 1, 0, 2, 0):
                f.edit(broken)
                frame, status = apply()
                planes = f.check(frame, status, broken, baseline)
                if baseline is None:
                    baseline = planes
            error = float(np.mean(np.abs(planes["combined"][:, :, :3] - reference(steps))))
            assert error < 0.0008, (case, steps, error)
            checks.append(dict(case=case, samples=steps, mean_difference=error))
        conversions = status["geometry_conversions"]
        camera = hou.node(f.rop.evalParm("camera"))
        camera.parm("tx").set(0.05)
        entry.session.wait(entry.apply(dict(camera_path=camera.path()), hou.time()))
        assert entry.session.status()["geometry_conversions"] == conversions
        camera.parm("tx").set(0)
        frame, status = apply()
        conversions = status["geometry_conversions"]
        rebuilds = status["displacement_rebuilds"]
        tx = f.obj.evalParm("tx")
        f.obj.parm("tx").set(tx + 0.05)
        frame, status = apply()
        assert status["geometry_conversions"] == conversions and status["displacement_rebuilds"] == rebuilds, (
            case,
            "Object motion rebuilt local displacement",
            status,
        )
        f.obj.parm("tx").set(tx)
    print("sampled attributes: offset shutters", flush=True)
    f.rop.parm("motion_samples").set(3)
    for case in CASES:
        f.configure(case)
        for offset in (-1.0, 1.0):
            f.rop.parm("shutteroffset").set(offset)
            camera.parm("tx").set(offset * 0.75)
            frame, status = apply()
            planes = f.check(frame, status, False)
            error = float(np.mean(np.abs(planes["combined"][:, :, :3] - reference(3))))
            assert error < 0.0008, (case, offset, error)
            checks.append(dict(case=case, offset=offset, mean_difference=error))
    f.rop.parm("shutteroffset").set(0)
    camera.parm("tx").set(0)
    print("sampled attributes: unused and independent inputs", flush=True)
    for case, unused in (("uv", "Cd"), ("color", "uv")):
        f.configure(case)
        frame, status = apply()
        baseline = f.check(frame, status, False)
        f.missing.parm("ptdel").set(unused)
        f.invalid.parm("snippet").set('v@%s=set(atof("nan"),0,0);' % unused)
        for broken in (1, 2, 0):
            f.edit(broken)
            frame, status = apply()
            assert not status["source_diagnostics"], status
            actual = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(90, 160, 4)
            assert np.allclose(actual, baseline["combined"], atol=1e-6)
    f.configure("uv")
    f.source.setNamedInput("color", f.color, "color")
    frame, status = apply()
    baseline = f.check(frame, status, False)
    f.missing.parm("ptdel").set("Cd")
    f.edit(1)
    frame, status = apply()
    assert {d["code"] for d in status["source_diagnostics"]} == {"geometry.shutter_color"}, status
    position = np.frombuffer(frame["planes"]["position"]["pixels"], np.float32).reshape(90, 160, 3)
    assert np.allclose(position, baseline["position"], atol=1e-5), (
        "Surface-only field failure changed independent displacement motion"
    )
    f.edit(0)
    f.source.setInput(f.source.inputIndex("color"), None)
    f.configure("color")
    kind = f.rop.evalParm("material_kind")
    configure_render(f.rop, dict(use_material_assignments=0, material_kind="emission", color_attribute="Cd"))
    f.edit(1)
    frame, status = apply()
    assert {d["code"] for d in status["source_diagnostics"]} == {"geometry.shutter_color"}, status
    assert np.frombuffer(frame["planes"]["depth"]["pixels"], np.float32).min() < 10, "Uniform material lost geometry"
    f.rop.parm("color_attribute").set("")
    frame, status = apply()
    assert not status["source_diagnostics"], status
    f.edit(0)
    configure_render(f.rop, dict(use_material_assignments=1, material_kind=kind))
    f.configure("uv")
    f.edit(1)
    frame, status = apply()
    f.check(frame, status, True)
    entry.session.wait(entry.apply(dict(motion_blur=False), hou.time()))
    assert not entry.session.status()["source_diagnostics"], entry.session.status()
    entry.session.wait(entry.apply(dict(motion_blur=True), hou.time()))
    assert entry.session.status()["source_diagnostics"]
    f.edit(0)
    print("sampled attributes: subdivision", flush=True)
    for subdivision in ("linear", "catmull_clark"):
        configure_render(f.rop, dict(subdivision=dict(type=subdivision, space="object", dicing_rate=0.1, max_level=3)))
        for case in ("uv", "color", "image_uv"):
            f.configure(case)
            f.rop.parm("motion_samples").set(3)
            frame, status = apply()
            planes = f.check(frame, status, False)
            error = float(np.mean(np.abs(planes["combined"][:, :, :3] - reference(3, subdivision + "_position"))))
            assert error < 0.0008, (subdivision, case, error)
            for broken in (1, 0):
                f.edit(broken)
                frame, status = apply()
                f.check(frame, status, broken, planes)
            checks.append(
                dict(
                    case=case, subdivision=subdivision, triangles=status["subdivision_triangles"], mean_difference=error
                )
            )
    set_subdivision(f.rop, type="none")
    print("sampled attributes: warm allocations", flush=True)
    f.configure("combined")
    warm = []
    for n in range(16):
        f.edit(n % 2)
        frame, status = apply()
        f.check(frame, status, n % 2)
        warm.append(
            (status["owned_geometry_bytes"], status["device_allocation_bytes"], status["engine_shader_resources"])
        )
    for parity in (0, 1):
        assert len(set(warm[8 + parity :: 2])) == 1, warm
    print("sampled attributes: final", flush=True)
    with ch.Session(
        device=device,
        width=160,
        height=90,
        samples=1024,
        execution="sync",
        interactive=False,
        passes=["combined", "position", "normal", "depth"],
    ) as final:
        for case in ("uv", "color", "image_uv"):
            f.configure(case)
            baseline = None
            for broken in (0, 1, 0):
                f.edit(broken)
                frame = final.wait(final.apply(native_scene.capture(f.rop), at_time=hou.time()))
                planes = f.check(frame, final.status(), broken, baseline)
                if baseline is None:
                    baseline = planes
    print("sampled attributes: five-frame batch", flush=True)
    f.configure("uv")
    f.edit(1)
    f.sop.parm("snippet").set("v@N=set(1,0,0); float h=.5+.25*sin(@Time*72); v@uv=set(h,.5,0); v@Cd=set(h,h,h);")
    f.rop.parm("picture").set(str(f.dest / "sampled-attributes.$F4.exr"))
    f.rop.render(frame_range=(1, 5, 1))
    for number in range(1, 6):
        image = oiio.ImageInput.open(str(f.dest / f"sampled-attributes.{number:04d}.exr"))
        assert image
        spec = image.spec()
        pixels = np.asarray(image.read_image(), np.float32)
        image.close()
        issues = json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert bool(issues) == (number == 1), (number, issues)
        assert (
            np.isfinite(pixels).all()
            and pixels[:, :, [list(spec.channelnames).index(c) for c in ("R", "G", "B")]].max() > 0.1
        ), number
    f.configure("uv")
    hou.setFrame(1)
    hou.hipFile.save(str(f.dest / "sampled-attributes.hiplc"))
finally:
    registry.close_all()
report = dict(
    result="passed", device=device, checks=checks, warm_allocations=warm, final_states=9, native_batch_frames=5
)
path = artifacts_root() / "stage2/displacement/houdini-sampled-attribute-displacement-smoke.json"
path.write_text(json.dumps(report, indent=2))
print(json.dumps(report))
