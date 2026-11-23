# SPDX-License-Identifier: Apache-2.0
"""Surface/world/volume native rasters, branch recovery and a shared bounded preview worker."""

from support.paths import artifacts_root
import argparse, json, os, time, traceback
from pathlib import Path
import hou, numpy as np, OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import _native, material_previews as mp

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/material-thumbnail-domains" / str(time.time_ns())
destination.mkdir(parents=True)
service = mp.PreviewService(device=device, samples=16, idle_seconds=30, busy=lambda: False)
report = dict(result="running", device=device, scope=__doc__, events=[])
nodes = []


def ready(label, count=None):
    count = service.completed + 1 if count is None else count
    deadline = time.monotonic() + 60
    while service.completed < count or service.active is not None or service.pending:
        service.tick(force=True)
        assert time.monotonic() < deadline, service.diagnostics()
        time.sleep(0.01)
    status = service.worker.status()
    field = service.worker.frame()["planes"]["combined"]
    pixels = np.frombuffer(field["pixels"], np.float32).reshape(64, 64, 4).copy()
    assert _native.thumbnail_pixels(last_node.sessionId()), service.diagnostics()
    assert status["state"] == "complete" and status["render_device_ids"] == [device], status
    report["events"].append(
        dict(
            label=label,
            status=status,
            domain=service.domain,
            warnings=dict(service.errors),
            corner=pixels[0, 0, :3].tolist(),
            center=pixels[32, 32, :3].tolist(),
        )
    )
    return status, pixels


def request(node):
    global last_node
    last_node = node
    service.request(node, 64)


def expect(value, expected):
    np.testing.assert_allclose(value, expected, atol=0.0001, rtol=0)


