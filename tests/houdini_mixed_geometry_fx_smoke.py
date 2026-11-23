# SPDX-License-Identifier: Apache-2.0
"""Five native SOP domains, scoped bindings, sharing and atomic failures."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import math
import os
from pathlib import Path
import subprocess

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene, object_properties, passes

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/mixed-geometry"
destination.mkdir(parents=True, exist_ok=True)
builders = {}
colors = {"mesh": (1, 0, 0), "curves": (0, 0, 1), "points": (0, 1, 0), "packed": (1, 0.6, 0)}
for name, color in colors.items():
    material = ch.create_material(name="mixed_fx_" + name, shader="emission")
    material.node("surface_shader").parmTuple("color").set(color)
    material.node("surface_shader").parm("strength").set(1)
    builders[name] = material
medium = ch.create_material(name="mixed_fx_volume", shader="absorption_volume")
medium.node("volume_shader").parmTuple("color").set((0, 0, 0))
medium.node("volume_shader").parm("density").set(1)
obj = hou.node("/obj").createNode("geo", "mixed_fx", run_init_scripts=False)


def python_sop(name, code, input_node=None):
    node = obj.createNode("python", name)
    if input_node:
        node.setInput(0, input_node)
    node.parm("python").set(code)
    return node


mesh = python_sop(
    "quad",
    """import hou
g=hou.pwd().geometry();face=g.createPolygon()
for x,y in [(-3.4,-.4),(-2.6,-.4),(-2.6,.4),(-3.4,.4)]:
    p=g.createPoint();p.setPosition((x,y,0));face.addVertex(p)
""",
)
curve = python_sop(
    "linear_curve",
    """import hou
g=hou.pwd().geometry();g.addAttrib(hou.attribType.Point,'width',.16)
face=g.createPolygon(is_closed=False)
for y in (-.4,.4):
    p=g.createPoint();p.setPosition((-1.5,y,0));face.addVertex(p)
""",
)
points = python_sop(
    "loose_points",
    """import hou
g=hou.pwd().geometry();g.addAttrib(hou.attribType.Point,'pscale',.12)
for y in (-.18,.18):
    p=g.createPoint();p.setPosition((0,y,0))
""",
)


def assign(node, material):
    assigned = obj.createNode("material", "bind_" + node.name())
    assigned.setInput(0, node)
    assigned.parm("shop_materialpath1").set(material.path())
    return assigned


mesh = assign(mesh, builders["mesh"])
curve = assign(curve, builders["curves"])
fixture = destination / "five-domain-density.vdb"
subprocess.run([str(runtime_executable("ch_harness.exe")), "--write-volume-fixture", str(fixture)], check=True)
file = obj.createNode("file")
file.parm("file").set(str(fixture))
volume = obj.createNode("stash", "owned_density")
volume.parm("stash").set(file.geometry())
file.destroy()
fixture.unlink()
shift = obj.createNode("xform", "translated_density")
shift.setInput(0, volume)
shift.parm("tx").set(1.5)
volume = assign(shift, medium)
prototype = obj.createNode("box", "shared_box")
prototype.parmTuple("size").set((0.4, 0.3, 0.4))
prototype = assign(prototype, builders["packed"])
targets = python_sop(
    "stable_targets",
    """import hou
g=hou.pwd().geometry();g.addAttrib(hou.attribType.Point,'id',0)
for i,y in enumerate((-.45,.45)):
    p=g.createPoint();p.setPosition((3,y,0));p.setAttribValue('id',100+i)
""",
)
copies = obj.createNode("copytopoints", "real_instances")
copies.setInput(0, prototype)
copies.setInput(1, targets)
copies.parm("pack").set(1)
packed = python_sop(
    "packed_identities",
    """import hou
