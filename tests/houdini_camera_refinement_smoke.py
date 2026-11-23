# SPDX-License-Identifier: Apache-2.0
"""Current render camera, retained dicing camera, final refinement and recovery."""

from support.paths import artifacts_root
from subdivision_test_setup import configure_render, set_subdivision
from cycles_houdini.device_selection import set_devices as set_rop_devices
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import time
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage2/subdivision"
destination.mkdir(parents=True, exist_ok=True)
obj = hou.node("/obj").createNode("geo", "camera_refinement")
grid = obj.createNode("grid", "coarse_native_grid")
grid.setParms(dict(type=0, orient=0, rows=3, cols=3))
grid.parmTuple("size").set((4, 4))
grid.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam", "refinement_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=3))
material = ch.create_material(name="refinement_material", shader="emission")
material.node("surface_shader").parm("strength").set(1)
coordinates = material.createNode("cycles::texture_coordinate::1", "local_bounds")
material.node("surface_shader").setNamedInput("color", coordinates, "generated")
rop = hou.node("/out").createNode("cycles_ipr", "refinement_rop")
set_rop_devices(rop, device)
configure_render(
    rop,
    dict(
        soppath=grid.path(),
        camera=camera.path(),
        materialpath=material.path(),
        resx=160,
        resy=120,
        samples=8,
        subdivision_limit=1000000,
        subdivision=dict(type="catmull_clark", space="pixel", dicing_rate=8, max_level=5),
    ),
)
rop.parmTuple("background").set((0, 0, 0))
rop.parmTuple("lightstrength").set((0, 0, 0))
checks = []


def pixels(frame):
    return np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(120, 160, 4)


with ch.Session(device=device, width=160, height=120, samples=8, collect_diagnostics=True) as session:
    first = session.wait(session.apply(native_scene.capture(rop)))
    before = session.status()
    for index in range(4):
        camera.parm("tx").set(0.08 * (index + 1))
        update = native_scene.capture(rop)
        update["defer_camera_subdivision"] = True
        current = session.wait(session.apply(update))
        status = session.status()
        diagnostics = session.diagnostics()
        assert status["subdivision_tessellations"] == before["subdivision_tessellations"], (before, status)
        assert status["geometry_conversions"] == before["geometry_conversions"]
        assert not {"tri_verts", "tri_vindex"} & diagnostics["buffer_copy_requests"].keys(), diagnostics
        assert not np.array_equal(pixels(first), pixels(current)), "Render camera did not move"
    checks.append(
        dict(
            name="Four camera updates change the image without retessellation, conversion or mesh buffer requests",
            status=status,
        )
    )
    # Same desired camera: finishing the deferred dicing operation must bypass
    # the unchanged-scene shortcut, including through queued/worker snapshots.
    update = native_scene.capture(rop)
    update["defer_camera_subdivision"] = False
    session.wait(session.apply(update, reuse_if_unchanged=True))
    refined = session.status()
    assert refined["subdivision_tessellations"] == before["subdivision_tessellations"] + 1, (before, refined)
    assert refined["geometry_conversions"] == before["geometry_conversions"]
    session.wait(session.apply(update, reuse_if_unchanged=True))
    assert session.status()["subdivision_tessellations"] == refined["subdivision_tessellations"]
    checks.append(
        dict(
            name="Unchanged render camera refines once after release; subsequent unchanged capture is retained",
            status=refined,
        )
    )

    # Request UV tangents on a dense control mesh, then abort subdivision. This
    # specifically exercises the incomplete-triangle path before MikkTSpace.
    normal_material = ch.create_material(name="tangent_cancellation", shader="diffuse_bsdf")
    normal = normal_material.createNode("cycles::normal_map::1", "uv_tangents")
    # The default this test was written for (new nodes now take Blender's).
    normal.setParms(dict(base="original"))
    normal_material.node("surface_shader").setNamedInput("normal", normal, "normal")
    uv = obj.createNode("attribwrangle", "native_uvs")
    uv.setInput(0, grid)
    uv.parm("snippet").set("v@uv=set(@P.x/4+.5,@P.y/4+.5,0);")
    uv.setRenderFlag(True)
    grid.setParms(dict(rows=180, cols=180))
    rop.parm("soppath").set(uv.path())
    configure_render(
        rop,
        dict(materialpath=normal_material.path(), subdivision_limit=1, subdivision=dict(dicing_rate=1, max_level=3)),
    )
    generation = session.apply(native_scene.capture(rop))
    try:
        session.wait(generation)
    except RuntimeError as error:
        assert "Subdivision triangle budget" in str(error), str(error)
    else:
        raise AssertionError("Deliberate subdivision failure succeeded")
    checks.append(
        dict(
            name="Tangent-demanding mesh fails its triangle budget without consuming partial triangles",
            status=session.status(),
        )
    )
    rop.parm("subdivision_limit").set(2000000)
    for attempt in range(3):
        generation = session.apply(native_scene.capture(rop))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = session.status()
            if "tessellat" in status["message"].lower() or status["state"] in ("failed", "complete"):
                break
            time.sleep(0.001)
        session.cancel()
        deadline = time.monotonic() + 5
        while session.status()["state"] != "cancelled" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert session.status()["state"] == "cancelled", session.status()
    grid.setParms(dict(rows=3, cols=3))
    rop.parm("materialpath").set(material.path())
    recovered = session.wait(session.apply(native_scene.capture(rop)))
    assert np.isfinite(pixels(recovered)).all() and session.status()["state"] == "complete"
    checks.append(
        dict(name="Repeated cancellation recovers to a complete render in the same session", status=session.status())
    )
assert session.status()["device_allocation_bytes"] == 0
report = dict(
    result="passed", device=device, execution=os.environ.get("CYCLES_HOUDINI_EXECUTION", "sync"), checks=checks
)
(destination / "houdini-camera-refinement-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result="passed", checks=len(checks))))
