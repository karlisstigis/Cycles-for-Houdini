# SPDX-License-Identifier: Apache-2.0
"""Rendered native bypass/default routes and direct Cycles output previews."""

import argparse, json, os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import network_preview
from shader_bypass_preview_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
args = parser.parse_args()
fixture = Fixture(args.device)
checks = []
out = fixture.output
with ch.Session(
    device=args.device, width=64, height=64, samples=8, threads=2, collect_diagnostics=True, use_texture_cache=False
) as session:
    first = True
    conversions = None

    def render(expected, name, warning=False):
        global first, conversions
        graph = ch.material_graph(fixture.builder)
        update = dict(materials=[dict(key="surface", kind="graph", graph=graph)])
        if first:
            update.update(
                geometry=[ch.geometry_request(fixture.geo, fixture.sop, material="surface")],
                camera_path=fixture.camera.path(),
                background=(0, 0, 0),
            )
            first = False
        frame = session.wait(session.apply(update))
        status = session.status()
        assert not status["error"], status
        assert status["render_device_ids"] == [args.device], status["render_device_ids"]
        if conversions is None:
            conversions = status["geometry_conversions"]
        assert status["geometry_conversions"] == conversions
        if warning:
            assert any(d["code"] == "texture.unavailable" for d in status["source_diagnostics"]), status
        else:
            assert not status["source_diagnostics"], status
        measured = fixture.expect(frame, expected) if expected is not None else fixture.measure(frame).tolist()
        checks.append(
            dict(
                name=name, measured=measured, generation=status["generation"], diagnostics=status["source_diagnostics"]
            )
        )
        (fixture.dest / "report.json").write_text(
            json.dumps(dict(result="running", checks=checks), indent=2), encoding="utf-8"
        )
        return graph

    assert out.isInputCompatible(0, fixture.color, 0, True)
    out.setNamedInput("surface", fixture.color, "color")
    render((0.2, 0.4, 0.8), "Color wired to Surface renders at unit emission")
    out.setNamedInput("surface", fixture.value, "value")
    render((0.3, 0.3, 0.3), "Scalar wired to Surface renders grayscale emission")
    mix = fixture.builder.createNode("cycles::mix_closure::1", "preview_mix")
    mix.parm("fac").set(0.0)
    mix.setNamedInput("closure1", fixture.color, "color")
    out.setNamedInput("surface", mix, "closure")
    render((0.2, 0.4, 0.8), "Numeric input to a shader socket uses the same Cycles conversion")
    mix.bypass(True)
    render((0.2, 0.4, 0.8), "Bypassing a shader mix preserves a numeric input preview")
    mix.bypass(False)
    out.setNamedInput("surface", fixture.math, "value")
    render((0.6, 0.6, 0.6), "Active Math evaluates normally")
    fixture.math.bypass(True)
    graph = render((0.3, 0.3, 0.3), "Bypassed Math follows its primary connected value")
    assert not any(n["definition"] == "cycles/math/1" for n in graph["nodes"])
    fixture.math.bypass(False)
    render((0.6, 0.6, 0.6), "Unbypassing Math restores its operation")
    out.setNamedInput("surface", fixture.range, "result")
    render((0.6, 0.6, 0.6), "Active Range remaps the value")
    fixture.range.bypass(True)
    render((0.3, 0.3, 0.3), "Bypassed Range ignores remap bounds")
    fixture.math.setInput(fixture.math.inputNames().index("value1"), None)
    fixture.math.parm("value1").set(0.4)
    fixture.math.bypass(True)
    out.setNamedInput("surface", fixture.math, "value")
    render((0.4, 0.4, 0.4), "Unconnected bypass input retains its authored value")
    fixture.math.setNamedInput("value1", fixture.value, "value")
    fixture.math.setNamedInput("value2", fixture.texture, "alpha")
    graph = render((0.3, 0.3, 0.3), "A broken unused secondary image is not prepared while Math is bypassed")
    assert not any(n["definition"] == "cycles/image_texture/1" for n in graph["nodes"])
    fixture.math.bypass(False)
    render(None, "Unbypassed missing texture remains recoverable", True)
    fixture.texture.parm("filename").set(fixture.image())
    render((0.225, 0.225, 0.225), "Repair restores the original shader without geometry conversion")
    fixture.math.setDebugFlag(True)
    render((0.225, 0.225, 0.225), "Default Houdini Debug executes the node; it is not a preview selector")
    fixture.math.setDebugFlag(False)
    ramp = fixture.builder.createNode("cycles::color_ramp::1", "muted_ramp")
    ramp.parm("factor").set(0.35)
    ramp.bypass(True)
    out.setNamedInput("surface", ramp, "color")
    render((0.35, 0.35, 0.35), "Bypassed Color Ramp forwards its scalar factor through Cycles conversion")
    fixture.emission.setNamedInput("color", ramp, "color")
    fixture.emission.setNamedInput("strength", ramp, "color")
    out.setNamedInput("surface", fixture.emission, "emission")
    render((0.1225, 0.1225, 0.1225), "Repeated use of a bypass conversion creates one input binding")
    bump = fixture.builder.createNode("cycles::bump::1", "muted_bump")
    # The default this test was written for (new nodes now take Blender's).
    bump.setParms(dict(distance=0.1))
    bump.bypass(True)
    bump.setNamedInput("height", fixture.texture, "alpha")
    fixture.texture.parm("filename").set(str(fixture.dest / "missing-again.exr"))
    out.setNamedInput("surface", bump, "normal")
    render((0, 0, 1), "Bypassed Bump retains geometric normal and ignores broken height")
    separate = fixture.builder.createNode("cycles::separate_xyz::1", "cycle_outputs")
    separate.parmTuple("vector").set((0.1, 0.2, 0.3))
    for index, value in enumerate((0.1, 0.2, 0.3)):
        assert network_preview.connect_to_output(separate) == index
        render((value, value, value), "Connect-to-output cycles scalar socket " + str(index))
    vector = fixture.builder.createNode("cycles::vector_math::1", "active_vector_output")
    vector.parmTuple("vector1").set((0.3, 0.4, 0))
    vector.parmTuple("vector2").set((0.1, 0.2, 0.3))
    assert vector.evalParm("math_type") == "add"
    assert vector.outputNames()[network_preview.connect_to_output(vector)] == "vector"
    render((0.4, 0.6, 0.3), "Vector Math preview selects its active vector output")
    vector.parm("math_type").set("length")
    assert vector.outputNames()[network_preview.connect_to_output(vector)] == "value"
    render((0.5, 0.5, 0.5), "Vector Math scalar operations preview their active value output")
    outer = ch.create_subnet(fixture.builder, "preview_outer")
    inner = ch.create_subnet(outer, "preview_inner")
    scalar = inner.createNode("cycles::value::1", "nested_value")
    scalar.parm("value").set(0.42)
    network_preview.connect_to_output(scalar)
    render((0.42, 0.42, 0.42), "Nested preview publishes typed outputs through both subnets")
    tint = inner.createNode("cycles::color::1", "nested_color")
    tint.parmTuple("value").set((0.25, 0.5, 0.75))
    network_preview.connect_to_output(tint)
    render((0.25, 0.5, 0.75), "Changing a nested preview from scalar to color reuses its ports")
    assert sum(n.userData("cycles_preview_output") == "1" for n in outer.allSubChildren()) == 2
    before = ch.material_graph(fixture.builder)
    try:
        network_preview.connect_to_output(fixture.sop)
    except ValueError:
        pass
    else:
        raise AssertionError("Preview modified an unrelated SOP network")
    assert ch.material_graph(fixture.builder) == before
    # An existing studio/artist hook must still receive unrelated events.
    import nodegraphhooks
    from types import SimpleNamespace

    previous = nodegraphhooks.createEventHandler
    delegated = []

    def existing(event, actions):
        delegated.append(event)
        return "existing", True

    try:
        nodegraphhooks.createEventHandler = existing
        network_preview.install()
        installed = nodegraphhooks.createEventHandler
        network_preview.install()
        assert nodegraphhooks.createEventHandler is installed
        event = SimpleNamespace(eventtype="keyhit")
        assert installed(event, []) == ("existing", True) and delegated == [event]
    finally:
        nodegraphhooks.createEventHandler = previous
    # Factory defaults are checked against independently audited Blender data.
    reference = json.loads(
        (Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"]) / "tests/fixtures/blender-shader-node-styles.json").read_text()
    )
    style_builder = ch.create_material(name="node_styles")
    world_builder = ch.create_world_material(name="world_node_styles")
    styles = []
    subnet = ch.create_subnet(style_builder, "interface_styles")
    for kind, entry in reference["nodes"].items():
        if kind in ("mix", "mix_float", "mix_vector", "mix_vector_non_uniform"):
            assert hou.nodeType(hou.vopNodeTypeCategory(), "cycles::" + kind + "::1") is None
            continue
        parent = (
            hou.node("/mat")
            if kind in ("material", "volume_material", "world_material")
            else subnet
            if kind == "subnet_connector"
            else world_builder
            if kind in ("background_shader", "world_output")
            else style_builder
        )
        try:
            if kind == "subnet":
                node = ch.create_subnet(style_builder, "styled_subnetwork")
            elif kind == "subnet_connector":
                node = ch.create_connector(subnet, "styled_port")
            else:
                node = parent.createNode("cycles::" + kind + "::1")
        except hou.OperationFailed as error:
            raise AssertionError((kind, str(error))) from error
        if kind in ("material", "volume_material", "world_material"):
            # Default children are laid out side by side: shader left, output right.
            shader, output = {
                "material": ("surface_shader", "surface_output"),
                "volume_material": ("volume_shader", "volume_output"),
                "world_material": ("background_shader", "world_output"),
            }[kind]
            shader, output = node.node(shader), node.node(output)
            assert output.position()[0] >= shader.position()[0] + shader.size()[0] + 0.5, (
                kind,
                shader.position(),
                shader.size(),
                output.position(),
            )
            assert abs(output.position()[1] - shader.position()[1]) < 1e-6, (kind, shader.position(), output.position())
        if kind in ("material", "volume_material"):
            # Artist-created builders keep Houdini's default node color rather
            # than Blender's green group color.
            # Houdini's base VOP default, which is none of Blender's category colors.
            houdini_default = np.asarray(node.type().defaultColor().rgb())
            assert np.allclose(node.color().rgb(), houdini_default, atol=1e-6, rtol=0), (
                kind,
                node.color().rgb(),
                houdini_default,
            )
            for category, rgb8 in reference["palette_rgb8"].items():
                assert not np.allclose(node.color().rgb(), np.asarray(rgb8) / 255, atol=1e-3), (
                    kind,
                    "uses Blender category color",
                    category,
                )
            styles.append(kind)
            continue
        if kind == "world_material":
            expected = np.asarray((74, 144, 226)) / 255
            assert np.allclose(node.color().rgb(), expected, atol=1e-6, rtol=0), (kind, node.color().rgb(), expected)
            styles.append(kind)
            continue
        expected = np.asarray(reference["palette_rgb8"][entry["style"]]) / 255
        assert np.allclose(node.color().rgb(), expected, atol=1e-6, rtol=0), (kind, node.color().rgb(), expected)
        styles.append(kind)
    fixture.color.setColor(hou.Color((0.9, 0.3, 0.1)))
    out.setNamedInput("surface", fixture.color, "color")
    render((0.2, 0.4, 0.8), "User node color does not change shading")
    assert np.allclose(fixture.color.color().rgb(), (0.9, 0.3, 0.1), atol=1e-6)
    saved = fixture.dest / "shader-bypass-preview.hiplc"
    path = fixture.color.path()
    muted_path = fixture.range.path()
    hou.hipFile.save(str(saved))
    checks.append(
        dict(name="All factory node categories match Blender and user colors remain editable", count=len(styles))
    )
hou.hipFile.load(str(saved), suppress_save_prompt=True)
assert np.allclose(hou.node(path).color().rgb(), (0.9, 0.3, 0.1), atol=1e-6)
assert hou.node(muted_path).isBypassed()
world = ch.create_world_material(name="preview_world")
world_color = world.createNode("cycles::color::1", "world_color")
world_color.parmTuple("value").set((0.125, 0.25, 0.5))
network_preview.connect_to_output(world_color)
with ch.Session(
    device=args.device, width=64, height=64, samples=4, threads=2, use_texture_cache=False
) as world_session:
    frame = world_session.wait(world_session.apply(dict(world=dict(graph=ch.material_graph(world)))))
    checks.append(
        dict(
            name="World output previews retain analytic background radiance",
            measured=fixture.expect(frame, (0.125, 0.25, 0.5)),
        )
    )
report = dict(
    result="passed",
    device=args.device,
    render_device_ids=[args.device],
    execution=session.execution,
    checks=checks,
    scope="Native connections and flags, independently specified linear pixels, Blender factory node defaults; physical mouse and display capture are separate.",
)
(fixture.dest / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", checks=len(checks))))
