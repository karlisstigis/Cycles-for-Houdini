# SPDX-License-Identifier: Apache-2.0
"""Unavailable packed prototypes remain local, including in owned worker captures."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
import argparse
import json
import os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene, registry
from packed_source_recovery_fixture import packed_source_fixture, assert_component_pixels

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
hou.setFps(24)
hou.setFrame(1)
obj, out, edit = packed_source_fixture()
cam = hou.node("/obj").createNode("cam", "camera")
cam.setParms(dict(tz=6, projection=1, orthowidth=6))
rop = hou.node("/out").createNode("cycles_ipr", "recovery")
set_rop_devices(rop, device)
rop.setParms(
    dict(
        source_mode="objects",
        vobject=obj.path(),
        camera=cam.path(),
        resx=192,
        resy=96,
        samples=16,
        ipr_samples=16,
        use_denoise=0,
        ipr_use_denoise=0,
        allowmotionblur=0,
        use_material_assignments=0,
        material_kind="emission",
        point_radius=0.2,
        curve_width=0.1,
    )
)
rop.parmTuple("basecolor").set((0.2, 0.7, 0.1))
rop.parmTuple("background").set((0, 0, 0))
entry = registry.for_rop(rop, native_scene.configuration(rop))
checks = []


def render(mode):
    edit.parm("mode").set(mode)
    frame = entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
    status = entry.session.status()
    assert status["state"] != "failed" and not out.errors(), (status, out.errors())
    return frame, np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).copy(), status


try:
    for nested in (0, 1):
        out.parm("input").set(nested)
        _, healthy, good = render(0)
        assert not good["source_diagnostics"], good
        assert good["object_resources"] == 6, good
        assert_component_pixels(healthy)
        for broken in (1, 3):
            _, image, status = render(broken)
            assert_component_pixels(image, broken=True)
            issues = status["source_diagnostics"]
            assert len(issues) == 1 and issues[0]["code"] == "geometry.packed_unavailable", issues
            assert status["object_resources"] == good["object_resources"] - 1, (good, status)
            assert "primitive 2" in issues[0]["message"] and ".bgeo.sc" in issues[0]["message"], issues
            count = status["geometry_conversions"]
            entry.session.wait(entry.apply(dict(camera_path=cam.path()), hou.time()))
            assert entry.session.status()["source_diagnostics"] == issues
            assert entry.session.status()["geometry_conversions"] == count
            _, reference, clean = render(2)
            assert np.array_equal(image, reference), (
                "Missing packed contents rendered differently from explicit removal"
            )
            assert not clean["source_diagnostics"]
            _, repaired, recovered = render(0)
            assert np.array_equal(repaired, healthy) and not recovered["source_diagnostics"]
            assert recovered["geometry_resources"] == good["geometry_resources"], "Prototype sharing was lost"
            checks.append(dict(nested=nested, broken=broken, diagnostics=issues))
    out.parm("input").set(0)
    # Missing shutter contents must retain valid requested-frame geometry.
    cam.parm("shutter").set(1)
    for method in ("transform", "deformation"):
        rop.setParms(dict(allowmotionblur=1, geometry_motion=method))
        _, healthy, _ = render(0)
        _, sampled, status = render(4)
        assert np.array_equal(sampled, healthy)
        assert any(d["code"] == "motion.packed_unavailable" for d in status["source_diagnostics"]), status
        checks.append("Missing shutter prototype keeps nominal geometry in " + method)
        if method == "deformation":
            _, warm, status = render(4)
            assert np.array_equal(warm, healthy) and status["motion_cooks"] > 0, status
            checks.append("File-backed deformation reacquires external contents on a warm update")
    rop.parm("allowmotionblur").set(0)
    destination = artifacts_root() / "stage3/materials/recovery/packed-source-frames"
    destination.mkdir(parents=True, exist_ok=True)
    for number, mode in enumerate((0, 1, 0, 3, 0), 1):
        hou.setFrame(number)
        render(mode)
        entry.session.write_exr(destination / ("%s-%d.exr" % ("cpu" if device == "CPU" else "optix", number)))
    checks.append("Five frames written across failures and repairs")
    hou.setFrame(1)
    # Many unavailable copies produce one source warning, not one per instance.
    repeated = obj.createNode("merge", "repeated_failures")
    repeated.setInput(0, out)
    for i in range(1, 9):
        repeated.setInput(i, edit)
    repeated.setRenderFlag(True)
    repeated.setDisplayFlag(True)
    _, many, status = render(1)
    assert (
        len(status["source_diagnostics"]) == 1
        and "9 unavailable packed instances" in status["source_diagnostics"][0]["message"]
    ), status
    repeated.destroy()
    out.setRenderFlag(True)
    out.setDisplayFlag(True)
    _, single, status = render(1)
    assert np.array_equal(many, single)
    checks.append("Repeated missing prototypes aggregate without stray particles")
    # Final/batch's direct capture does not use the worker's frozen ledger.
    with ch.Session(device=device, width=192, height=96, samples=16, interactive=False, execution="sync") as final:

        def direct(mode):
            edit.parm("mode").set(mode)
            f = final.wait(final.apply(native_scene.capture(rop), at_time=hou.time()))
            return np.frombuffer(f["planes"]["combined"]["pixels"], np.float32).copy()

        broken = direct(1)
        assert len(final.status()["source_diagnostics"]) == 1
        clean = direct(2)
        assert not final.status()["source_diagnostics"]
        assert np.array_equal(broken, clean)
        direct(0)
        assert not final.status()["source_diagnostics"]
    checks.append("Direct final capture recovers and repairs too")
    render(1)
    obj.destroy()
    entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
    status = entry.session.status()
    assert not status["source_diagnostics"] and status["object_resources"] == 0, status
    checks.append("Deleting the source retires its objects and warning")
finally:
    registry.close_all()
report = artifacts_root() / "stage3/materials/recovery/houdini-packed-source-recovery-smoke.json"
report.write_text(json.dumps(dict(result="passed", device=device, checks=checks), indent=2), encoding="utf-8")
