# SPDX-License-Identifier: Apache-2.0
"""Dense scalar volume ownership and rendering against Houdini's VDB conversion."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
import argparse, json, math, os, time, traceback
from pathlib import Path
import hou, numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene, registry

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
dest = artifacts_root() / "stage2/volumes/dense"
dest.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=device, checks=[])
obj = hou.node("/obj").createNode("geo", "dense_volume", run_init_scripts=False)
volume = obj.createNode("volume", "native_density")
volume.setParms(dict(name="density", initialval1=0.35, uniformsamples=0, divsx=18, divsy=14, divsz=10))
volume.parmTuple("size").set((2, 1.5, 1))
transform = obj.createNode("xform", "field_transform")
transform.setInput(0, volume)
transform.setParms(dict(tx=0.2, ty=-0.1, rz=15, sx=1.2, sy=0.8, sz=1.1))
transform.setDisplayFlag(True)
transform.setRenderFlag(True)
converted = obj.createNode("convertvdb", "independent_native_reference")
converted.setInput(0, transform)
converted.parm("conversion").set("vdb")
converted.parm("vdbclass").set("fog")
material = ch.create_material(name="dense_absorption", shader="absorption_volume")
material.node("volume_shader").parmTuple("color").set((0, 0, 0))
obj.parm("shop_materialpath").set(material.path())
camera = hou.node("/obj").createNode("cam", "dense_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
rop = hou.node("/out").createNode("cycles_ipr", "dense_volume")
set_rop_devices(rop, device)
rop.setParms(
    dict(
        source_mode="objects",
        vobject=obj.path(),
        camera=camera.path(),
        resx=96,
        resy=64,
        samples=8192,
        ipr_samples=8192,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        use_denoise=0,
        ipr_use_denoise=0,
        allowmotionblur=0,
        use_material_assignments=1,
    )
)
rop.parmTuple("background").set((1, 1, 1))
entry = registry.for_rop(rop, native_scene.configuration(rop))


def pixels(frame):
    plane = frame["planes"]["combined"]
    return np.frombuffer(plane["pixels"], np.float32).reshape(64, 96, plane["channels"])[:, :, :3].copy()


def render(label):
    frame = entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
    status = entry.session.status()
    image = pixels(frame)
    assert np.isfinite(image).all() and status["state"] != "failed" and not status["source_diagnostics"], status
    assert (
        status["geometry_resources"] == status["object_resources"] == 1
        and status["dense_field_conversions"] > 0
        and status["retained_dense_voxel_bytes"] > 0
    ), status
    assert 0.2 < float(image[32, 48, 0]) < 0.95 and float(image[0, 0, 0]) > 0.999, image[32, 48]
    # Interior rays cross 1.1 scene units of constant density. This Beer-Lambert
    # reference does not use either conversion or the render engine.
    transmittance = math.exp(-volume.evalParm("initialval1") * 1.1)
    observed = float(image[28:36, 44:52, 0].mean())
    assert abs(observed - transmittance) < 0.003, (label, observed, transmittance)
    report["checks"].append(
        dict(
            label=label,
            status=status,
            centre=image[32, 48].tolist(),
            analytic_transmittance=transmittance,
            observed_transmittance=observed,
        )
    )
    return image, status


try:
    # Retained source handles must survive real recooks, transform/name edits
    # and restoration while subsequent captures continue to use the same SOP.
    lease = ch._native.preserve_volume(transform.path(), hou.time())
    owned = ch._native.convert_preserved_volume(lease)
    field = owned["fields"][0]
    primitive = transform.geometry().prims()[0]
    assert field["active_voxels"] == 18 * 14 * 10 and abs(field["voxel_zero"][0] - 0.35) < 1e-6, field
    assert np.max(np.abs(np.array(field["origin"]) - np.array(primitive.indexToPos((0, 0, 0))))) < 1e-6, field
    assert np.max(np.abs(np.array(field["voxel_size"]) - np.array(primitive.voxelSize()))) < 1e-6, field
    report["ownership"] = owned
    dense, status = render("Native dense volume renders directly")
    for value in (0.7, 0.2, 0.35):
        volume.parm("initialval1").set(value)
        transform.geometry()
        assert ch._native.convert_preserved_volume(lease)["fingerprint"] == owned["fingerprint"], (
            "Retained voxels changed after a native recook"
        )
        image, updated = render("Density " + str(value))
        assert updated["dense_field_conversions"] > status["dense_field_conversions"], updated
        status = updated
    del lease
    converted.setDisplayFlag(True)
    converted.setRenderFlag(True)
    with registry.session_from_configuration(native_scene.configuration(rop), interactive=False) as reference:
        expected = pixels(reference.wait(reference.apply(native_scene.capture(rop), at_time=hou.time())))
    difference = float(np.abs(dense - expected).mean())
    report["native_vdb_difference"] = difference
    if difference >= 0.0012:
        np.savez(dest / "failed-image.npz", dense=dense, native_vdb=expected)
        raise AssertionError(difference)
    transform.setDisplayFlag(True)
    transform.setRenderFlag(True)
    render("Returning to direct dense rendering")
    registry.close_all()
    deadline = time.monotonic() + 15
    while entry.session.status()["state"] != "closed" and time.monotonic() < deadline:
        time.sleep(0.01)
    report["closed"] = entry.session.status()
    assert (
        report["closed"]["state"] == "closed"
        and report["closed"]["device_allocation_bytes"]
        == report["closed"]["owned_geometry_bytes"]
        == report["closed"]["retained_dense_voxel_bytes"]
        == 0
    ), report["closed"]
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (dest / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
