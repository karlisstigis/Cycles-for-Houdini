# SPDX-License-Identifier: Apache-2.0
"""Packed leaf batches preserve mixed geometry, bindings, identities and aliases."""

from support.paths import artifacts_root
import argparse
import json
import os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "stage2/packed/batches"
dest.mkdir(parents=True, exist_ok=True)
obj = hou.node("/obj").createNode("geo", "mixed_packed_batch", run_init_scripts=False)
box = obj.createNode("box")
box.parmTuple("size").set((0.35, 0.45, 0.3))
curve = obj.createNode("line")
curve.parm("dist").set(0.45)
place = obj.createNode("xform")
place.setInput(0, curve)
place.setParms(dict(tx=0.55, ty=-0.225))
width = obj.createNode("attribwrangle")
width.setInput(0, place)
width.parm("snippet").set("f@width=.08;")
point = obj.createNode("add")
point.parm("points").set(1)
point.setParms(dict(usept0=1, pt0x=-0.55))
radius = obj.createNode("attribwrangle")
radius.setInput(0, point)
radius.parm("snippet").set("f@pscale=.09;")
mixed = obj.createNode("merge")
mixed.setInput(0, box)
mixed.setInput(1, width)
mixed.setInput(2, radius)
targets = obj.createNode("grid")
targets.setParms(dict(rows=2, cols=2, orient=0))
targets.parmTuple("size").set((2.8, 1.2))
copies = obj.createNode("copytopoints")
copies.setInput(0, mixed)
copies.setInput(1, targets)
copies.parm("pack").set(1)
copies.setDisplayFlag(True)
copies.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam", "batch_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=5))
red = ch.create_material(name="batch_red", shader="emission")
# The default this test was written for (new nodes now take Blender's).
red.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
green = ch.create_material(name="batch_green", shader="emission")
# The default this test was written for (new nodes now take Blender's).
green.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
for builder, color in ((red, (0.8, 0.1, 0.05)), (green, (0.05, 0.7, 0.1))):
    builder.node("surface_shader").parmTuple("color").set(color)
    builder.node("surface_shader").parm("strength").set(1)
obj.parm("shop_materialpath").set(red.path())
assignment = obj.createNode("attribwrangle", "per_instance_material")
assignment.setInput(0, copies)
assignment.parm("class").set(1)
assignment.addSpareParmTuple(
    hou.StringParmTemplate("shop_materialpath1", "Instance Material", 1, default_value=(green.path(),))
)
# A raw authored binding remains present when its target is deleted. Material
# SOP validates operator paths itself and can omit an already missing target.
assignment.parm("snippet").set('s@shop_materialpath = (@primnum%2) ? chs("shop_materialpath1") : "";')
checks = []


def request(sop=copies, native=False):
    return ch.geometry_request(obj, sop, material="plain", material_mode="native" if native else "uniform")


def scene(sop=copies, native=False):
    return dict(
        geometry=[request(sop, native)],
        materials=[dict(key="plain", kind="emission", color=(0.2, 0.6, 0.3), strength=1)],
        material_resolver=native_scene.resolve_materials,
        camera_path=camera.path(),
        background=(0, 0, 0),
        cryptomatte=dict(object=True, material=True, asset=True, levels=4),
    )


def rgb(frame):
    return np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(frame["height"], frame["width"], 4)[
        ..., :3
    ]


def crypto(session):
    metadata = session.output_metadata()
    result = {}
    for key, value in metadata.items():
        if key.startswith("cryptomatte/") and key.endswith("/name"):
            result[value] = json.loads(metadata[key[:-4] + "manifest"])
    return result


def covered_ids(frame, family):
    result = set()
    for name, plane in frame["planes"].items():
        if not name.startswith(family):
            continue
        values = np.frombuffer(plane["pixels"], np.float32).reshape(-1, 4)
        for offset in (0, 2):
            result.update(values[values[:, offset + 1] > 1e-4, offset].copy().view(np.uint32).tolist())
    return result


def id_bits(encoded):
    value = int(encoded, 16)
    return value ^ (1 << 23) if ((value >> 23) & 255) in (0, 255) else value


