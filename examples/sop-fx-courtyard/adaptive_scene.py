# SPDX-License-Identifier: Apache-2.0
"""Prepare a new showcase copy with separate adaptive turbine and ground.

Existing artist HIPs are inputs only. The same upgrade is used by the latency
benchmark, so the showcase exercises actual object-level renderer subdivision.
"""

import argparse
import json
import os
from pathlib import Path

import hou
import cycles_houdini as ch
from cycles_houdini import object_properties


def split_branch(source_object, branch_name, name):
    branch = source_object.node(branch_name)
    if branch is None:
        raise RuntimeError("Missing showcase branch: " + branch_name)
    merge = source_object.node("MIXED_SOP_CONTENT")
    inputs = [node for node in merge.inputs() if node is not None and node != branch]
    merge.setInput(0, None)
    for index in range(len(merge.inputs())):
        merge.setInput(index, None)
    for index, node in enumerate(inputs):
        merge.setInput(index, node)
    obj = source_object.parent().createNode("geo", name)
    for child in obj.children():
        child.destroy()
    source = obj.createNode("object_merge", "SOURCE_" + name.upper())
    source.parm("objpath1").set(branch.path())
    source.parm("xformtype").set(0)
    obj.setWorldTransform(source_object.worldTransform())
    out = obj.createNode("null", "OUT")
    out.setInput(0, source)
    out.setDisplayFlag(True)
    out.setRenderFlag(True)
    obj.layoutChildren()
    return obj


def upgrade():
    source_object = hou.node("/obj/fx_courtyard")
    if source_object is None:
        raise RuntimeError("This upgrade requires the native SOP courtyard")
    turbine = hou.node("/obj/courtyard_turbine") or split_branch(
        source_object, "SCULPTURE_AND_RINGS", "courtyard_turbine"
    )
    object_properties.install(turbine)
    turbine.setParms(
        dict(
            cycles_subdivision_override=1,
            cycles_subdivision_type="catmull_clark",
            cycles_subdivision_adaptive=1,
            cycles_subdivision_space="pixel",
            cycles_subdivision_pixel_size=1,
        )
    )
    turbine.setComment(
        "Renderer-only Catmull-Clark adaptive subdivision is enabled in Cycles > Subdivision. The SOP cage stays editable. Camera navigation retains tessellation until the mouse is released."
    )
    ground = hou.node("/obj/ground") or split_branch(source_object, "GROUND_MATERIAL", "ground")
    builder = hou.node("/mat/courtyard_ground_noise")
    if builder is None:
        builder = ch.create_material(name="courtyard_ground_noise", shader="principled_bsdf")
        surface = builder.node("surface_shader")
        surface.parmTuple("base_color").set((0.20, 0.18, 0.14))
        surface.parm("roughness").set(0.72)
        uv = builder.createNode("cycles::uvmap::1", "ground_coordinates")
        noise = builder.createNode("cycles::noise_texture::1", "ground_noise")
        noise.setNamedInput("vector", uv, "UV")
        noise.parm("dimensions").set("4D")
        noise.parm("scale").set(6.5)
        noise.parm("detail").set(3)
        noise.parm("roughness").set(0.65)
        w = noise.parm("w") or noise.parm("W")
        if w is None:
            raise RuntimeError("Pinned Noise Texture has no 4D coordinate parameter")
        w.setExpression("$T * 0.35", hou.exprLanguage.Hscript)
        displacement = builder.createNode("cycles::displacement::1", "true_ground_displacement")
        displacement.parm("space").set("object")
        displacement.parm("scale").set(1.1)
        displacement.parm("midlevel").set(0.5)
        displacement.setNamedInput("height", noise, "fac")
        builder.node("surface_output").setNamedInput("displacement", displacement, "displacement")
        builder.parm("cycles_displacement_method").set("both")
        ch.promote_parameter(builder, noise, "scale", name="ground_noise_scale", label="Ground Noise Scale")
        ch.promote_parameter(builder, displacement, "scale", name="ground_relief", label="Ground Displacement Height")
        builder.setComment(
            "True renderer displacement: animated 4D noise over UVs. Height changes the rendered geometry; the Houdini ground SOP remains flat. Set Height to 0 to compare. Noise scale 6.5 produces visible relief across the courtyard."
        )
        builder.layoutChildren()
    output = ground.renderNode()
    binding = ground.node("CYCLES_GROUND_MATERIAL")
    if binding is None:
        binding = ground.createNode("material", "CYCLES_GROUND_MATERIAL")
        binding.setInput(0, output.inputs()[0] if output.type().name() == "null" else output)
        binding.parm("shop_materialpath1").set(builder.path())
        if output.type().name() == "null":
            output.setInput(0, binding)
        else:
            binding.setDisplayFlag(True)
            binding.setRenderFlag(True)
    ground.setComment(
        "Independent ground object. /mat/courtyard_ground_noise controls true animated noise displacement. SOP geometry is deliberately flat."
    )
    rop = hou.node("/out/cycles_sop_courtyard")
    # Object pixel size multiplies the ROP's Viewport/Render dicing rates;
    # subdivision depth and the optional budget are now owned by the ROP.
    rop.setParms(
        dict(
            source_mode="objects",
            vobject="*",
            ipr_dicing_rate=8,
            dicing_rate=2,
            max_subdivisions=6,
            limit_subdivision=1,
            subdivision_limit=1000000,
        )
    )
    hou.node("/obj").layoutChildren()
    ground.layoutChildren()
    return dict(
        turbine=turbine.path(),
        ground=ground.path(),
        material=builder.path(),
        render_sop=turbine.renderNode().path(),
        adaptive_subdivision=True,
        ground_noise_scale=builder.evalParm("ground_noise_scale"),
        ground_relief=builder.evalParm("ground_relief"),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", help="Explicit compatible source HIP; defaults to the generated current showcase")
    args = parser.parse_args()
    root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
    folder = Path(os.environ.get("CYCLES_HOUDINI_TEST_ARTIFACTS", root / "artifacts")) / "examples/sop-fx-courtyard"
    source = Path(args.source) if args.source else folder / "sop-fx-courtyard-current.hiplc"
    hou.hipFile.load(str(source), suppress_save_prompt=True)
    report = upgrade()
    destination = folder / "sop-fx-courtyard-adaptive.hiplc"
    hou.hipFile.save(str(destination))
    report.update(source=str(source), example=str(destination))
    (folder / "adaptive-scene.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
