# SPDX-License-Identifier: Apache-2.0
"""Direct MeshData native material priority, blanket styling and selective recovery."""

from support.paths import artifacts_root
import argparse
import hashlib
import json
from pathlib import Path

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import blender_assets, native_scene, materials
from cycles_houdini.device_selection import set_devices
from blend_direct_fixture import mesh, prototype

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--out", type=Path)
parser.add_argument("--report", type=Path)
parser.add_argument("--device", default="CPU")
parser.add_argument("--execution", choices=("worker", "sync"), default="worker")
args = parser.parse_args()
requested_device = args.device
args.device = next(d["id"] for d in ch.devices() if d["id"] == args.device or d["backend"] == args.device.upper())
if args.out is None:
    args.out = (
        args.report.parent
        if args.report
        else (
            artifacts_root()
            / "tests/blend-direct-styles"
            / args.execution
            / requested_device.replace(":", "_").replace(" ", "_")
        )
    )
args.out.mkdir(parents=True, exist_ok=True)
root = hou.node("/obj").createNode("subnet", "direct_style_test")
obj = root.createNode("geo", "points", run_init_scripts=False)
source = obj.createNode("python", "instance_points")
source.setDisplayFlag(True)
source.setRenderFlag(True)
obj.addSpareParmTuple(hou.StringParmTemplate("shop_materialstylesheet", "Material Style Sheet", 1))
camera = root.createNode("cam", "camera")
camera.setParms(dict(tz=4, projection=1, orthowidth=3))
rop = hou.node("/out").createNode("cycles_ipr", "direct_style_test")
set_devices(rop, args.device)
rop.setParms(
    dict(
        source_mode="objects",
        objectroot=root.path(),
        vobject="points",
        lights="",
        camera=camera.path(),
        resx=32,
        resy=32,
        samples=1,
        ipr_samples=1,
        use_denoise=0,
        ipr_use_denoise=0,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        transparent_background=1,
        use_material_assignments=1,
        nested_dielectrics=1,
        render_threads=2,
    )
)
a = materials.create_material(name="direct_style_a", shader="emission")
# The default this test was written for (new nodes now take Blender's).
a.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
b = materials.create_material(name="direct_style_b", shader="emission")
# The default this test was written for (new nodes now take Blender's).
b.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
for mat, color in ((a, (0.2, 0.3, 0.4)), (b, (0.1, 0.8, 0.2))):
    shader = mat.node("surface_shader")
    shader.parmTuple("color").set(color)
    shader.parm("strength").set(1)
    ch.promote_parameter(mat, shader, "strength", name="power")
asset = args.out.resolve() / "fixture.blend"
asset.write_bytes(b"authored-native-mesh")
reference = asset.as_posix() + "#object=Assigned"
unassigned = asset.as_posix() + "#object=Unassigned"
prepared = {
    reference: prototype("style/assigned", mesh("style/assigned", material=a.path())),
    unassigned: prototype("style/unassigned", mesh("style/unassigned")),
}


def author(value=reference):
    source.parm("python").set(
        "import hou\ng=hou.pwd().geometry()\ng.addAttrib(hou.attribType.Point,'instance','')\ng.addAttrib(hou.attribType.Point,'shop_materialpath','')\ng.addAttrib(hou.attribType.Point,'cycles_dielectric_priority',7)\np=g.createPoint()\np.setAttribValue('instance',"
        + repr(value)
        + ")\np.setAttribValue('shop_materialpath',"
        + repr(b.path())
        + ")\n"
    )


def sheet(overrides, target=None):
    return dict(target={} if target is None else target, overrides=overrides)


source_capsule = prepared[reference]["instances"][0]["mesh"]
source_bytes = ch._native.blend_mesh_nbytes(source_capsule)
checks = []
original = blender_assets.prepare_prototype
original_many = blender_assets.prepare_prototypes
original_warmup = blender_assets.prewarm_prototypes
blender_assets.prepare_prototype = lambda value, frame, **kwargs: prepared[value]


def prepare_many(specs):
    results = []
    for reference, frame, native in specs:
        try:
            results.append(blender_assets.prepare_prototype(reference, frame, native_subdivision=native))
        except Exception as error:
            results.append(error)
    return results


