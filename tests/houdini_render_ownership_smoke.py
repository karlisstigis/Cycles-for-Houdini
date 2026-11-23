# SPDX-License-Identifier: Apache-2.0
"""One native IPR path, independent ordered final ownership and HIP cleanup."""

from support.paths import artifacts_root

from cycles_houdini.device_selection import set_devices as set_rop_devices
import argparse
import json
import os
from pathlib import Path

import hou
import numpy as np
import OpenImageIO as OIIO
import cycles_houdini as ch
from cycles_houdini import final_render, native_scene, registry


parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
destination = artifacts_root() / "stage1/worker-ownership"
destination.mkdir(parents=True, exist_ok=True)
checks = []
created_final = []
factory = registry.session_from_configuration
original_environment = os.environ.get("CYCLES_HOUDINI_EXECUTION")
obj = hou.node("/obj").createNode("geo", "render_ownership_geometry", run_init_scripts=False)
shape = obj.createNode("box")
shape.parmTuple("size").set((1, 1, 0.2))
shape.setRenderFlag(True)
material = ch.create_material(name="render_ownership_emission", shader="emission")
material.node("surface_shader").parmTuple("color").set((0.25, 0.125, 0.0625))
material.node("surface_shader").parm("strength").set(1)
obj.parm("shop_materialpath").set(material.path())
camera = hou.node("/obj").createNode("cam", "render_ownership_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
rop = hou.node("/out").createNode("cycles_ipr", "render_ownership_rop")
set_rop_devices(rop, device)
rop.setParms(
    dict(
        source_mode="objects",
        vobject=obj.name(),
        camera=camera.path(),
        resx=96,
        resy=72,
        samples=8,
        # This ownership fixture compares exact authored emission. Denoising
        # quality has separate references; do not filter its constant oracle.
        use_denoise=0,
        ipr_use_denoise=0,
        adaptive_sampling=0,
        transparent_background=1,
        # Persistent Data defaults off (Blender's default); this fixture checks a persistent session.
        persistent_data=1,
    )
)
rop.parmTuple("background").set((0, 0, 0))
assert rop.parm("ipr_execution") is None and rop.parm("final_execution") is None
assert rop.evalParm("persistent_data") == 1
notifications = []
observer = native_scene.SceneObserver(rop, lambda: notifications.append("changed"))


def image(entry):
    frame = entry.session.wait(entry.apply(native_scene.capture(entry.rop), hou.time()))
    pixels = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(72, 96, 4)
    assert np.allclose(pixels[36, 48], (0.25, 0.125, 0.0625, 1), atol=1e-5, rtol=0)
    return entry.session.status()


def record_factory(config, **kwargs):
    session = factory(config, **kwargs)
    if kwargs.get("interactive") is False:
        created_final.append(session)
    return session


try:
    # An old development environment must not alter an artist ROP's process policy.
    os.environ["CYCLES_HOUDINI_EXECUTION"] = "sync"
    entry = registry.for_rop(rop, native_scene.configuration(rop))
    baseline = image(entry)
    assert entry.session.execution == "worker" and baseline["renderer_pid"] != os.getpid()
    other = rop.copyTo(hou.node("/out"))
    other.setName("other_render_ownership_rop")
    second = registry.for_rop(other, native_scene.configuration(other))
    other_status = image(second)
    assert second.session.execution == "worker" and other_status["renderer_pid"] not in (
        os.getpid(),
        baseline["renderer_pid"],
    )
    checks.append(
        dict(
            name="Each native ROP owns one persistent worker independently of the development selector",
            first=baseline,
            second=other_status,
        )
    )

    camera.parm("tx").set(0.1)
    moved = image(entry)
    assert (
        moved["renderer_pid"] == baseline["renderer_pid"]
        and moved["geometry_conversions"] == baseline["geometry_conversions"]
    )
    shape.parm("sizez").set(0.4)
    deformed = image(entry)
    assert (
        deformed["renderer_pid"] == baseline["renderer_pid"]
        and deformed["geometry_conversions"] == baseline["geometry_conversions"] + 1
    )
    assert second.session.status()["generation"] == other_status["generation"]
    checks.append(
        dict(
            name="Camera retains geometry; deformation updates the selected worker without replacing it or another ROP",
            camera=moved,
            deformation=deformed,
        )
    )

    notifications.clear()
    rop.parm("persistent_data").set(0)
    rop.parm("persistent_data").set(1)
    assert not notifications, notifications
    registry.session_from_configuration = record_factory
    path = destination / "final.exr"
    path.unlink(missing_ok=True)
    rop.parm("picture").set(str(path))
    accepted = entry.session.status()["generation"]
    os.environ["CYCLES_HOUDINI_EXECUTION"] = "worker"
    rop.render(frame_range=(1, 1, 1))
    assert len(created_final) == 1 and created_final[0].execution == "sync"
    assert created_final[0] is not entry.session and created_final[0].status()["state"] == "closed"
    assert (
        entry.session.status()["generation"] == accepted
        and entry.session.status()["renderer_pid"] == baseline["renderer_pid"]
    )
    assert not final_render.active_sequences()
    output = OIIO.ImageInput.open(str(path))
    assert output, OIIO.geterror()
    spec = output.spec()
    pixels = output.read_image(format=OIIO.FLOAT)
    output.close()
    color = pixels[36, 48, [spec.channelnames.index(name) for name in ("R", "G", "B", "A")]]
    assert np.allclose(color, (0.25, 0.125, 0.0625, 1), atol=1e-5, rtol=0)
    checks.append(
        dict(
            name="Final ROP uses ordered in-process batch ownership and leaves the interactive worker intact",
            color=color.tolist(),
            summary=rop.cachedUserData(final_render._summary_key),
        )
    )

    observer.close()
    saved = destination / "render-ownership-reload.hiplc"
    hou.hipFile.save(str(saved))
    path = rop.path()
    first_session, second_session = entry.session, second.session
    hou.hipFile.load(str(saved), suppress_save_prompt=True)
    for session in (first_session, second_session):
        closed = session.status()
        assert closed["state"] == "closed" and closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0
    assert not registry.entries()
    rop = hou.node(path)
    os.environ["CYCLES_HOUDINI_EXECUTION"] = "sync"
    loaded = registry.for_rop(rop, native_scene.configuration(rop))
    status = image(loaded)
    assert loaded.session.execution == "worker" and status["renderer_pid"] != os.getpid()
    checks.append(
        dict(name="HIP reload cleans both workers and the reopened ROP still uses the single IPR policy", status=status)
    )
finally:
    observer.close()
    registry.session_from_configuration = factory
    registry.close_all()
    final_render.close_all()
    if original_environment is None:
        os.environ.pop("CYCLES_HOUDINI_EXECUTION", None)
    else:
        os.environ["CYCLES_HOUDINI_EXECUTION"] = original_environment

report = dict(
    result="passed",
    device=device,
    native_ipr="worker",
    final_render="sync",
    checks=checks,
    scope="Actual analytic renders, camera/deformation updates, independent ROP/final ownership and HIP shutdown. Artist responsiveness is checked separately on the heavy scene.",
)
(destination / "houdini-render-ownership-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", checks=len(checks))))
