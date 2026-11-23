# SPDX-License-Identifier: Apache-2.0
"""Independent temporal displacement images and retained engine geometry."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
from subdivision_test_setup import configure_render, set_subdivision
import argparse, json, os, subprocess, time, traceback
from pathlib import Path
import hou, numpy as np, OpenImageIO as oiio
from cycles_houdini import native_scene, registry
from sampled_override_fixture import Fixture
from light_shader_fixture import write_image

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "stage2/displacement/temporal-binding-reuse"
dest.mkdir(parents=True, exist_ok=True)
f = Fixture(device)
reference_dir = dest / "references"
run = subprocess.run(
    [
        str(runtime_executable("ch_harness.exe")),
        "--resources",
        os.environ["CYCLES_HOUDINI_RESOURCES"],
        "--device",
        device,
        "--displacement-motion-reference",
        "--output",
        str(reference_dir),
    ],
    capture_output=True,
    text=True,
    timeout=180,
)
assert run.returncode == 0, (run.stdout, run.stderr)
references = {}
for count in (3, 5):
    reader = oiio.ImageInput.open(str(reference_dir / f"position-{count}.exr"))
    assert reader
    spec = reader.spec()
    pixels = np.asarray(reader.read_image(), np.float32)
    reader.close()
    references[count] = np.flipud(pixels[:, :, [list(spec.channelnames).index(c) for c in ("R", "G", "B")]])
entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
entry.session.close()
entry.session = registry.session_from_configuration(native_scene.configuration(f.rop), collect_diagnostics=True)
report = dict(result="running", device=device, checks=[])


def render(label, power=1, samples=3, shift=0, retain=False, color=None, codes=()):
    before = entry.session.status()
    f.rop.parm("motion_samples").set(samples)
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
    status = entry.session.status()
    deadline = time.monotonic() + 3
    while status["state"] != "complete" and time.monotonic() < deadline:
        assert status["state"] != "failed", status
        time.sleep(0.005)
        status = entry.session.status()
    scale = power if color is None else power * np.asarray(color) / np.asarray((0.2, 0.4, 0.8))
    planes = f.pixels(frame)
    expected = np.roll(references[samples], shift, axis=1) * scale
    if shift > 0:
        expected[:, :shift] = 0
    elif shift < 0:
        expected[:, shift:] = 0
    error = np.abs(planes["combined"][:, :, :3] - expected)
    diagnostic = entry.session.diagnostics()
    report["checks"].append(
        dict(label=label, mean_difference=float(error.mean()), status=status, previous=before, diagnostics=diagnostic)
    )
    assert status["state"] == "complete" and not status["motion_warnings"], status
    assert {issue["code"] for issue in status["source_diagnostics"]} == set(codes), status
    assert error.mean() < 0.0008 * max(float(np.max(scale)), 1), (label, float(error.mean()))
    assert all(np.isfinite(pixels).all() for pixels in planes.values())
    if retain:
        for name in (
            "geometry_conversions",
            "displacement_rebuilds",
            "subdivision_tessellations",
            "engine_shader_resources",
        ):
            assert status[name] == before[name], (label, name, before[name], status[name])
        assert not {"tri_verts", "tri_vindex"} & diagnostic["buffer_copy_requests"].keys(), diagnostic
    return status, planes


try:
    for scheme, samples in (("none", 3), ("none", 5), ("linear", 3), ("catmull_clark", 3)):
        f.source.setInput(f.source.inputIndex("color"), None)
        f.setup("deformation")
        configure_render(f.rop, dict(subdivision=dict(type=scheme, space="object", dicing_rate=0.1, max_level=3)))
        if scheme != "none":
            f.vector.parm("space").set("object")
            for component in f.vector.parmTuple("vector"):
                component.deleteAllKeyframes()
                component.set(0, follow_parm_reference=False)
            f.vector.parmTuple("vector")[0].set(f.material.parm("height"), follow_parm_reference=False)
            f.material.node("surface_output").setNamedInput("displacement", f.vector, "displacement")
        f.assigned.setParms(dict(num_local1=2, local1_name2="power", local1_type2="float", local1_fval2=1))
        render(scheme + " initial sampled material", samples=samples)
        for index in range(16):
            power = 1 + (index + 1) / 32
            f.assigned.parm("local1_fval2").set(power)
            render(scheme + " surface-only override " + str(index), power, samples, retain=True)
        previous = entry.session.status()
        f.assigned.parm("local1_fval1").setExpression('.625+$T*ch("test_speed")', hou.exprLanguage.Hscript)
        displaced, _ = render(scheme + " real displacement change", power, samples, shift=5)
        assert displaced["displacement_rebuilds"] > previous["displacement_rebuilds"]
        f.assigned.parm("local1_fval1").setExpression('.5+$T*ch("test_speed")', hou.exprLanguage.Hscript)
        restored, _ = render(scheme + " displacement repair", power, samples)
        assert restored["displacement_rebuilds"] > displaced["displacement_rebuilds"]
        base = dest / "surface-base.exr"
        replacement = dest / "surface-replacement.exr"
        missing = dest / "surface-repaired.exr"
        if missing.exists():
            missing.unlink()
        write_image(base, (0.25, 0.5, 0.75))
        write_image(replacement, (0.5, 0.25, 0.1))
        f.source.setNamedInput("color", f.image, "color")
        f.assigned.setParms(
            dict(num_local1=3, local1_name3="image_file", local1_type3="string", local1_sval3=str(base))
        )
        render(
            scheme + " sampled displacement with an independent color image", power, samples, color=(0.25, 0.5, 0.75)
        )
        f.assigned.parm("local1_sval3").set(str(replacement))
        render(
            scheme + " color filename retains temporal displacement",
            power,
            samples,
            color=(0.5, 0.25, 0.1),
            retain=True,
        )
        f.assigned.parm("local1_sval3").set(str(missing))
        render(
            scheme + " missing color image retains temporal displacement",
            power,
            samples,
            color=(1, 0, 1),
            codes=("texture.unavailable",),
            retain=True,
        )
        write_image(missing, (0.2, 0.4, 0.8))
        render(scheme + " color file repair retains temporal displacement", power, samples, retain=True)
        f.source.setInput(f.source.inputIndex("color"), None)
        render(scheme + " removing the color branch retains temporal displacement", power, samples, retain=True)
    # Identical shader snapshots cannot justify reusing genuinely different geometry.
    before = entry.session.status()
    snippet = f.sop.evalParm("snippet")
    f.sop.parm("snippet").set(snippet + "@P.z+=.05;")
    changed, planes = render("Real geometry edit retains requested displacement and updates vertices", power, 3)
    assert (
        changed["geometry_conversions"] > before["geometry_conversions"]
        and changed["displacement_rebuilds"] > before["displacement_rebuilds"]
    )
    hit = (planes["depth"][:, :, 0] > 0) & (planes["depth"][:, :, 0] < 1e6)
    assert hit.any()
    np.testing.assert_allclose(planes["position"][:, :, 2][hit], 0.05, atol=1e-5, rtol=0)
    f.sop.parm("snippet").set(snippet)
    repaired, planes = render("Geometry repair restores vertices", power, 3)
    assert repaired["geometry_conversions"] > changed["geometry_conversions"]
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (dest / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
