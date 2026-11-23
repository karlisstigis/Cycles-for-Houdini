# SPDX-License-Identifier: Apache-2.0
"""Preview texture failure/repair preserves independent displacement and survives an owned worker exit."""

from support.paths import artifacts_root
import json
import os
from pathlib import Path
import sys
from support.devices import fixture_device
import time
import traceback

import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import _native
from cycles_houdini.material_previews import PreviewService

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/material-thumbnail-resources" / str(time.time_ns())
destination.mkdir(parents=True)
device = fixture_device(sys.argv)
service = PreviewService(device=device, samples=8, idle_seconds=30, busy=lambda: False)
report = dict(result="running", scope=__doc__, device=device, events=[])


def image(path, color):
    pixels = np.broadcast_to(np.array((*color, 1), np.float32), (4, 4, 4)).copy()
    output = oiio.ImageOutput.create(str(path))
    spec = oiio.ImageSpec(4, 4, 4, oiio.FLOAT)
    assert output.open(str(path), spec) and output.write_image(pixels) and output.close()
    return str(path)


def wait(predicate, seconds=45):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        service.tick(force=True)
        if predicate():
            return
        time.sleep(0.01)
    raise TimeoutError(service.diagnostics())


def ready(count):
    wait(lambda: service.completed >= count and service.active is None and not service.pending)
    status = service.worker.status()
    frame = service.worker.frame()
    field = frame["planes"]["combined"]
    pixels = np.frombuffer(field["pixels"], np.float32).reshape(64, 64, 4).copy()
    report["events"].append(dict(completed=count, status=status, diagnostics=service.diagnostics()))
    return status, pixels


