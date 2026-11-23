# SPDX-License-Identifier: Apache-2.0
"""World-medium thumbnails expose finite-reference attenuation instead of an unlit black sphere."""

from support.paths import artifacts_root
import argparse, json, os, time, traceback
from pathlib import Path
import hou, numpy as np
import cycles_houdini as ch
from cycles_houdini import _native
from cycles_houdini.material_previews import PreviewService

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
destination = artifacts_root() / "stage3/world-volume-thumbnails"
destination.mkdir(parents=True, exist_ok=True)
world = ch.create_world_material(name="world_medium_preview")
background = world.node("background_shader")
background.parmTuple("color").set((0.1, 0.2, 0.4))
medium = world.createNode("cycles::absorption_volume::1", "absorption")
medium.parmTuple("color").set((0, 0, 0))
medium.parm("density").set(0.4)
world.node("world_output").setNamedInput("volume", medium, "volume")
service = PreviewService(device=device, samples=32, idle_seconds=30, busy=lambda: False)
report = dict(result="running", device=device, events=[])


def ready(count):
    deadline = time.monotonic() + 60
    while service.completed < count or service.active or service.pending:
        service.tick(force=True)
        assert time.monotonic() < deadline, service.diagnostics()
        time.sleep(0.01)
    frame = service.worker.frame()
    pixels = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(64, 64, 4).copy()
    assert _native.thumbnail_pixels(world.sessionId()) and not service.errors, service.diagnostics()
    report["events"].append(
        dict(center=pixels[32, 32, :3].tolist(), corner=pixels[0, 0, :3].tolist(), status=service.worker.status())
    )
    return pixels


try:
    service.request(world, 64)
    fog = ready(1)
    pid = service.worker.status()["renderer_pid"]
    # Camera at z=3.7, reference sphere front near z=1. With a near clip of
    # .01, radiance is approximately .5*exp(-.4*2.69) at the central pixel.
    np.testing.assert_allclose(fog[32, 32, :3], 0.5 * np.exp(-0.4 * 2.69), atol=0.003, rtol=0)
    np.testing.assert_allclose(fog[0, 0, :3], 0, atol=0.00001, rtol=0)
    medium.parm("density").set(0)
    clear = ready(2)
    np.testing.assert_allclose(clear[32, 32, :3], 0.5, atol=0.0001, rtol=0)
    assert service.worker.status()["renderer_pid"] == pid
    world.node("world_output").setInput(1, None)
    ordinary = ready(3)
    np.testing.assert_allclose(ordinary[0, 0, :3], (0.1, 0.2, 0.4), atol=0.0001, rtol=0)
    assert ordinary[32, 32, 2] > ordinary[32, 32, 0] * 2
    assert service.worker.status()["renderer_pid"] == pid and service.worker.status()["geometry_resources"] == 1
    report.update(result="passed", renderer_pid=pid)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    service.close(wait=True)
    world.destroy()
    (destination / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(destination / "report.json")