g=hou.pwd().geometry();g.addAttrib(hou.attribType.Prim,'id',0)
for i,p in enumerate(g.prims()):p.setAttribValue('id',100+i)
""",
    copies,
)
merge = obj.createNode("merge", "UNPACKED_AND_INSTANCED")
branches = [mesh, curve, points, volume, packed]


def connect(nodes):
    # Merge has unordered inputs; disconnecting one compacts its input list.
    for i in reversed(range(len(merge.inputs()))):
        merge.setInput(i, None)
    for i, node in enumerate(nodes):
        merge.setInput(i, node)


connect(branches)
out = python_sop(
    "detail_fallback",
    "import hou\ng=hou.pwd().geometry()\ng.addAttrib(hou.attribType.Global,'shop_materialpath','')\ng.setGlobalAttribValue('shop_materialpath',%r)"
    % builders["points"].path(),
    merge,
)
camera = hou.node("/obj").createNode("cam", "mixed_fx_camera")
camera.parm("tz").set(3)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(8)
rop = hou.node("/out").createNode("cycles_ipr", "mixed_fx_cycles")
for name, value in dict(
    source_mode="sop",
    soppath=out.path(),
    camera=camera.path(),
    resx=240,
    resy=120,
    samples=512,
    ipr_samples=512,
    adaptive_sampling=0,
    ipr_adaptive_sampling=0,
    use_denoise=0,
    ipr_use_denoise=0,
).items():
    rop.parm(name).set(value)
rop.parmTuple("lightstrength").set((0, 0, 0))
rop.parmTuple("background").set((1, 1, 1))
passes.add(rop, "position")
out.setRenderFlag(True)
assert object_properties.install(obj)
# Curve Shape defaults to Rounded Ribbons (Blender); this fixture is written for linear curves.
obj.parm("cycles_curve_shape").set("linear")


def sample(frame, name, x, y=0, patch=False):
    plane = frame["planes"][name]
    pixels = np.frombuffer(plane["pixels"], np.float32).reshape(120, 240, plane["channels"])
    px = round((x / 8 + 0.5) * 240)
    py = round((y / 4 + 0.5) * 120)
    return pixels[py - 2 : py + 3, px - 2 : px + 3].mean(axis=(0, 1)) if patch else pixels[py, px]


reports = []
for device in ("CPU", ch.default_device()):
    set_rop_devices(rop, device)
    checks = []
    with ch.Session(device=device, width=240, height=120, samples=512, collect_diagnostics=True) as session:

        def capture():
            return session.wait(session.apply(native_scene.capture(rop), reuse_if_unchanged=True))

        frame = capture()
        initial = session.status()
        assert initial["geometry_resources"] == 5 and initial["object_resources"] == 6, initial
        assert initial["points"] == 2 and initial["curves"] == 1 and initial["packed_instances"] == 2, initial
        for kind, x, y in (("mesh", -3, 0), ("curves", -1.5, 0), ("points", 0, 0.18), ("packed", 3, 0.45)):
            assert np.allclose(sample(frame, "combined", x, y)[:3], colors[kind], atol=0.001), (
                kind,
                sample(frame, "combined", x, y),
            )
        transmission = sample(frame, "combined", 1.5, patch=True)[:3]
        assert np.allclose(transmission, math.exp(-9 / 8), atol=0.02), (transmission, math.exp(-9 / 8))
        assert abs(float(sample(frame, "position", 3, 0.45)[2]) - 0.2) < 0.001
        session.write_exr(destination / ("mixed-fx-cpu.exr" if device == "CPU" else "mixed-fx-optix.exr"))
        checks.append("Independent radiance, packed-box front plane and sparse VDB Beer-Lambert extinction in one SOP")
        keys = session.native_object_keys()
        camera.parm("tx").set(0.1)
        capture()
        warm = session.status()
        assert (
            warm["geometry_conversions"] == initial["geometry_conversions"]
            and warm["partition_scans"] == initial["partition_scans"]
        ), warm
        assert (
            warm["material_binding_scans"] == initial["material_binding_scans"] and session.native_object_keys() == keys
        )
        checks.append("Camera edit scans no primitive partitions or material strings and uploads no geometry")
        camera.parm("tx").set(0)
        mesh_source = mesh.inputs()[0]
        mesh_script = mesh_source.evalParm("python")
        before = session.status()["geometry_conversions"]
        mesh_source.parm("python").set(mesh_script.replace("(-3.4,-.4)", "(-3.45,-.4)"))
        capture()
        after = session.status()
        assert after["geometry_conversions"] == before + 1, after
        assert (
            after["geometry_resources"] == 5 and after["object_resources"] == 6 and session.native_object_keys() == keys
        )
        mesh_source.parm("python").set(mesh_script)
        capture()
        assert session.status()["geometry_conversions"] == before + 2, session.status()
        checks.append("Mesh edits retain the sparse VDB tree, curves, particles and shared packed prototype")
        before = session.status()["geometry_conversions"]
        shift.parm("tx").set(1.55)
        capture()
        assert session.status()["geometry_conversions"] == before + 1, session.status()
        shift.parm("tx").set(1.5)
        capture()
        assert session.status()["geometry_conversions"] == before + 2, session.status()
        checks.append("A VDB anchor edit refreshes only the volume component")
        # One Houdini geometry object owns all five domains. Its opt-in controls
        # must remove only the requested component, retaining every other key.
        for kind in ("mesh", "curves", "points", "volume", "packed"):
            before = session.status()
            before_keys = set(session.native_object_keys())
            obj.parm("cycles_render_" + kind).set(0)
            capture()
            disabled = session.status()
            # Mesh controls also apply to meshes inside packed prototypes.
            assert disabled["geometry_resources"] == (3 if kind == "mesh" else 4), (kind, disabled)
            assert set(session.native_object_keys()) < before_keys, (kind, session.native_object_keys())
            assert disabled["geometry_conversions"] == before["geometry_conversions"], (kind, before, disabled)
            obj.parm("cycles_render_" + kind).set(1)
            capture()
            assert session.status()["geometry_resources"] == 5 and set(session.native_object_keys()) == before_keys
        before = session.status()
        obj.parm("cycles_curve_shape").set("ribbon")
        capture()
        ribbon = session.status()
        assert ribbon["geometry_conversions"] == before["geometry_conversions"] + 1, (before, ribbon)
        obj.parm("cycles_curve_shape").set("thick")
        capture()
        assert session.status()["geometry_conversions"] == before["geometry_conversions"] + 2
        obj.parm("cycles_curve_shape").set("linear")
        capture()
        checks.append(
            "Render toggles affect only their geometry types, including packed leaves; curve shape converts only curves"
        )
        # Deleting a grid while retaining its unused point intentionally adds
        # that point to the loose point cloud. Remove it with keep_points=False
        # when a pure missing-field input is intended instead.
        orphan = python_sop(
            "retained_grid_anchor",
            "import hou\ng=hou.pwd().geometry()\ng.deletePrims([p for p in g.prims() if p.type()==hou.primType.VDB],keep_points=True)",
            out,
        )
        rop.parm("soppath").set(orphan.path())
        capture()
        assert session.status()["points"] == 3 and session.status()["geometry_resources"] == 4
        orphan.parm("python").set(orphan.evalParm("python").replace("keep_points=True", "keep_points=False"))
        capture()
        assert session.status()["points"] == 2 and session.status()["geometry_resources"] == 4
        rop.parm("soppath").set(out.path())
        orphan.destroy()
        capture()
        checks.append(
            "Retained orphan grid points join the loose cloud; deleting unused points preserves field-only semantics"
        )
        # Reorder whole domains: primitive zero becomes a VDB, while component
        # identities, shader scope and the shared prototype remain unchanged.
        reordered = [volume, packed, points, curve, mesh]
        connect(reordered)
        capture()
        assert session.native_object_keys() == keys
        assert session.status()["geometry_resources"] == 5 and session.status()["object_resources"] == 6
        checks.append("VDB-first primitive order does not select or suppress another domain")
        # Packed objects are present even though primitive zero is a VDB. Every
        # leaf component must receive the same sampled OBJ transform, while the
        # packed subgroup keeps its stable instance correspondence.
        before = session.status()["geometry_conversions"]
        obj.parm("tx").setExpression("$T * 48", hou.exprLanguage.Hscript)
        rop.parm("allowmotionblur").set(1)
        blurred = capture()
        for kind, x, y, coverage in (
            ("mesh", -3, 0, 0.8),
            ("curves", -1.5, 0, 0.16),
            ("points", 0, 0.18, 0.24),
            ("packed", 3, 0.45, 0.4),
        ):
            measured = sample(blurred, "combined", x, y)[:3]
            # White background occupies the time outside a surface silhouette.
            expected = np.asarray(colors[kind]) * coverage + np.ones(3) * (1 - coverage)
            assert np.allclose(measured, expected, atol=0.05), (kind, measured, expected)
        assert session.status()["geometry_conversions"] == before and session.native_object_keys() == keys
        checks.append("Mixed OBJ transform blur reaches every unpacked and packed component without geometry uploads")
        obj.parm("tx").set(0)
        rop.parm("allowmotionblur").set(0)
        capture()
        # A real particle primitive renders its members only. This keeps the
        # existing explicit-particle policy for an additional loose point.
        particle = obj.createNode("add", "explicit_particles")
        particle.setInput(0, points)
        particle.parm("addparticlesystem").set(1)
        particle.parm("particlegroup").set("0")
        connect([volume, packed, particle, curve, mesh])
        capture()
        assert session.status()["points"] == 1
        checks.append("Explicit particle vertex membership excludes unrelated loose points")
        connect(reordered)
        particle.destroy()
        capture()
        # Remove one component without renaming the remaining ones, then restore.
        before = session.native_object_keys()
        connect([volume, packed, points, mesh])
        capture()
        assert session.status()["curves"] == 0 and session.status()["geometry_resources"] == 4
        assert set(session.native_object_keys()) == {k for k in before if not k.endswith("/part:curves")}
        connect(reordered)
        capture()
        assert session.native_object_keys() == before
        checks.append("Partial deletion retires one component and preserves the remaining identities")
        sphere = obj.createNode("sphere", "unsupported_sphere")
        connect(reordered + [sphere])
        supported = session.native_object_keys()
        frame = capture()
        recovered = session.status()
        assert not recovered["error"] and session.native_object_keys() == supported, recovered
        assert recovered["geometry_resources"] == 5 and recovered["points"] == 2, recovered
        assert any(d["code"] == "geometry.unsupported_primitive" for d in recovered["source_diagnostics"]), recovered
        assert np.isfinite(sample(frame, "combined", -3)).all()
        connect(reordered)
        sphere.destroy()
        capture()
        assert not session.status()["source_diagnostics"]
        checks.append(
            "Unsupported late primitive is omitted locally without turning its vertices into particles; supported geometry renders and repair clears warnings"
        )
        session.wait(session.apply(dict(geometry=[])))
        assert (
            session.status()["geometry_resources"]
            == session.status()["object_resources"]
            == session.status()["partition_cache_bytes"]
            == 0
        )
        checks.append("All geometry and owned partition metadata retire")
    assert (
        session.status()["owned_geometry_bytes"]
        == session.status()["device_allocation_bytes"]
        == session.status()["partition_cache_bytes"]
        == 0
    )
    reports.append(dict(device=device, checks=checks, initial=initial, warm=warm))
    connect(branches)
(destination / "houdini-mixed-geometry-fx-smoke.json").write_text(
    json.dumps(dict(result="passed", devices=reports), indent=2), encoding="utf-8"
)
print(json.dumps(dict(result="passed", checks_per_device=[len(r["checks"]) for r in reports])))
