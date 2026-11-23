# SPDX-License-Identifier: Apache-2.0
"""Independent Blender availability reference, native folders and rendered edits."""

from support.paths import artifacts_root
import argparse, json, os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import _native, network_preview
from shader_bypass_preview_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
args = parser.parse_args()
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "stage3/materials/layout"
dest.mkdir(parents=True, exist_ok=True)
reference = json.loads((root / "tests/fixtures/blender-shader-layouts.json").read_text())
builder = ch.create_material(hou.node("/mat"), "availability_reference")
nodes = {}
checked = 0
for variant in reference["variants"]:
    kind = variant["kind"]
    node = nodes.get(kind)
    if node is None:
        node = nodes[kind] = builder.createNode("cycles::" + kind + "::1", kind)
    node.setParms(variant["settings"])
    node.updateParmStates()
    expected = set(variant["active_inputs"])
    layout = _native.vop_connector_layout(node.sessionId())
    drawn = {entry["name"] for entry in layout if not entry["group"]}
    assert drawn == expected, (kind, variant["settings"], expected, layout)
    activity = _native.vop_input_activity(node.sessionId(), hou.time())
    for name in node.inputNames():
        assert activity[name] == (name in expected), (kind, name, activity)
        parm = node.parmTuple(name)
        if parm is not None:
            if parm.parmTemplate().type() == hou.parmTemplateType.Label:
                assert parm.isHidden(), (kind, name)
            else:
                assert parm.isHidden() == (name not in expected), (kind, variant["settings"], name, parm.isHidden())
    checked += 1

panels = {
    "principled_bsdf": {
        "Diffuse": ["diffuse_roughness"],
        "Subsurface": [
            "subsurface_method",
            "subsurface_weight",
            "subsurface_radius",
            "subsurface_scale",
            "subsurface_ior",
            "subsurface_anisotropy",
        ],
        "Specular": [
            "distribution",
            "specular_ior_level",
            "specular_tint",
            "anisotropic",
            "anisotropic_rotation",
            "tangent",
        ],
        "Transmission": [
            "transmission_weight",
            "thin_wall",
            "transmission_dispersion_scale",
            "transmission_dispersion_abbe_number",
        ],
        "Coat": ["coat_weight", "coat_roughness", "coat_ior", "coat_tint", "coat_normal"],
        "Sheen": ["sheen_weight", "sheen_roughness", "sheen_tint"],
        "Emission": ["emission_color", "emission_strength"],
        "Thin Film": ["thin_film_thickness", "thin_film_ior"],
    },
    "glass_bsdf": {"Thin Film": ["thin_film_thickness", "thin_film_ior"]},
    "metallic_bsdf": {"Thin Film": ["thin_film_thickness", "thin_film_ior"]},
    "volume_coefficients": {
        "Absorption": ["absorption_coeffs"],
        "Scatter": ["phase", "scatter_coeffs", "anisotropy", "IOR", "backscatter", "alpha", "diameter"],
        "Emission": ["emission_coeffs"],
    },
}
for kind, expected in panels.items():
    node = nodes.get(kind) or builder.createNode("cycles::" + kind + "::1", kind)
    folders = [p for p in node.parmTemplateGroup().entries() if isinstance(p, hou.FolderParmTemplate)]
    if folders and folders[0].label() == "Surface":
        assert folders[0].folderType() == hou.folderType.Simple
        groups = [p["label"] for p in _native.vop_connector_layout(node.sessionId()) if p["group"]]
        assert groups[0] == "Surface" and "Other" not in groups, (kind, groups)
        folders = folders[1:]
    assert [p.label() for p in folders] == list(expected), (kind, folders)
    for p in folders:
        assert p.folderType() == hou.folderType.Collapsible, (kind, p)
        assert [s.name() for s in p.parmTemplates()] == expected[p.label()], (kind, p)
    if kind == "principled_bsdf":
        surface = node.parmTemplateGroup().entries()[0]
        assert surface.parmTemplates()[0].name() == "base_color"
        drawn = [p["name"] for p in _native.vop_connector_layout(node.sessionId()) if not p["group"]]
        assert drawn[:6] == ["base_color", "metallic", "roughness", "ior", "alpha", "normal"], drawn
        assert node.parm("subsurface_weight").parmTemplate().label() == "Weight"
        assert node.parm("coat_roughness").parmTemplate().label() == "Roughness"

