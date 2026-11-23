# SPDX-License-Identifier: Apache-2.0
"""Animated owned COP displacement against independent moving-vertex images."""

from native_test_runtime import runtime_executable
from subdivision_test_setup import configure_render, set_subdivision
import argparse, json, os, subprocess, traceback
from pathlib import Path
import hou
import numpy as np
import OpenImageIO as oiio
from cycles_houdini import native_scene, registry, _native, object_properties
from cop_shutter_fixture import Fixture

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
report = dict(result="running", device=device, checks=[])


def pixels(frame):
    return {
        name: np.frombuffer(p["pixels"], np.float32).reshape(90, 160, p["channels"]).copy()
        for name, p in frame["planes"].items()
    }


def render(name, broken=False, reference=None):
    scene = native_scene.capture(f.rop, 0)
    frame = entry.session.wait(entry.apply(scene, 0), timeout=120)
    status = entry.session.status()
    assert not status["error"], status
    assert status["render_device_ids"] == [device], status
    assert bool(status["source_diagnostics"]) == broken, status["source_diagnostics"]
    planes = pixels(frame)
    assert all(np.isfinite(p).all() for p in planes.values())
    assert planes["combined"][:, :, :3].max() > 0.1
    result = dict(
        name=name,
        diagnostics=status["source_diagnostics"],
        cop=_native.cop_capture_stats(entry.session._handle),
        displacement_rebuilds=status["displacement_rebuilds"],
        geometry_conversions=status["geometry_conversions"],
    )
    if reference:
        reader = oiio.ImageInput.open(str(references / reference))
        assert reader
        spec = reader.spec()
        values = np.asarray(reader.read_image(), np.float32)
        reader.close()
        expected = np.flipud(values[:, :, [list(spec.channelnames).index(c) for c in ("R", "G", "B")]])
        error = np.abs(expected - planes["combined"][:, :, :3])
        result.update(mean_difference=float(error.mean()), maximum_difference=float(error.max()))
        assert (
            error.mean() < 0.0015
            and abs(float(expected.sum() - planes["combined"][:, :, :3].sum())) < float(expected.sum()) * 0.012
        ), result
    report["checks"].append(result)
    return planes, status


