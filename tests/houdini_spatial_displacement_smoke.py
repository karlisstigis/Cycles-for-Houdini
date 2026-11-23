# SPDX-License-Identifier: Apache-2.0
"""Generated and Vector Transform motion against independent affine geometry."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
from subdivision_test_setup import configure_render, set_subdivision
import argparse, hashlib, json, os, re, subprocess, time, traceback
from pathlib import Path
import hou, numpy as np, OpenImageIO as oiio
from cycles_houdini import native_scene, registry, final_render
from spatial_displacement_fixture import Fixture

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "stage2/displacement/spatial"
dest.mkdir(parents=True, exist_ok=True)
references = dest / ("references-" + re.sub("[^A-Za-z0-9_-]", "_", device))
run = subprocess.run(
    [
        str(runtime_executable("ch_harness.exe")),
        "--resources",
        os.environ["CYCLES_HOUDINI_RESOURCES"],
        "--device",
        device,
        "--spatial-displacement-reference",
        "--output",
        str(references),
    ],
    capture_output=True,
    text=True,
    timeout=240,
)
assert run.returncode == 0, (run.stdout, run.stderr)
specs = json.loads((references / "specifications.json").read_text())
f = Fixture(device)
entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
report = dict(
    result="running", device=device, reference_contract=json.loads(run.stdout.strip().splitlines()[-1]), checks=[]
)
entry.session.close()
entry.session = registry.session_from_configuration(native_scene.configuration(f.rop), collect_diagnostics=True)
cache = {}


def reference(label):
    if label not in cache:
        path = references / (label + ".exr")
        image = oiio.ImageBuf(str(path))
        pixels = image.get_pixels()
        channels = list(image.spec().channelnames)
        cache[label] = (
            np.flipud(pixels[:, :, [channels.index(c) for c in ("R", "G", "B")]]),
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return cache[label]


def render(label, steps=3, offset=0, ref=None, objects=1, camera_only=False, codes=(), retain=False):
    configure_render(f.rop, dict(motion_samples=steps, shutteroffset=offset))
    before = entry.session.status()
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop, camera_only=camera_only), hou.time()))
    status = entry.session.status()
    deadline = time.monotonic() + 3
    while status["state"] != "complete" and time.monotonic() < deadline:
        assert status["state"] != "failed", status
        time.sleep(0.005)
        status = entry.session.status()
    plane = frame["planes"]["combined"]
    pixels = np.frombuffer(plane["pixels"], np.float32).reshape(96, 160, plane["channels"])[:, :, :3]
    expected, digest = reference(
        ref or f.spec["name"] + "-" + str(steps) + "-" + ("m" if offset < 0 else "p" if offset > 0 else "c")
    )
    error = float(np.abs(pixels - expected).mean())
    diagnostics = entry.session.diagnostics()
    report["checks"].append(
        dict(
            label=label,
            mean_difference=error,
            reference_sha256=digest,
            status=status,
            previous=before,
            diagnostics=diagnostics,
        )
    )
    assert (
        status["state"] == "complete" and status["geometry_resources"] == 1 and status["object_resources"] == objects
    ), status
    assert not status["motion_warnings"] and {d["code"] for d in status["source_diagnostics"]} == set(codes), status
    if not np.isfinite(pixels).all() or error >= 0.0012:
        np.savez(dest / "failed-image.npz", expected=expected, actual=pixels)
        raise AssertionError((label, error))
    if retain:
        assert all(
            status[key] == before[key] for key in ("geometry_conversions", "extractions", "displacement_rebuilds")
        ), status
        # Inactive camera controls now canonicalize to an identical snapshot.
        # Its cached diagnostics describe the preceding physical render.
        if status["render_revision"] != before["render_revision"]:
            assert not {"tri_verts", "tri_vindex"} & diagnostics["buffer_copy_requests"].keys(), diagnostics
    return status, pixels


try:
    for spec in specs:
        if spec["name"].startswith("gui_"):
            continue
        f.configure(spec)
        for steps in (3, 5):
            for offset in (-0.75, 0, 0.75):
                render(spec["name"], steps, offset)
    by_name = {spec["name"]: spec for spec in specs}
    for name in ("generated_scalar", "world_object_normal"):
        f.configure(by_name[name])
        for scheme in ("linear", "catmull_clark"):
            configure_render(f.rop, dict(subdivision=dict(type=scheme, space="object", dicing_rate=0.1, max_level=3)))
            render(name + " " + scheme + " subdivision", offset=0.75, ref=name + "-" + scheme)
    # Distinct material identities deliberately split render vertices. Generated
    # must still address the separately owned nominal canonical mesh.
    for name in ("generated_vector", "generated_scalar"):
        f.configure(by_name[name])
        f.grid.parm("cols").set(3)
        f.output.parm("snippet").set(
            's@shop_materialpath=@primnum%2?"'
            + f.materials[2]["material"].path()
            + '":"'
            + f.materials[0]["material"].path()
            + '";'
        )
        for steps in (3, 5):
            render(name + " material seams retain nominal Generated", steps, 0.75)
    targets = f.obj.createNode("line", "two_targets")
    targets.setParms(dict(points=2, dist=4))
    targets.parmTuple("dir").set((1, 0, 0))
    copies = f.obj.createNode("copytopoints", "shared_spatial_instances")
    copies.setInput(0, f.output)
    copies.setInput(1, targets)
    copies.parm("pack").set(1)
    copies.setDisplayFlag(True)
    copies.setRenderFlag(True)
    for name in ("generated_vector", "generated_scalar", "world_object_normal"):
        f.configure(by_name[name])
        f.transforms(0.35)
        for steps in (3, 5):
            for offset in (-0.75, 0.75):
                render(name + " parented packed instances", steps, offset, objects=2)
    f.transforms()
    f.output.setDisplayFlag(True)
    f.output.setRenderFlag(True)
    f.configure(by_name["world_object_vector"])
    render("Before ordinary camera navigation")
    f.camera.parm("tx").set(0.1)
    render("Ordinary camera edit retains geometry", ref="gui_plain_camera_shift-3-c", camera_only=True, retain=True)
    f.configure(by_name["gui_camera_point"])
    before, _ = render("Camera-dependent point displacement")
    f.camera.parm("tx").set(0.2)
    updated, _ = render(
        "Camera matrix edit reevaluates dependent displacement", ref="gui_camera_shift-3-c", camera_only=True
    )
    assert (
        updated["geometry_conversions"] == before["geometry_conversions"]
        and updated["extractions"] == before["extractions"]
        and updated["displacement_rebuilds"] > before["displacement_rebuilds"]
    ), updated
    f.camera.parm("focal").set(60)
    render(
        "Orthographic lens edit keeps camera-space displacement",
        ref="gui_camera_shift-3-c",
        camera_only=True,
        retain=True,
    )
    f.camera.parm("tx").set(0.1)
    render("Restoring the camera matrix restores its displaced image", ref="gui_camera_point-3-c", camera_only=True)
    # Add Generated only to surface shading on an existing split mesh. Repairing
    # unsupported instancer coordinates must retain independent displacement.
    f.configure(by_name["world_object_vector"])
    f.grid.parm("cols").set(3)
    f.output.parm("snippet").set(
        's@shop_materialpath=@primnum%2?"'
        + f.materials[2]["material"].path()
        + '":"'
        + f.materials[0]["material"].path()
        + '";'
    )
    render("Split mesh before Generated surface demand", offset=0.75)
    for nodes in f.materials:
        nodes["surface"].setNamedInput("color", nodes["color"], "vector")
    render("New Generated demand builds the retained seam map", offset=0.75, ref="gui_surface_generated-3-p")
    for nodes in f.materials:
        nodes["coordinate"].parm("from_dupli").set(1)
    render(
        "Unsupported instancer source keeps independent displaced geometry",
        offset=0.75,
        ref="gui_surface_diagnostic-3-p",
        codes=("material.unsupported_attribute",),
        retain=True,
    )
    for nodes in f.materials:
        nodes["coordinate"].parm("from_dupli").set(0)
    render(
        "Repairing the Generated source clears warnings without rebuilding displacement",
        offset=0.75,
        ref="gui_surface_generated-3-p",
        retain=True,
    )
    warm = []
    for nodes in f.materials:
        nodes["surface"].setInput(nodes["surface"].inputIndex("color"), None)
    for index in range(12):
        high = index % 2
        for nodes in f.materials:
            nodes["transform"].parmTuple("vector")[0].set(0.4 if high else 0.25)
        chosen = by_name["gui_input_high" if high else "world_object_vector"]
        status, _ = render("Repeated spatial displacement edit " + str(index), offset=0.75, ref=chosen["name"] + "-3-p")
        warm.append(
            {key: status[key] for key in ("owned_geometry_bytes", "device_allocation_bytes", "engine_shader_resources")}
        )
    for parity in (0, 1):
        for key in warm[0]:
            assert len({row[key] for row in warm[6 + parity :: 2]}) == 1, (key, warm)
    report["warm_resources"] = warm
    registry.close_all()
    f.configure(by_name["generated_scalar"])
    configure_render(f.rop, dict(motion_samples=3, persistent_data=1, picture=str(dest / "frame.$F4.exr")))
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    batch = []
    for index in range(1, 6):
        path = dest / f"frame.{index:04d}.exr"
        image = oiio.ImageBuf(str(path))
        pixels = image.get_pixels()
        channels = list(image.spec().channelnames)
        expected, digest = reference("frame-" + str(index))
        error = float(np.abs(pixels[:, :, [channels.index(c) for c in ("R", "G", "B")]] - np.flipud(expected)).mean())
        assert error < 0.0012, (index, error)
        issues = json.loads(image.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert not issues, issues
        batch.append(dict(frame=index, mean_difference=error, reference_sha256=digest))
    report.update(batch=batch, final=summary)
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (dest / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
