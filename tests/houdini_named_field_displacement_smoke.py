# SPDX-License-Identifier: Apache-2.0
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
from named_field_displacement_fixture import Fixture, CASES

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
f = Fixture(device)
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


def reference(steps, kind="position"):
    image = oiio.ImageInput.open(str(refs / f"{kind}-{steps}.exr"))
    assert image
    spec = image.spec()
    pixels = np.asarray(image.read_image(), np.float32)
    image.close()
    return np.flipud(pixels[:, :, [list(spec.channelnames).index(c) for c in ("R", "G", "B")]])


entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
checks = []


def apply():
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
    return frame, entry.session.status()


try:
    for case in CASES:
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
            error = float(np.abs(planes["combined"][:, :, :3] - reference(steps)).mean())
            assert error < 0.0008, (case, steps, error)
            checks.append(dict(case=case, samples=steps, mean_difference=error))
        conversions = status["geometry_conversions"]
        rebuilds = status["displacement_rebuilds"]
        f.obj.parm("tx").set(-0.4)
        frame, status = apply()
        assert status["geometry_conversions"] == conversions and status["displacement_rebuilds"] == rebuilds, status
        f.obj.parm("tx").set(-0.5)
    # Both coarse subdivision paths interpolate all sampled numeric types.
    for scheme in ("linear", "catmull_clark"):
        configure_render(f.rop, dict(subdivision=dict(type=scheme, space="object", dicing_rate=0.1, max_level=3)))
        for case in ("scalar", "float2", "color_layer"):
            f.configure(case)
            f.rop.parm("motion_samples").set(3)
            frame, status = apply()
            planes = f.check(frame, status, False)
            error = float(np.abs(planes["combined"][:, :, :3] - reference(3, scheme + "_position")).mean())
            assert error < 0.0008, (scheme, case, error)
            for broken in (1, 0):
                f.edit(broken)
                frame, status = apply()
                f.check(frame, status, broken, planes)
            checks.append(dict(case=case, subdivision=scheme, mean_difference=error))
    set_subdivision(f.rop, type="none")
    f.configure("scalar")
    # A failed surface field cannot invalidate independent named displacement.
    f.source.setNamedInput("color", f.color, "color")
    frame, status = apply()
    baseline = f.check(frame, status, False)
    f.missing.parm("ptdel").set("height_vector")
    f.edit(1)
    frame, status = apply()
    assert {d["code"] for d in status["source_diagnostics"]} == {"geometry.shutter_attribute"}, status
    p = np.frombuffer(frame["planes"]["position"]["pixels"], np.float32).reshape(90, 160, 3)
    assert np.allclose(p, baseline["position"], atol=1e-5), "Surface field broke independent displacement motion"
    f.edit(0)
    f.source.setInput(f.source.inputIndex("color"), None)
    f.configure("scalar")
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
    with ch.Session(
        device=device,
        width=160,
        height=90,
        samples=1024,
        execution="sync",
        interactive=False,
        passes=["combined", "position", "normal", "depth"],
    ) as final:
        for broken in (0, 1, 0):
            f.edit(broken)
            frame = final.wait(final.apply(native_scene.capture(f.rop), at_time=hou.time()))
            f.check(frame, final.status(), broken)
    f.configure("scalar")
    hou.hipFile.save(str(f.dest / "named-field-displacement.hiplc"))
finally:
    registry.close_all()
report = dict(result="passed", device=device, checks=checks, final_states=3, warm_tracked_resources=warm)
path = artifacts_root() / "stage2/displacement/houdini-named-field-displacement-smoke.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(report, indent=2))
print(json.dumps(report))