checks = []
f = Fixture(args.device)
vector = f.builder.createNode("cycles::vector_math::1", "adaptive_vector")
vector.setNamedInput("vector1", f.color, "color")
vector.setNamedInput("vector2", f.texture, "color")
vector.parmTuple("vector3").set((0.13, 0.17, 0.19))
vector.parm("scale").set(2.0)
f.texture.parm("filename").set(str(dest / "missing-dormant.exr"))
first = True
conversions = None
with ch.Session(
    device=args.device, width=64, height=64, samples=8, threads=2, collect_diagnostics=True, use_texture_cache=False
) as session:

    def render(expected, name, warning=False, at_time=None):
        global first, conversions
        graph = ch.material_graph(f.builder, at_time=at_time)
        update = dict(materials=[dict(key="surface", kind="graph", graph=graph)])
        if first:
            update.update(
                geometry=[ch.geometry_request(f.geo, f.sop, material="surface")],
                camera_path=f.camera.path(),
                background=(0, 0, 0),
            )
            first = False
        frame = session.wait(session.apply(update, at_time=at_time))
        status = session.status()
        assert not status["error"] and status["state"] == "complete", status
        assert bool(status["source_diagnostics"]) == warning, status["source_diagnostics"]
        if conversions is None:
            conversions = status["geometry_conversions"]
        assert status["geometry_conversions"] == conversions, status
        assert status["render_device_ids"] == [args.device], status
        measured = f.measure(frame).tolist()
        if expected is not None:
            f.expect(frame, expected)
        checks.append(dict(name=name, measured=measured, diagnostics=status["source_diagnostics"]))
        return graph

    vector.parm("math_type").set("scale")
    network_preview.connect_to_output(vector)
    graph = render((0.4, 0.8, 1.6), "Scale ignores broken texture on its inactive second vector")
    assert not any(n["definition"] == "cycles/image_texture/1" for n in graph["nodes"])
    assert vector.input(1) == f.texture and vector.inputNames().index("scale") == 3
    vector.parm("math_type").set("round")
    render((0, 0, 1), "Round shows one vector and preserves dormant inputs")
    vector.parm("math_type").set("multiply")
    render(None, "Multiply restores its saved second vector and local diagnostic", True)
    f.texture.parm("filename").set(f.image((0.5, 0.25, 0.125, 1)))
    render((0.1, 0.1, 0.1), "Repairing the second vector restores multiplication")
    vector.parm("math_type").set("scale")
    render((0.4, 0.8, 1.6), "Switching back restores the saved Scale value")
    # Hidden scalar parameters are not translated either, including malformed
    # values which would otherwise trip strict engine graph validation.
    f.math.parm("math_type").set("sine")
    f.math.setNamedInput("value2", f.texture, "color")
    f.math.parm("value3").set(float("nan"))
    f.output.setNamedInput("surface", f.math, "value")
    f.texture.parm("filename").set(str(dest / "another-missing.exr"))
    graph = render((np.sin(0.3),) * 3, "Unary Math ignores dormant links and invalid unused values")
    math = next(n for n in graph["nodes"] if n["definition"] == "cycles/math/1")
    assert "value2" not in math["parameters"] and "value3" not in math["parameters"]
    # A broken subnet on an inactive input must also stay unreachable.
    from cycles_houdini.materials import create_connector

    foreign = ch.create_subnet(f.builder, "irrelevant_broken_subnet")
    create_connector(foreign, "broken", kind="output", type="vector")
    vector.setNamedInput("vector3", foreign, "broken")
    vector.parm("math_type").set("scale")
    network_preview.connect_to_output(vector)
    render((0.4, 0.8, 1.6), "Inactive operation branch does not validate a broken subnet")
    vector.setInput(2, None)
    remap = f.builder.createNode("cycles::map_range::1", "adaptive_range")
    # The default this test was written for (new nodes now take Blender's).
    remap.setParms(dict(clamp=0))
    remap.setParms(dict(range_type="linear", value=0.25))
    remap.setNamedInput("steps", f.texture, "alpha")
    f.output.setNamedInput("surface", remap, "result")
    render((0.25,) * 3, "Linear Map Range ignores a broken Steps input")
    remap.parm("range_type").set("stepped")
    render(None, "Stepped exposes its retained Steps input and diagnostic", True)
    f.texture.parm("filename").set(f.image((0.5, 0.25, 0.125, 4)))
    render((0.25,) * 3, "Repairing Steps restores the stepped remap")
    # Inactive texture coordinate dependencies must not cause UV recovery or
    # invoke a deleted image from a dimension the node does not use.
    noise = f.builder.createNode("cycles::noise_texture::1", "dimension_noise")
    # The default this test was written for (new nodes now take Blender's).
    noise.setParms(dict(scale=1.0))
    noise.setNamedInput("vector", f.texture, "color")
    noise.setParms(dict(dimensions="1D", w=0.37, type="fBM"))
    f.texture.parm("filename").set(str(dest / "missing-coordinate.exr"))
    f.output.setNamedInput("surface", noise, "fac")
    graph = render(None, "1D noise ignores its inactive Vector source")
    assert not any(n["definition"] == "cycles/image_texture/1" for n in graph["nodes"])
    noise.parm("dimensions").set("3D")
    render(None, "3D noise restores the Vector dependency", True)
    f.texture.parm("filename").set(f.image((0.5, 0.25, 0.125, 1)))
    render(None, "Coordinate repair clears the warning")
    hou.setFrame(1)
    vector.parm("math_type").setExpression('ifs($F < 2,"scale","multiply")', hou.exprLanguage.Hscript)
    f.output.setNamedInput("surface", vector, "vector")
    graph = render(
        (0.4, 0.8, 1.6), "Animated operation at frame one ignores its dormant image", at_time=hou.frameToTime(1)
    )
    assert not any(n["definition"] == "cycles/image_texture/1" for n in graph["nodes"])
    graph = render(
        (0.1, 0.1, 0.1),
        "Explicit frame two evaluates operation availability at render time",
        at_time=hou.frameToTime(2),
    )
    assert any(n["definition"] == "cycles/image_texture/1" for n in graph["nodes"]) and hou.frame() == 1

# HIP persistence keeps parameter names, animation and dormant connection IDs.
vector.parm("math_type").set("round")
path = vector.path()
texture_path = f.texture.path()
hip = dest / "shader-layout.hiplc"
hou.hipFile.save(str(hip))
hou.hipFile.load(str(hip), suppress_save_prompt=True, ignore_load_warnings=True)
vector = hou.node(path)
assert vector.input(1).path() == texture_path and not vector.isInputVisible("vector2")
assert vector.evalParm("scale") == 2 and tuple(vector.parmTuple("vector3").eval()) == (0.13, 0.17, 0.19)
vector.parm("math_type").set("multiply")
assert vector.isInputVisible("vector2")
report = dict(
    result="passed",
    device=args.device,
    blender_version=reference["blender_version"],
    blender_commit=reference["blender_commit"],
    availability_variants=checked,
    panels=sum(map(len, panels.values())),
    checks=checks,
    hip=str(hip),
    scope="Native connector/pane state against Blender factory availability, independent rendered values, dormant branch recovery and HIP reload. GUI interaction and physical display are separate.",
)
(dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result="passed", variants=checked, checks=len(checks))))