try:
    surface = ch.create_material(name="thumbnail_surface", shader="emission")
    nodes.append(surface)
    surface.node("surface_shader").parmTuple("color").set((0.2, 0.4, 0.6))
    surface.node("surface_shader").parm("strength").set(1)
    world = ch.create_world_material(name="thumbnail_world")
    nodes.append(world)
    background = world.node("background_shader")
    background.parmTuple("color").set((0.1, 0.2, 0.4))
    background.parm("strength").set(1)
    volume = ch.create_material(name="thumbnail_volume", shader="absorption_volume")
    nodes.append(volume)
    volume.node("volume_shader").parmTuple("color").set((0.2, 0.6, 0.3))
    scale = volume.node("density_scale")
    scale.parm("value2").set(2)
    hou.hipFile.save(str(destination / "domains.hiplc"), save_to_recent_files=False)
    before = dict(
        objects=[n.path() for n in hou.node("/obj").children()], dirty=hou.hipFile.hasUnsavedChanges(), time=hou.time()
    )
    request(surface)
    first, pixels = ready("surface raster")
    expect(pixels[32, 32, :3], (0.2, 0.4, 0.6))
    pid = first["renderer_pid"]
    assert (
        dict(
            objects=[n.path() for n in hou.node("/obj").children()],
            dirty=hou.hipFile.hasUnsavedChanges(),
            time=hou.time(),
        )
        == before
    )
    request(world)
    initial, pixels = ready("world shader illuminates neutral sphere with no analytic lights")
    assert initial["renderer_pid"] == pid and initial["light_resources"] == 0 and initial["geometry_resources"] == 1, (
        initial
    )
    expect(pixels[0, 0, :3], (0.1, 0.2, 0.4))
    base = pixels.copy()
    background.parm("strength").set(2)
    edited, pixels = ready("world strength edit reuses geometry")
    expect(pixels[:, :, :3], base[:, :, :3] * 2)
    assert edited["geometry_conversions"] == initial["geometry_conversions"]
    image = world.createNode("cycles::environment_texture::1", "repairable_environment")
    missing = destination / "repaired_environment.exr"
    image.setParms(dict(filename=str(missing), colorspace="data"))
    background.setNamedInput("color", image, "color")
    broken, pixels = ready("broken world uses neutral fallback, never magenta illumination")
    expect(pixels[0, 0, :3], (0.12, 0.12, 0.12))
    assert world.sessionId() in service.errors
    assert any(d["code"] == "world.unavailable" for d in broken["source_diagnostics"]), broken
    output = oiio.ImageOutput.create(str(missing))
    assert output.open(str(missing), oiio.ImageSpec(4, 2, 3, oiio.FLOAT))
    assert (
        output.write_image(np.broadcast_to(np.array((0.3, 0.6, 0.2), np.float32), (2, 4, 3)).copy()) and output.close()
    )
    repaired, pixels = ready("same-path world texture file repair")
    expect(pixels[0, 0, :3], (0.6, 1.2, 0.4))
    assert not repaired["source_diagnostics"] and world.sessionId() not in service.errors
    request(volume)
    cloud, pixels = ready("owned density cloud renders absorption with channel ordering")
    # volume_ray_marching names the biased integrator policy, not the presence
    # of a volume. Absorption/zero-density images below verify the actual field.
    assert cloud["renderer_pid"] == pid and cloud["geometry_resources"] == cloud["object_resources"] == 1, cloud
    assert 0.12 > pixels[32, 32, 1] > pixels[32, 32, 2] > pixels[32, 32, 0] > 0, pixels[32, 32]
    expect(pixels[0, 0, :3], (0.12, 0.12, 0.12))
    conversions = cloud["geometry_conversions"]
    scale.parm("value2").set(0)
    zero, pixels = ready("zero density is exactly the independent background")
    expect(pixels[:, :, :3], np.full((64, 64, 3), 0.12))
    assert zero["geometry_conversions"] == conversions
    attribute = volume.createNode("cycles::attribute::1", "missing_field")
    attribute.parm("attribute").set("missing_density")
    scale.setNamedInput("value1", attribute, "fac")
    scale.parm("value2").set(2)
    broken, pixels = ready("missing volume field keeps cloud with diagnostic scattering")
    assert broken["source_diagnostics"] and volume.sessionId() in service.errors, broken
    assert pixels[32, 32, 0] > pixels[32, 32, 1] and pixels[32, 32, 2] > pixels[32, 32, 1], pixels[32, 32]
    attribute.parm("attribute").set("density")
    fixed, pixels = ready("repair volume field during live preview")
    assert not fixed["source_diagnostics"] and volume.sessionId() not in service.errors, fixed
    assert 0.12 > pixels[32, 32, 1] > pixels[32, 32, 2] > pixels[32, 32, 0] > 0, pixels[32, 32]
    volume.node("volume_output").bypass(True)
    fallback, pixels = ready("broken volume graph remains a density-based cloud")
    assert volume.sessionId() in service.errors and pixels[32, 32, 0] > pixels[32, 32, 1], service.diagnostics()
    volume.node("volume_output").bypass(False)
    fixed, _ = ready("repair output graph clears preview warning")
    assert volume.sessionId() not in service.errors and not fixed["source_diagnostics"]
    # Switching domains uses one worker and retires unused scene resources.
    retained = {}
    for cycle in range(3):
        for node in (surface, world, volume):
            request(node)
            status, pixels = ready("domain switch " + str(cycle) + " " + node.type().name())
            assert status["renderer_pid"] == pid and status["geometry_resources"] == status["object_resources"] == 1, (
                status
            )
            if cycle == 1:
                retained[service.domain] = status["owned_geometry_bytes"]
            if cycle == 2:
                assert retained[service.domain] == status["owned_geometry_bytes"], (retained, status)
    assert service.diagnostics()["native"]["images"] <= 32 and len(service.watches) == 3
    report.update(result="passed", renderer_pid=pid, owned_resource_plateau=retained, no_scene_objects=True)
except BaseException:
    report.update(result="failed", error=traceback.format_exc(), final=service.diagnostics())
    raise
finally:
    service.close(wait=True)
    for node in nodes:
        node.destroy()
    assert _native.thumbnail_stats()["images"] == 0
    (destination / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (destination.parent / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], events=len(report["events"]), report=str(destination / "report.json"))))
