# SPDX-License-Identifier: Apache-2.0
"""Convert to Cycles Material in Houdini: Principled Shader, MaterialX Standard Surface and OpenPBR.

Builds its own sources in /mat, converts them through the public call and the
node-menu hooks, and checks the new Cycles nodes, their values, texture paths,
colour spaces, links, the report comment, that the sources are untouched and
that the converted graphs extract for the renderer.
"""

from support.paths import artifacts_root

from pathlib import Path
import json
import tempfile

import hou
import cycles_houdini as ch

root = Path(__file__).resolve().parents[1]
ch.__path__.insert(0, str(root / "houdini/python3.13libs/cycles_houdini"))
from cycles_houdini import convert, materials  # noqa: E402
import voptoolutils  # noqa: E402

destination = artifacts_root() / "convert-material"
destination.mkdir(parents=True, exist_ok=True)
report = dict(result="running", checks=[])
temporary = Path(tempfile.mkdtemp(prefix="cycles_convert_"))
mat = hou.node("/mat")


def check(name, condition, detail=None):
    report["checks"].append(dict(name=name, passed=bool(condition), detail=detail))
    assert condition, (name, detail)


def snapshot(node):
    values = {}
    for parm in node.parms():
        try:
            values[parm.name()] = (
                parm.unexpandedString() if parm.parmTemplate().type() == hou.parmTemplateType.String else parm.eval()
            )
        except Exception:
            values[parm.name()] = parm.eval()
    return values


def source(node, input):
    for connection in node.inputConnections():
        if node.inputNames()[connection.inputIndex()] == input:
            upstream = connection.inputNode()
            return upstream, upstream.outputNames()[connection.outputIndex()]
    return None, None


def close(a, b, tolerance=1e-5):
    if isinstance(a, (tuple, list)):
        return len(a) == len(b) and all(abs(x - y) <= tolerance for x, y in zip(a, b))
    return abs(a - b) <= tolerance