blender_assets.prepare_prototypes = prepare_many
blender_assets.prewarm_prototypes = lambda specs: None
try:
    with ch.Session(
        device=args.device,
        width=32,
        height=32,
        samples=1,
        threads=2,
        execution=args.execution,
        collect_diagnostics=True,
    ) as session:

        def render(label, color, rules=(), warning=False):
            obj.parm("shop_materialstylesheet").set(json.dumps(dict(styles=list(rules))) if rules else "")
            session.wait(session.apply(native_scene.capture(rop)))
            status = session.status()
            assert not status["error"], status
            assert (
                status["dielectric_priority_objects"] == 1
                and status["dielectric_priority_min"] == status["dielectric_priority_max"] == 7
            ), status
            frame = session.frame()
            plane = frame["planes"]["combined"]
            pixels = np.frombuffer(plane["pixels"], np.float32).reshape(
                frame["height"], frame["width"], plane["channels"]
            )
            np.testing.assert_allclose(pixels[16, 16, :3], color, atol=3e-5, rtol=3e-5)
            diagnostics = status["source_diagnostics"]
            assert bool(diagnostics) == warning, diagnostics
            if warning:
                assert any(d["code"] == "material.external_stylesheet" for d in diagnostics), diagnostics
            assert prepared[reference]["instances"][0]["mesh"] is source_capsule
            assert ch._native.blend_mesh_nbytes(source_capsule) == source_bytes
            geometry = {
                key: status[key]
                for key in (
                    "geometry_conversions",
                    "geometry_resources",
                    "owned_geometry_bytes",
                    "mesh_family_resources",
                    "mesh_family_members",
                    "mesh_variant_bindings",
                )
            }
            np.save(args.out / ("frame-%02d.npy" % len(checks)), pixels)
            checks.append(
                dict(
                    label=label,
                    color=pixels[16, 16, :3].tolist(),
                    frame_sha256=hashlib.sha256(pixels.tobytes()).hexdigest(),
                    diagnostics=diagnostics,
                    geometry=geometry,
                )
            )

        author()
        render("Blender primitive slot outranks point material", (0.2, 0.3, 0.4))
        author(unassigned)
        render("Point material fills an unassigned Blender slot", (0.1, 0.8, 0.2))
        author()
        render("Blanket material stylesheet", (0.1, 0.8, 0.2), [sheet(dict(material=dict(name=b.path())))])
        render("Blanket parameter stylesheet", (0.6, 0.9, 1.2), [sheet(dict(materialParameters=dict(power=3)))])
        render(
            "Selective rule never becomes a blanket override",
            (0.1, 0.8, 0.2),
            [
                sheet(dict(material=dict(name=a.path())), dict(group="missing_group")),
                sheet(dict(material=dict(name=b.path()))),
            ],
            True,
        )
        render("Stylesheet repair restores Blender slots", (0.2, 0.3, 0.4))
        # A library edit can replace a slot while reusing the identical source
        # mesh, point geometry, frame and transform from the preceding capture.
        prepared[reference]["instances"][0]["material_paths"] = [b.path()]
        render("Material-only edit on resident mesh", (0.1, 0.8, 0.2))
        left, right = [
            dict(prepared[reference]["instances"][0], key=name, material_paths=[mat.path()])
            for name, mat in (("left", a), ("right", b))
        ]
        left["transform"] = list(left["transform"])
        right["transform"] = list(right["transform"])
        left["transform"][3], right["transform"][3] = -0.7, 0.7
        prepared[reference]["instances"] = [left, right]
        frame = session.wait(session.apply(native_scene.capture(rop)))
        plane = frame["planes"]["combined"]
        rgb = np.frombuffer(plane["pixels"], np.float32).reshape(32, 32, -1)
        np.testing.assert_allclose(rgb[16, 8, :3], (0.2, 0.3, 0.4), atol=3e-5)
        np.testing.assert_allclose(rgb[16, 24, :3], (0.1, 0.8, 0.2), atol=3e-5)
        assert left["mesh"] is right["mesh"] is source_capsule
        assert ch._native.blend_mesh_nbytes(source_capsule) == source_bytes
        np.save(args.out / ("frame-%02d.npy" % len(checks)), rgb)
        checks.append(
            dict(
                label="Simultaneous palettes on shared mesh",
                frame_sha256=hashlib.sha256(rgb.tobytes()).hexdigest(),
                source_capsule_bytes=source_bytes,
                status=session.status(),
            )
        )
        owned_pixels = bytes(plane["pixels"])
    assert frame["planes"]["combined"]["pixels"] == owned_pixels
    assert ch._native.blend_mesh_nbytes(source_capsule) == source_bytes
    checks.append(dict(label="Source mesh and published frame owned after session close"))
finally:
    blender_assets.prepare_prototype = original
    blender_assets.prepare_prototypes = original_many
    blender_assets.prewarm_prototypes = original_warmup
report = dict(result="passed", execution=args.execution, device=args.device, checks=checks)
report_path = args.report or args.out / "report.json"
report_path.parent.mkdir(parents=True, exist_ok=True)
report_path.write_text(json.dumps(report, indent=2))
print(json.dumps(report), flush=True)