try:
    material = ch.create_material(name="resource_thumbnail", shader="emission")
    surface = material.node("surface_shader")
    surface.parm("strength").set(1)
    color = material.createNode("cycles::image_texture::1", "color_image")
    height = material.createNode("cycles::image_texture::1", "height_image")
    color.setParms(dict(filename=image(destination / "color.exr", (0.2, 0.4, 0.8)), colorspace="data", tiles=""))
    height.setParms(dict(filename=image(destination / "height.exr", (0.5, 0.5, 0.5)), colorspace="data", tiles=""))
    surface.setNamedInput("color", color, "color")
    displacement = material.createNode("cycles::displacement::1", "displacement")
    displacement.setParms(dict(midlevel=0, scale=0.3))
    displacement.setNamedInput("height", height, "color")
    material.node("surface_output").setNamedInput("displacement", displacement, "displacement")
    material.parm("cycles_displacement_method").set("true")
    service.request(material, 64)
    first, _ = ready(1)
    assert first["displacement_rebuilds"] > 0 and not first["source_diagnostics"], first
    # Independent alpha coverage gives a geometry check unaffected by the
    # surface's recovered color. This flag belongs only to this test's worker.
    generation = service.worker.apply(dict(transparent_background=True))
    service.worker.wait(generation, timeout=40)
    alpha = (
        np.frombuffer(service.worker.frame()["planes"]["combined"]["pixels"], np.float32)
        .reshape(64, 64, 4)[:, :, 3]
        .copy()
    )
    assert alpha.min() == 0 and alpha.max() == 1
    color.parm("filename").set(str(destination / "missing_color.exr"))
    broken, pixels = ready(2)
    assert broken["source_diagnostics"] and material.sessionId() in service.errors, broken
    np.testing.assert_allclose(pixels[32, 32, :3], (1, 0, 1), atol=0.001, rtol=0)
    np.testing.assert_array_equal(pixels[:, :, 3], alpha)
    assert broken["displacement_rebuilds"] == first["displacement_rebuilds"], (first, broken)
    color.parm("filename").set(str(destination / "color.exr"))
    repaired, pixels = ready(3)
    assert not repaired["source_diagnostics"] and material.sessionId() not in service.errors, repaired
    np.testing.assert_allclose(pixels[32, 32, :3], (0.2, 0.4, 0.8), atol=0.001, rtol=0)
    np.testing.assert_array_equal(pixels[:, :, 3], alpha)
    height.parm("filename").set(str(destination / "missing_height.exr"))
    omitted, pixels = ready(4)
    assert omitted["source_diagnostics"] and material.sessionId() in service.errors, omitted
    np.testing.assert_allclose(pixels[32, 32, :3], (0.2, 0.4, 0.8), atol=0.001, rtol=0)
    assert alpha.sum() > pixels[:, :, 3].sum() + 20, (alpha.sum(), pixels[:, :, 3].sum())
    # Repair the same filename without a parameter edit: the live preview
    # worker's existing texture observer must request the repaired source.
    image(destination / "missing_height.exr", (0.5, 0.5, 0.5))
    restored, pixels = ready(5)
    assert not restored["source_diagnostics"] and material.sessionId() not in service.errors, restored
    np.testing.assert_array_equal(pixels[:, :, 3], alpha)
    old_process = service.worker._controller.backend.process
    old_pid = old_process.pid
    old_process.kill()
    old_process.wait(timeout=10)
    wait(lambda: service.worker is None and service.retiring is None)
    assert any(event["event"] == "worker_failed" for event in service.events), service.diagnostics()
    service.request(material, 64)
    new, _ = ready(6)
    assert new["renderer_pid"] != old_pid and new["state"] == "complete"
    surface.parm("strength").setExpression("$F", hou.exprLanguage.Hscript)
    rebuilds = new["displacement_rebuilds"]
    animation = []
    for frame in range(1, 6):
        count = service.completed + 1
        hou.setFrame(frame)
        animated, pixels = ready(count)
        np.testing.assert_allclose(pixels[32, 32, :3], np.array((0.2, 0.4, 0.8)) * frame, atol=0.001, rtol=0)
        assert animated["displacement_rebuilds"] == rebuilds and not animated["source_diagnostics"], animated
        animation.append(
            dict(
                frame=frame, pixel=pixels[32, 32, :3].tolist(), displacement_rebuilds=animated["displacement_rebuilds"]
            )
        )
    diffuse = material.createNode("cycles::diffuse_bsdf::1", "surface_with_normal_map")
    diffuse.parmTuple("color").set((0.4, 0.6, 0.2))
    normal_image = material.createNode("cycles::image_texture::1", "normal_image")
    normal_image.setParms(dict(filename=image(destination / "normal.exr", (0.5, 0.5, 1)), colorspace="data", tiles=""))
    normal = material.createNode("cycles::normal_map::1", "normal_map")
    # The default this test was written for (new nodes now take Blender's).
    normal.setParms(dict(base="original"))
    normal.setNamedInput("color", normal_image, "color")
    diffuse.setNamedInput("normal", normal, "normal")
    closure = next(name for name, kind in zip(diffuse.outputNames(), diffuse.outputDataTypes()) if kind == "bsdf")
    material.node("surface_output").setNamedInput("surface", diffuse, closure)
    normal_valid, _ = ready(service.completed + 1)
    assert not normal_valid["source_diagnostics"], normal_valid
    # Adding the tangent basis can refresh mesh attributes once; losing an
    # independent normal input must not re-evaluate the displacement branch.
    normal_rebuilds = normal_valid["displacement_rebuilds"]
    normal_image.parm("filename").set(str(destination / "missing_normal.exr"))
    normal_omitted, pixels = ready(service.completed + 1)
    assert normal_omitted["source_diagnostics"] and material.sessionId() in service.errors, normal_omitted
    assert pixels[32, 32, 1] > 0.001 and normal_omitted["displacement_rebuilds"] == normal_rebuilds, (
        pixels[32, 32],
        normal_omitted,
    )
    normal_image.parm("filename").set(str(destination / "normal.exr"))
    normal_repaired, _ = ready(service.completed + 1)
    assert not normal_repaired["source_diagnostics"] and material.sessionId() not in service.errors, normal_repaired
    report.update(
        result="passed",
        independent_color_failure_keeps_displacement=True,
        missing_displacement_omitted=True,
        repair_without_parameter_edit=True,
        old_worker_pid=old_pid,
        new_worker_pid=new["renderer_pid"],
        five_frame_animation=animation,
        normal_failure_omits_effect=True,
    )
except BaseException:
    report.update(result="failed", error=traceback.format_exc(), final=service.diagnostics())
    raise
finally:
    try:
        service.close(wait=True)
    finally:
        (destination / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        (destination.parent / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], events=len(report["events"]), report=str(destination / "report.json"))))