with ch.Session(device=device, width=320, height=192, samples=128, execution="worker", interactive=False) as session:
    baseline = session.wait(session.apply(scene()))
    status = session.status()
    assert status["geometry_resources"] == 3 and status["object_resources"] == 12, status
    assert status["points"] == 4 and status["curves"] == 4 and status["packed_instances"] == 12, status
    assert status["index_instance_ids"] == 4 and not status["source_diagnostics"], status
    assert status["index_primitive_ids"] == 8, status
    assert (rgb(baseline)[..., 1] > 0.4).sum() > 1000
    keys = session.native_object_keys()
    assert len(keys) == 12 and len(set(keys)) == 12
    checks.append(
        "Four native packed copies retain all mesh/point/hair components and instance-based primitive counters without IDs"
    )
    # An independent native unpacked route must draw the same world geometry.
    for scale in ((1, 1, 1), (-1.1, 0.8, 1.2)):
        obj.parmTuple("s").set(scale)
        copies.parm("pack").set(1)
        packed = session.wait(session.apply(scene()))
        copies.parm("pack").set(0)
        unpacked = session.wait(session.apply(scene()))
        difference = float(np.abs(rgb(packed) - rgb(unpacked)).mean())
        assert difference < 0.002, (scale, difference)
        checks.append(
            dict(
                check="Packed versus independently unpacked native mixed geometry",
                scale=scale,
                mean_difference=difference,
            )
        )
    obj.parmTuple("s").set((1, 1, 1))
    copies.parm("pack").set(1)
    baseline = session.wait(session.apply(scene()))
    conversions = session.status()["geometry_conversions"]
    camera.parm("tx").set(0.05)
    session.wait(session.apply(dict(camera_path=camera.path())))
    assert session.status()["geometry_conversions"] == conversions
    camera.parm("tx").set(0)
    obj.parm("tx").set(0.05)
    session.wait(session.apply(scene()))
    assert session.status()["geometry_conversions"] == conversions
    obj.parm("tx").set(0)
    session.wait(session.apply(scene()))
    checks.append("Camera and OBJ transforms retain all three prototype resources")
    # Authored packed bindings split only their necessary geometry variants.
    good = session.wait(session.apply(scene(assignment, True)))
    status = session.status()
    assert status["geometry_resources"] == 6 and status["object_resources"] == 12, status
    pixels = rgb(good)
    assert (pixels[..., 0] > 0.6).sum() > 300 and (pixels[..., 1] > 0.6).sum() > 300
    good_crypto = crypto(session)
    names = {obj.path() + "/packed:index:" + str(n) for n in range(4)}
    assert set(good_crypto["CryptoObject"]) - {"cycles_houdini/environment"} == names, good_crypto
    assert set(good_crypto["CryptoAsset"]) - {""} == {obj.path()}, good_crypto
    assert {red.path(), green.path()} <= good_crypto["CryptoMaterial"].keys(), good_crypto
    assert {id_bits(good_crypto["CryptoObject"][name]) for name in names} <= covered_ids(good, "CryptoObject")
    assignment.parm("shop_materialpath1").set("/mat/missing_batch_material")
    broken = session.wait(session.apply(scene(assignment, True)))
    status = session.status()
    assert status["object_resources"] == 12 and status["geometry_resources"] == 6 and not status["error"], status
    assert any(d["code"] == "material.missing" for d in status["source_diagnostics"]), status
    broken_crypto = crypto(session)
    assert (
        broken_crypto["CryptoObject"] == good_crypto["CryptoObject"]
        and broken_crypto["CryptoAsset"] == good_crypto["CryptoAsset"]
    )
    assert {red.path(), "/mat/missing_batch_material"} <= broken_crypto["CryptoMaterial"].keys(), broken_crypto
    missing_id = id_bits(broken_crypto["CryptoMaterial"]["/mat/missing_batch_material"])
    assert missing_id in covered_ids(broken, "CryptoMaterial")
    pixels = rgb(broken)
    assert ((pixels[..., 0] > 0.8) & (pixels[..., 2] > 0.8)).sum() > 300
    assert ((pixels[..., 0] > 0.6) & (pixels[..., 2] < 0.2)).sum() > 300
    assignment.parm("shop_materialpath1").set(green.path())
    repaired = session.wait(session.apply(scene(assignment, True)))
    assert not session.status()["source_diagnostics"] and np.allclose(rgb(good), rgb(repaired), atol=1e-6)
    repaired_crypto = crypto(session)
    assert (
        repaired_crypto["CryptoObject"] == good_crypto["CryptoObject"]
        and repaired_crypto["CryptoAsset"] == good_crypto["CryptoAsset"]
    )
    assert all(
        repaired_crypto["CryptoMaterial"][name] == good_crypto["CryptoMaterial"][name]
        for name in (red.path(), green.path())
    )
    assert missing_id not in covered_ids(repaired, "CryptoMaterial")
    checks.append("Per-instance native materials and local missing-material repair retain meshes, hair and points")
    checks.append(
        "Each mixed packed instance shares one object matte across its components; asset and material identities survive local fallback and repair"
    )
    # Explicit logical aliases must remove every deferred component, including
    # after a camera-only commit that no longer rescans the alias sets.
    r = request(assignment, True)
    camera.parm("tx").set(0.01)
    session.wait(session.apply(dict(camera_path=camera.path())))
    session.wait(session.apply(dict(deleted_objects=[r["object_key"]], deleted_geometry=[r["geometry_key"]])))
    assert session.status()["object_resources"] == session.status()["geometry_resources"] == 0
    camera.parm("tx").set(0)
    session.wait(session.apply(scene(assignment, True)))
    assert session.status()["object_resources"] == 12 and session.status()["geometry_resources"] == 6
    checks.append("Logical object/geometry deletion and recreation retain correct alias ownership")
    session.write_exr(dest / ("mixed-" + ("cpu" if device == "CPU" else "optix") + ".exr"))
assert session.status()["owned_geometry_bytes"] == session.status()["device_allocation_bytes"] == 0
hou.hipFile.save(str(dest / "packed-batches.hiplc"))
report = dict(
    result="passed",
    device=device,
    checks=checks,
    scope="No-blur deferred packed leaf expansion; packed shutter sampling retains its existing path and separate tests.",
)
(dest / "houdini-packed-batch-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