try:
    for kind in ("legacy", "modern"):
        f.choose(kind)
        for steps in (3, 5):
            f.rop.parm("motion_samples").set(steps)
            reference = ("legacy_sequence_position" if kind == "legacy" else "position") + "-%d.exr" % steps
            base, status = render(kind + " COP uses independently owned shutter images", reference=reference)
            captures = _native.cop_capture_stats(entry.session._handle)["captures"]
            conversions = status["geometry_conversions"]
            camera = hou.node(f.rop.evalParm("camera"))
            camera.parm("tx").set(0.05)
            entry.session.wait(entry.apply(dict(camera_path=camera.path()), 0))
            assert (
                entry.session.status()["geometry_conversions"] == conversions
                and _native.cop_capture_stats(entry.session._handle)["captures"] == captures
            )
            camera.parm("tx").set(0)
            f.break_input(1)
            broken, status = render(kind + " missing shutter-only COP retains usable nominal displaced geometry", True)
            assert np.mean(np.abs(base["combined"] - broken["combined"])) > 0.0001
            assert "world.unavailable" not in {d["code"] for d in status["source_diagnostics"]}
            f.break_input(0)
            repaired, status = render(kind + " shutter source repairs without geometry recapture", reference=reference)
            assert status["geometry_conversions"] == conversions
            assert np.allclose(base["position"], repaired["position"], atol=1e-5)
        rebuilt = status["displacement_rebuilds"]
        f.break_input(2)
        broken, status = render(kind + " bad surface color preserves independent COP displacement", True)
        assert status["displacement_rebuilds"] == rebuilt and np.allclose(
            broken["position"], repaired["position"], atol=1e-5
        )
        f.break_input(0)
        render(
            kind + " independent color repair retains displacement",
            reference=("legacy_sequence_position" if kind == "legacy" else "position") + "-5.exr",
        )
    # The resolver's ordinary legacy image-frame sampling is held across this
    # fractional shutter. Do not invent interpolated COP pixels in the plugin.
    for name in ("colorr", "colorg", "colorb"):
        f.height.parm(name).setExpression(".5+$T*72", hou.exprLanguage.Hscript)
    f.image.parm("filename").deleteAllKeyframes()
    f.image.parm("filename").set("op:" + f.height.path())
    render(
        "Ordinary legacy COP held-frame sampling matches an independent static mesh", reference="held_position-5.exr"
    )
    f.choose("modern")
    configure_render(
        f.rop, dict(motion_samples=3, subdivision=dict(type="linear", space="object", dicing_rate=0.1, max_level=2))
    )
    _, status = render("Owned COP shutter data reaches generated subdivision vertices", reference="position-3.exr")
    assert status["subdivision_triangles"] > 2
    set_subdivision(f.rop, type="none")
    f.rop.parm("geometry_motion").set("transform")
    f.sop.parm("snippet").set("v@N=set(1,0,0);v@uv=set(@P.x,@P.y,0);")
    targets = f.obj.createNode("grid", "cop_instance_targets")
    targets.setParms(dict(type=0, orient=0, rows=2, cols=2))
    targets.parmTuple("size").set((1.8, 1.2))
    copies = f.obj.createNode("copytopoints", "cop_instances")
    copies.setInput(0, f.sop)
    copies.setInput(1, targets)
    copies.parm("pack").set(1)
    copies.setDisplayFlag(True)
    copies.setRenderFlag(True)
    for broken in (False, True, False):
        f.break_input(int(broken))
        _, status = render("Packed COP shutter sharing and repair", broken)
        # World-position texture coordinates require per-instance rendering
        # variants even when the current image happens to be uniform.
        assert (
            status["geometry_resources"] == 1
            and status["mesh_variant_resources"] == 4
            and status["object_resources"] == 4
        ), status
    uv = f.material.createNode("cycles::uvmap::1", "packed_cop_uv")
    f.image.setNamedInput("vector", uv, "UV")
    for broken in (False, True, False):
        f.break_input(int(broken))
        _, status = render("UV-compatible packed COP displacement shares one variant through repairs", broken)
        assert (
            status["geometry_resources"] == status["mesh_variant_resources"] == 1 and status["object_resources"] == 4
        ), status
    f.image.setNamedInput("vector", f.geometry, "position")
    f.sop.setDisplayFlag(True)
    f.sop.setRenderFlag(True)
    copies.destroy()
    targets.destroy()
    f.rop.parm("geometry_motion").set("deformation")
    f.configure("deformation")
    f.choose("modern", 24)
    render("SOP deformation and COP displacement motion combine in physical time", reference="position-3.exr")
    f.configure("image")
    f.choose("modern")
    object_properties.install(f.obj)
    f.obj.parm("cycles_motion_blur").set("off")
    still, _ = render("Per-object motion opt-out uses nominal COP displacement")
    f.break_input(1)
    ignored, _ = render("Missing unused shutter COP is ignored when object motion is disabled")
    assert np.allclose(still["combined"], ignored["combined"], atol=1e-6)
    f.break_input(0)
    f.obj.parm("cycles_motion_blur").set("inherit")
    render("Re-enabling motion restores COP shutter samples", reference="position-3.exr")
    f.rop.parm("ipr_samples").set(32)
    allocations = []
    resources = []
    images = []
    for n in range(12):
        f.break_input(n % 2)
        _, status = render("Warm COP shutter resource replacement " + str(n), bool(n % 2))
        allocations.append(status["device_allocation_bytes"])
        resources.append(status["engine_shader_resources"])
        images.append(_native.cop_capture_stats(entry.session._handle)["pixel_bytes"])
    for parity in (0, 1):
        assert len(set(allocations[6 + parity :: 2])) == 1, allocations
        assert len(set(resources[6 + parity :: 2])) == 1, resources
        assert len(set(images[6 + parity :: 2])) == 1, images
    f.break_input(0)
    configure_render(f.rop, dict(samples=64, ipr_samples=64, picture=str(f.dest / "shutter.$F4.exr")))
    # HScript trigonometry uses degrees; the independent calculation below
    # specifies the animation in radians per second.
    f.constant.parm("f1").setExpression(".5+.5*sin(deg($T*72))", hou.exprLanguage.Hscript)
    f.rop.render(frame_range=(1, 5, 1))
    centroids = []
    for n in range(1, 6):
        reader = oiio.ImageInput.open(str(f.dest / ("shutter.%04d.exr" % n)))
        assert reader
        spec = reader.spec()
        values = np.asarray(reader.read_image())
        reader.close()
        assert np.isfinite(values).all() and values.max() > 0.1
        assert not json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        # A uniformly emitting translating quad has an analytic shutter-mean
        # centroid. With three linearly interpolated motion samples, endpoints
        # have weight 1/4 each and the centre has weight 1/2. The image is fully
        # inside this camera, so visibility-weighted pixel X has the same mean.
        weights = values[:, :, list(spec.channelnames).index("B")]
        xs = (np.arange(160) + 0.5 - 80) * (4 / 160)
        measured = float((weights * xs[None, :]).sum() / weights.sum())
        nominal = hou.frameToTime(n)
        half = 1 / 96
        expected = 0.5 * (np.sin((nominal - half) * 72) + 2 * np.sin(nominal * 72) + np.sin((nominal + half) * 72)) / 4
        assert abs(measured - expected) < 0.012, (n, measured, expected)
        centroids.append(dict(frame=n, measured=measured, expected=float(expected)))
    hou.setFrame(1)
    f.choose("modern")
    hou.hipFile.save(str(f.dest / "cop-shutter.hiplc"))
    report.update(
        result="passed",
        batch_frames=5,
        batch_centroids=centroids,
        warm_logical_bytes=allocations,
        warm_shader_resources=resources,
        warm_host_image_bytes=images,
        scope="Independent moving-vertex reference images, 3/5 samples, CPU/selected device, subdivision/deformation, local repair, short logical resource plateau and five batch frames; production memory/latency certification is separate.",
    )
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (f.dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result="passed", checks=len(report["checks"]), batch_frames=5)))