try:
    # -------------------------------------------------------------- Principled Shader
    ps = mat.createNode("principledshader::2.0", "brick")
    ps.setPosition(hou.Vector2(0, 0))
    ps.setParms(
        dict(
            basecolor_usePointColor=0,
            rough=0.6,
            metallic=0.2,
            ior=1.45,
            reflect=0.8,
            coat=0.3,
            coatrough=0.1,
            emitint=0.0,
            basecolor_useTexture=1,
            basecolor_texture="$HIP/tex/brick_diffuse.<UDIM>.png",
            basecolor_textureColorSpace="sRGB",
            rough_useTexture=1,
            rough_texture="$JOB/tex/brick_rough.png",
            rough_textureColorSpace="linear",
            rough_monoChannel=2,
            baseBumpAndNormal_enable=1,
            baseBumpAndNormal_type="normal",
            baseNormal_texture=str(temporary / "brick_normal.png").replace("\\", "/"),
            baseNormal_flipY=1,
            dispersion=0.2,
        )
    )
    ps.parmTuple("basecolor").set((0.9, 0.8, 0.7))
    ps.parm("metallic").setExpression("0.1 + 0.1 * $F", hou.exprLanguage.Hscript)
    before = snapshot(ps)
    obj = hou.node("/obj").createNode("geo", "assigned")
    obj.parm("shop_materialpath").set(ps.path())

    check("menu offers the Principled Shader", convert.menu_visible({"node": ps}))
    null = mat.createNode("null", "not_a_material") if mat.childTypeCategory().nodeType("null") else None
    cycles_material = materials.create_material(parent=mat, name="already_cycles")
    check("menu hidden on a Cycles material", not convert.menu_visible({"node": cycles_material}))
    if null is not None:
        check("menu hidden on other nodes", not convert.menu_visible({"node": null}))

    [result] = convert.convert([ps])
    builder = result.material
    check(
        "builder type and name",
        builder.type().name() == "cycles::material::1" and builder.name() == "brick_cycles",
        builder.path(),
    )
    check(
        "builder sits beside the source",
        builder.parent() == mat and abs(builder.position()[0] - ps.position()[0] - 3.0) < 1e-6,
    )
    check("source unchanged", snapshot(ps) == before)
    check("assignment unchanged", obj.parm("shop_materialpath").eval() == ps.path())
    surface = builder.node("surface_shader")
    check("roughness 0.6 times texture green", source(surface, "roughness")[0].type().name() == "cycles::math::1")
    rough_math = source(surface, "roughness")[0]
    check("roughness constant", close(rough_math.parm("value1").eval(), 0.6))
    channel, output = source(rough_math, "value2")
    check("green channel", channel.type().name() == "cycles::separate_color::1" and output == "g")
    rough_image = source(channel, "color")[0]
    check("roughness path kept", rough_image.parm("filename").unexpandedString() == "$JOB/tex/brick_rough.png")
    check("roughness read as data", rough_image.parm("colorspace").eval() == "data")
    base_mix = source(surface, "base_color")[0]
    check(
        "base colour tint",
        close(base_mix.parmTuple("b").eval(), (0.9, 0.8, 0.7)) and base_mix.parm("blend_type").eval() == "multiply",
    )
    base_image = source(base_mix, "a")[0]
    check("UDIM path kept", base_image.parm("filename").unexpandedString() == "$HIP/tex/brick_diffuse.<UDIM>.png")
    space = base_image.parm("colorspace").eval()
    check("sRGB colour space resolved", space in base_image.parm("colorspace").menuItems() and "sRGB" in space, space)
    normal_map = source(surface, "normal")[0]
    check(
        "normal map",
        normal_map.type().name() == "cycles::normal_map::1" and normal_map.parm("space").eval() == "tangent",
    )
    check("metallic expression value copied", close(surface.parm("metallic").eval(), 0.2))
    check(
        "ior / reflectivity / coat",
        close(surface.parm("ior").eval(), 1.45)
        and close(surface.parm("specular_ior_level").eval(), 0.4)
        and close(surface.parm("coat_weight").eval(), 0.3),
    )
    check(
        "report lists dispersion and the expression",
        "Dispersion" in result.message and "Metallic" in result.message,
        result.message,
    )
    check(
        "report shown as node comment",
        builder.comment() == result.message and builder.isGenericFlagSet(hou.nodeFlag.DisplayComment),
    )
    graph = materials.material_graph(builder)
    check("converted graph extracts", graph["surface"] is not None and len(graph["nodes"]) >= 6, len(graph["nodes"]))

    # -------------------------------------------------------------- glass with absorption
    glass = mat.createNode("principledshader::2.0", "glass")
    glass.setParms(dict(basecolor_usePointColor=0, transparency=1.0, rough=0.0, transdist=0.5))
    glass.parmTuple("transcolor").set((0.4, 0.8, 1.0))
    [glass_result] = convert.convert([glass])
    output = glass_result.material.node("surface_output")
    volume = source(output, "volume")[0]
    check("glass absorption volume", volume is not None and volume.type().name() == "cycles::absorption_volume::1")
    check(
        "clean conversion has no comment",
        glass_result.message == "" and glass_result.material.comment() == "",
        glass_result.message,
    )
    materials.material_graph(glass_result.material)

    # -------------------------------------------------------------- MaterialX Standard Surface (Karma Material Builder)
    builder_net = mat.createNode("subnet", "karma_wood")
    voptoolutils._setupMtlXBuilderSubnet(
        subnet_node=builder_net,
        name="karma_wood",
        mask=voptoolutils.KARMAMTLX_TAB_MASK,
        folder_label="Karma Material Builder",
        render_context="kma",
    )
    standard = builder_net.node("mtlxstandard_surface")
    standard.setParms(dict(metalness=0.0, specular_roughness=0.35, coat=0.5, specular_IOR=1.4))
    image = builder_net.createNode("mtlximage", "albedo")
    image.parm("file").set("$HIP/tex/wood_<UDIM>.jpg")
    image.parm("filecolorspace").set("srgb_texture")
    standard.setNamedInput("base_color", image, "out")
    normal_image = builder_net.createNode("mtlximage", "normal_image")
    normal_image.parm("signature").set("vector3")
    normal_image.parm("file").set("$HIP/tex/wood_normal.png")
    normal = builder_net.createNode("mtlxnormalmap", "normalmap")
    normal.setNamedInput("in", normal_image, "out")
    standard.setNamedInput("normal", normal, "out")
    noise = builder_net.createNode("mtlxnoise2d", "noise")
    standard.setNamedInput("metalness", noise, "out")
    check("menu offers a Karma Material Builder", convert.menu_visible({"node": builder_net}))
    check("menu offers its Standard Surface", convert.menu_visible({"node": standard}))
    [mx] = convert.convert([builder_net])
    mx_surface = mx.material.node("surface_shader")
    check("MaterialX builder placed in /mat", mx.material.parent() == mat and mx.material.name() == "karma_wood_cycles")
    check(
        "standard surface values",
        close(mx_surface.parm("roughness").eval(), 0.35)
        and close(mx_surface.parm("coat_weight").eval(), 0.5)
        and close(mx_surface.parm("ior").eval(), 1.4),
    )
    mx_image = source(mx_surface, "base_color")[0]
    check(
        "mtlximage -> Image Texture",
        mx_image.type().name() == "cycles::image_texture::1"
        and mx_image.parm("filename").unexpandedString() == "$HIP/tex/wood_<UDIM>.jpg",
    )
    check("srgb_texture resolved", "sRGB" in mx_image.parm("colorspace").eval(), mx_image.parm("colorspace").eval())
    mx_normal = source(mx_surface, "normal")[0]
    check("mtlxnormalmap -> Normal Map", mx_normal.type().name() == "cycles::normal_map::1")
    check("normal image is data", source(mx_normal, "color")[0].parm("colorspace").eval() == "data")
    check("noise reported", "noise" in mx.message and "Metalness" in mx.message, mx.message)
    materials.material_graph(mx.material)

    # -------------------------------------------------------------- OpenPBR in a MaterialX builder
    pbr_net = mat.createNode("subnet", "pbr_film")
    # A hand-made MaterialX subnet: OpenPBR -> Surface Material (no suboutput).
    pbr = pbr_net.createNode("mtlxopen_pbr_surface", "openpbr")
    surface_material = pbr_net.createNode("mtlxsurfacematerial", "material")
    surface_material.setNamedInput("surfaceshader", pbr, "out")
    pbr.setParms(dict(thin_film_weight=1.0, thin_film_thickness=0.3, fuzz_weight=0.4, emission_luminance=2.0))
    pbr.parmTuple("base_color").set((0.1, 0.3, 0.6))
    [op] = convert.convert([pbr])
    op_surface = op.material.node("surface_shader")
    check("OpenPBR named after its builder", op.material.name() == "pbr_film_cycles", op.material.name())
    check("OpenPBR thin film nm", close(op_surface.parm("thin_film_thickness").eval(), 300.0))
    check("OpenPBR fuzz -> sheen", close(op_surface.parm("sheen_weight").eval(), 0.4))
    check("OpenPBR emission", close(op_surface.parm("emission_strength").eval(), 2.0))
    check("OpenPBR base colour", close(op_surface.parmTuple("base_color").eval(), (0.1, 0.3, 0.6)))
    materials.material_graph(op.material)

    # -------------------------------------------------------------- a colour range (Vector Map Range)
    range_net = mat.createNode("subnet", "karma_range")
    voptoolutils._setupMtlXBuilderSubnet(
        subnet_node=range_net,
        name="karma_range",
        mask=voptoolutils.KARMAMTLX_TAB_MASK,
        folder_label="Karma Material Builder",
        render_context="kma",
    )
    range_image = range_net.createNode("mtlximage", "albedo")
    range_image.parm("file").set("$HIP/tex/albedo.png")
    color_range = range_net.createNode("mtlxrange", "range")
    # A colour with scalar ranges (color3FA); per-channel ranges are reported, not converted.
    color_range.parm("signature").set("color3FA")
    color_range.parm("doclamp").set(1)
    color_range.setNamedInput("in", range_image, "out")
    range_net.node("mtlxstandard_surface").setNamedInput("base_color", color_range, "out")
    [ranged] = convert.convert([range_net])
    vector_ranges = [n for n in ranged.material.children() if n.type().name() == "cycles::vector_map_range::1"]
    check(
        "colour range -> Vector Map Range with its clamp (use_clamp)",
        sorted(n.parm("use_clamp").eval() for n in vector_ranges) == [0, 1],
        ([(n.name(), n.parm("use_clamp").eval()) for n in vector_ranges], ranged.message),
    )
    materials.material_graph(ranged.material)

    # Every parameter a converter plan sets exists on its native node (plan.add keywords).
    import ast

    missing = []
    for module in ("materialx.py", "principled.py", "plan.py"):
        tree = ast.parse((root / "houdini/python3.13libs/cycles_houdini/convert" / module).read_text("utf-8"))
        for call in ast.walk(tree):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "add"
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and isinstance(call.args[0].value, str)
            ):
                continue
            kind = hou.nodeType(hou.vopNodeTypeCategory(), "cycles::" + call.args[0].value + "::1")
            names = {t.name() for t in kind.parmTemplateGroup().entriesWithoutFolders()} if kind else set()
            for keyword in call.keywords:
                if keyword.arg and keyword.arg not in names:
                    missing.append(f"{module}:{call.lineno} {call.args[0].value}.{keyword.arg}")
    check("converter plans set only existing native parameters", not missing, missing)

    # -------------------------------------------------------------- several at once, through the menu hook
    hou.clearAllSelected()
    ps.setSelected(True)
    glass.setSelected(True)
    results = convert.convert_selected({"node": ps})
    check(
        "menu converts the selection",
        sorted(r.material.name() for r in results) == ["brick_cycles1", "glass_cycles1"],
        [r.material.name() for r in results],
    )
    if hou.undos.areEnabled():
        hou.undos.performUndo()
        check(
            "one undo removes the whole conversion",
            mat.node("brick_cycles1") is None and mat.node("glass_cycles1") is None,
        )
    report["result"] = "passed"
except Exception:
    import traceback

    report["result"] = "failed"
    report["error"] = traceback.format_exc()
    raise
finally:
    (destination / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(dict(result=report["result"], checks=len(report["checks"]), error=report.get("error"))))
