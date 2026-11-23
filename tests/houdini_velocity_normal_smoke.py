# SPDX-License-Identifier: Apache-2.0
"""Velocity blur with point/vertex normals against independently authored motion images."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
from subdivision_test_setup import configure_render, set_subdivision
import argparse, json, os, subprocess, time, traceback
from pathlib import Path
import hou, numpy as np, OpenImageIO as oiio
from cycles_houdini import native_scene, registry, final_render
from velocity_normal_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "stage2/motion/velocity-normals"
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
report = dict(result="running", device=device, checks=[])


def render(label, samples=3, codes=(), normal=None, shift=0, static=False, retain=False):
    f.rop.parm("motion_samples").set(samples)
    before = entry.session.status()
    now = hou.time()
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), now))
    status = entry.session.status()
    deadline = time.monotonic() + 3
    while status["state"] != "complete" and time.monotonic() < deadline:
        assert status["state"] != "failed", status
        time.sleep(0.005)
        status = entry.session.status()
    planes = f.pixels(frame)
    expected = np.roll(references[samples], shift, axis=1)
    if shift < 0:
        expected[:, shift:] = 0
    elif shift > 0:
        expected[:, :shift] = 0
    error = np.abs(planes["combined"][:, :, :3] - expected)
    report["checks"].append(dict(label=label, mean_difference=float(error.mean()), status=status, previous=before))
    assert status["state"] == "complete" and not status["motion_warnings"], status
    assert {issue["code"] for issue in status["source_diagnostics"]} == set(codes), status
    assert status["motion_cooks"] == 0 and hou.time() == now, (
        "Velocity must not cook shutter geometry or move the timeline"
    )
    assert all(np.isfinite(pixels).all() for pixels in planes.values())
    if static:
        assert abs(float(planes["combined"][:, :, 2].sum()) - 0.8 * 60 * 40) < 25
    else:
        assert error.mean() < 0.0008, (label, float(error.mean()))
    expected_normal = np.asarray(normal if normal is not None else f.normal)
    measured = planes["normal"][40:50, 76:84]
    length = np.linalg.norm(measured, axis=2)
    assert (length > 0.1).all()
    np.testing.assert_allclose(
        measured / length[:, :, None], np.broadcast_to(expected_normal, measured.shape), atol=0.002, rtol=0
    )
    if retain:
        assert (
            status["geometry_conversions"] == before["geometry_conversions"]
            and status["extractions"] == before["extractions"]
        ), (before, status)
    return status, planes


try:
    for displaced in (False, True):
        for vertex in (False, True):
            f.setup(displaced, vertex)
            for samples in (3, 5):
                render(
                    ("Displaced" if displaced else "Plain") + (" vertex" if vertex else " point") + " normals", samples
                )
                for offset in (-1, 1):
                    f.rop.parm("shutteroffset").set(offset)
                    f.camera.parm("tx").set(offset * 0.75)
                    render("Authored normals with shutter offset " + str(offset), samples)
                f.rop.parm("shutteroffset").set(0)
                f.camera.parm("tx").set(0)
    f.setup(vertex=True)
    render("Before invalid vertex normal")
    f.vertex.parm("snippet").set("v@N=set(0,0,0);")
    render("Invalid normal retains geometry velocity", codes=("geometry.invalid_normal",), normal=(0, 0, 1))
    f.vertex.parm("snippet").set("v@N=set(.6,0,.8);")
    render("Normal repair restores authored shading")
    f.sop.parm("snippet").set("v@N=set(.6,0,.8);")
    render("Missing velocity retains authored normals and requested geometry", static=True)
    f.sop.parm("snippet").set("v@N=set(.6,0,.8);v@v=set(72,0,0);")
    render("Velocity repair restores blur")
    f.camera.parm("tx").set(0.05)
    render("Camera-only edit retains velocity geometry", shift=-2, retain=True)
    f.camera.parm("tx").set(0)
    f.sop.parm("snippet").set("v@N=set(.6,0,.8);v@v=set(72,0,@P.x*36);")
    _, planes = render("Nonuniform point velocity moves geometry while retaining authored shading normals")
    assert np.abs(planes["position"][:, :, 2]).max() > 0.05
    for scheme in ("linear", "catmull_clark"):
        f.setup()
        f.sop.parm("snippet").set("v@N=set(0,0,1);v@v=set(72,0,0);")
        f.normal = (0, 0, 1)
        configure_render(f.rop, dict(subdivision=dict(type=scheme, space="object", dicing_rate=0.1, max_level=3)))
        render(scheme + " velocity geometry with authored normals")
    f.setup(vertex=True)
    targets = f.obj.createNode("line", "two_packed_targets")
    targets.setParms(dict(points=2, dist=5))
    targets.parmTuple("dir").set((1, 0, 0))
    copies = f.obj.createNode("copytopoints", "packed_velocity_normals")
    copies.setInput(0, f.vertex)
    copies.setInput(1, targets)
    copies.parm("pack").set(1)
    copies.setDisplayFlag(True)
    copies.setRenderFlag(True)
    packed, _ = render("Packed prototypes retain authored normals and vertex velocity")
    assert packed["geometry_resources"] == 1 and packed["object_resources"] == packed["packed_instances"] == 2, packed
    f.vertex.parm("snippet").set("v@N=set(0,0,0);")
    render(
        "Invalid packed prototype normals retain geometry velocity",
        codes=("geometry.invalid_normal",),
        normal=(0, 0, 1),
    )
    f.vertex.parm("snippet").set("v@N=set(.6,0,.8);")
    render("Packed prototype normal repair restores shading")
    f.camera.parm("tx").set(0.05)
    render("Packed camera edit retains captured velocity prototypes", shift=-2, retain=True)
    registry.close_all()
    f.setup(vertex=True)
    f.obj.node("grid1").parm("rows").setExpression("2+$F%3", hou.exprLanguage.Hscript)
    f.vertex.parm("snippet").set("v@N=@Frame==3?set(0,0,0):set(.6,0,.8);")
    configure_render(f.rop, dict(motion_samples=3, persistent_data=1, picture=str(dest / "frame.$F4.exr")))
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    reference = oiio.ImageBuf(str(reference_dir / "position-3.exr"))
    pixels = reference.get_pixels()
    ref = pixels[:, :, [list(reference.spec().channelnames).index(c) for c in ("R", "G", "B")]]
    batch = []
    for index in range(1, 6):
        image = oiio.ImageBuf(str(dest / f"frame.{index:04d}.exr"))
        pixels = image.get_pixels()
        channels = list(image.spec().channelnames)
        error = np.abs(pixels[:, :, [channels.index(c) for c in ("R", "G", "B")]] - ref)
        assert error.mean() < 0.0008, (index, float(error.mean()))
        issues = json.loads(image.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert {issue["code"] for issue in issues} == ({"geometry.invalid_normal"} if index == 3 else set()), issues
        geometry = f.vertex.geometryAtFrame(index)
        assert not any(
            (
                geometry.findPointAttrib("id"),
                geometry.findPointAttrib("name"),
                geometry.findPrimAttrib("id"),
                geometry.findPrimAttrib("name"),
            )
        )
        batch.append(
            dict(
                frame=index,
                points=len(geometry.points()),
                primitives=len(geometry.prims()),
                mean_difference=float(error.mean()),
                diagnostics=issues,
            )
        )
    assert len({frame["points"] for frame in batch}) == 3
    report.update(batch=batch, final=summary)
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (dest / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
