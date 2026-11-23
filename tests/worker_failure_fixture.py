# SPDX-License-Identifier: Apache-2.0
"""Generate the worker-failure courtyard using current definitions and test-owned assets."""

from pathlib import Path
import os
import runpy
import subprocess

import hou
import PyOpenColorIO as OCIO
import cycles_houdini as ch
from cycles_houdini import light_properties
from native_test_runtime import runtime_executable


def build(destination):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
    if hou.node("/obj/fx_courtyard") or hou.node("/out/cycles_sop_courtyard"):
        raise RuntimeError("Worker failure fixture requires a fresh scene")
    hou.setFrame(1)
    hou.setFps(24)
    config = OCIO.Config.CreateFromBuiltinConfig("cg-config-latest")
    config.setRole(OCIO.ROLE_SCENE_LINEAR, "Linear Rec.709 (sRGB)")
    config.validate()
    profile = destination / "showcase.ocio"
    profile.write_text(config.serialize(), encoding="utf-8")
    hou.putenv("OCIO", str(profile))
    hou.Color.reloadOCIO()
    ch.synchronize_color_configuration()

    material = runpy.run_path(
        str(root / "examples/sop-fx-courtyard/build_scene.py"), run_name="worker_failure_materials"
    )["material"]
    specs = {
        "brass": ("burnished_brass", (0.43, 0.22, 0.055), 0.85, 0.24),
        "steel": ("blue_steel", (0.035, 0.09, 0.14), 0.78, 0.32),
        "ceramic": ("ivory_ceramic", (0.56, 0.52, 0.43), 0.05, 0.36),
        "grass": ("copper_strands", (0.075, 0.19, 0.13), 0.22, 0.4),
        "stone": ("noise_displaced_stone", (0.2, 0.2, 0.2), 0.03, 0.72),
    }
    materials = {
        key: material("courtyard_" + name, color, metal, roughness)
        for key, (name, color, metal, roughness) in specs.items()
    }
    for key, color, strength in (
        ("glow", (0.03, 0.65, 1.0), 6.0),
        ("hot", (1.0, 0.19, 0.015), 5.0),
        ("sparks", (1.0, 0.4, 0.03), 9.0),
    ):
        materials[key] = material("courtyard_" + key, color, emission=strength)
    materials["glass"] = ch.create_material(name="courtyard_optical_glass", shader="glass_bsdf")
    # The default this test was written for (new nodes now take Blender's).
    materials["glass"].node("surface_shader").setParms(dict(distribution="ggx"))
    glass = materials["glass"].node("surface_shader")
    glass.parmTuple("color").set((0.91, 0.98, 1.0))
    glass.parm("IOR").set(1.46)
    glass.parm("roughness").set(0.025)
    medium = ch.create_material(name="courtyard_procedural_plume", shader="principled_volume")
    medium.node("volume_shader").parm("density").set(0.75)

    obj = hou.node("/obj").createNode("geo", "fx_courtyard", run_init_scripts=False)
    geometry = runpy.run_path(
        str(root / "examples/sop-fx-courtyard/native_geometry.py"), run_name="worker_failure_geometry"
    )["geometry"](obj, materials)
    plume = destination / "procedural-plume.vdb"
    subprocess.run([str(runtime_executable()), "--write-showcase-volume", str(plume)], check=True)
    source = obj.createNode("file", "LOAD_PROCEDURAL_PLUME")
    source.parm("file").set(str(plume))
    embedded = obj.createNode("stash", "EMBEDDED_VDB_PLUME")
    embedded.parm("stash").set(source.geometry())
    source.destroy()
    move = obj.createNode("xform", "PLUME_POSITION")
    move.setInput(0, embedded)
    move.parmTuple("t").set((-1.7, 0.4, -0.35))
    volume = obj.createNode("material", "PLUME_MATERIAL")
    volume.setInput(0, move)
    volume.parm("shop_materialpath1").set(medium.path())
    merged = obj.createNode("merge", "MIXED_SOP_CONTENT")
    for index, branch in enumerate([*geometry["branches"], volume]):
        merged.setInput(index, branch)
    output = obj.createNode("null", "RENDER_COURTYARD")
    output.setInput(0, merged)
    output.setDisplayFlag(True)
    output.setRenderFlag(True)
    errors = {node.path(): node.errors() for node in obj.children() if node.errors()}
    if errors:
        raise RuntimeError("Worker failure fixture cooking failed: " + str(errors))

    camera = hou.node("/obj").createNode("cam", "courtyard_camera")
    position, target = hou.Vector3(10, 7.5, 14), hou.Vector3(0, 2.0, -0.25)
    back = (position - target).normalized()
    right = hou.Vector3(0, 1, 0).cross(back).normalized()
    up = back.cross(right)
    camera.setWorldTransform(hou.Matrix4((*right, 0, *up, 0, *back, 0, *position, 1)))
    camera.setParms(dict(focal=38, aperture=36))
    camera.parmTuple("res").set((960, 540))
    light = hou.node("/obj").createNode(light_properties.LIGHT_TYPE, "courtyard_key_light")
    light.setParms(
        dict(cycles_light_type="point", cycles_light_radius=0, light_exposure=0, normalizearea=1, light_intensity=34000)
    )
    light.parmTuple("t").set((-3.5, 7.0, 4.5))
    light.parmTuple("cycles_light_color").set((1.0, 6500 / 8500, 4500 / 8500))
    rop = hou.node("/out").createNode("cycles_ipr", "cycles_sop_courtyard")
    rop.setParms(
        dict(
            source_mode="objects",
            vobject="fx_courtyard",
            lights="*",
            camera=camera.path(),
            resx=960,
            resy=540,
            use_material_assignments=1,
        )
    )
    rop.parmTuple("background").set((0.025, 0.04, 0.065))
    return dict(
        generator="current native SOP definitions",
        ocio=str(profile),
        volume=str(plume),
        geometry=output.path(),
        rop=rop.path(),
    )
