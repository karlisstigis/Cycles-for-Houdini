# SPDX-License-Identifier: Apache-2.0
"""Independently specified nested VOP interfaces, defaults and render updates."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
from array import array
import json
import os
from pathlib import Path

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import materials as m, native_scene

destination = artifacts_root() / "stage3/materials/subnets"
destination.mkdir(parents=True, exist_ok=True)
hou.setFrame(1)
assert ch.capabilities()["material_subnetworks"]
builder = hou.node("/mat").createNode(m.BUILDER, "native_nested_surface", run_init_scripts=False)
output = builder.createNode(m.SURFACE_OUTPUT, "surface_output")
outer = m.create_subnet(builder, "outer")
tint = m.create_connector(outer, "tint", type="color", default=(0.2, 0.4, 0.8))
gain = m.create_connector(outer, "gain", default=2.0)
inner = m.create_subnet(outer, "inner")
inner_tint = m.create_connector(inner, "tint", type="color", default=(0.9, 0.1, 0.1))
inner_gain = m.create_connector(inner, "gain", default=9.0)
emission = inner.createNode("cycles::emission::1", "emission")
emission.setNamedInput("color", inner_tint, inner_tint.outputNames()[0])
emission.setNamedInput("strength", inner_gain, inner_gain.outputNames()[0])
inner_color = m.create_connector(inner, "color", kind="output", type="color")
inner_color.setInput(0, inner_tint)
inner_surface = m.create_connector(inner, "surface", kind="output", type="bsdf")
inner_surface.setInput(0, emission)
inner.setNamedInput("tint", tint, tint.outputNames()[0])
inner.setNamedInput("gain", gain, gain.outputNames()[0])
outer_color = m.create_connector(outer, "color", kind="output", type="color")
outer_color.setNamedInput(outer_color.inputNames()[0], inner, "color")
outer_surface = m.create_connector(outer, "surface", kind="output", type="bsdf")
outer_surface.setNamedInput(outer_surface.inputNames()[0], inner, "surface")
output.setNamedInput("surface", outer, "surface")
assert inner.outputNames() == ("color", "surface") and outer.outputNames() == ("color", "surface")
assert inner.outputDataTypes() == ("vector", "bsdf")
assert outer.inputNames() == ("tint", "gain", "next")
assert not any(connection.outputName() == "next" for connection in outer.inputConnections())
assert not any(node.errors() for node in builder.allSubChildren())

color = builder.createNode("cycles::color::1", "external_color")
color.parmTuple("value").set((0.1, 0.3, 0.5))
scalar = builder.createNode("cycles::value::1", "external_gain")
scalar.parm("value").set(4.0)
separate = builder.createNode("cycles::separate_xyz::1", "separate")
separate.parmTuple("vector").set((1.0, 2.0, 3.0))

# An input/output connector reads the external input inside the network and
# publishes the value connected to its child input as the parent output.
feedback = m.create_subnet(builder, "gain_processor")
io = m.create_connector(feedback, "gain", kind="inputoutput", default=2.0)
multiply = feedback.createNode("cycles::math::1", "multiply")
multiply.parm("math_type").set("multiply")
multiply.parm("value2").set(3.0)
multiply.setInput(0, io)
io.setInput(0, multiply)
assert feedback.inputNames() == ("gain", "next") and feedback.outputNames() == ("_gain",)
assert not any(connection.outputName() == "next" for connection in feedback.inputConnections())

# Unconnected BSDF inputs retain the engine's null-closure default. They must
# not be replaced by black emission, which can change closure sampling.
optional = m.create_subnet(builder, "optional_closure")
empty = m.create_connector(optional, "empty", type="bsdf")
optional_emission = optional.createNode("cycles::emission::1", "emission")
optional_emission.parmTuple("color").set((0.3, 0.6, 0.9))
optional_emission.parm("strength").set(2.0)
add = optional.createNode("cycles::add_closure::1", "add")
add.setInput(0, optional_emission)
add.setInput(1, empty)
optional_surface = m.create_connector(optional, "surface", kind="output", type="bsdf")
optional_surface.setInput(0, add)

obj = hou.node("/obj").createNode("geo", "subnet_receiver", run_init_scripts=False)
grid = obj.createNode("grid")
grid.parm("type").set(0)
grid.parm("orient").set(0)
grid.parm("rows").set(2)
grid.parm("cols").set(2)
grid.parmTuple("size").set((8, 8))
grid.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam", "subnet_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)
rop = hou.node("/out").createNode("cycles_ipr", "native_subnet_cycles")
set_rop_devices(rop, "CPU")
for name, value in dict(
    soppath=grid.path(), camera=camera.path(), materialpath=builder.path(), resx=64, resy=64, samples=32
).items():
    rop.parm(name).set(value)
rop.parmTuple("background").set((0, 0, 0))
rop.parmTuple("lightstrength").set((0, 0, 0))


def disconnect(subnet, name):
    subnet.setInput(subnet.inputNames().index(name), None)


def expect(frame, expected):
    data = array("f")
    data.frombytes(frame["planes"]["combined"]["pixels"])
    offset = (32 * 64 + 32) * 4
    measured = list(data[offset : offset + 3])
    assert np.allclose(measured, expected, atol=1e-5, rtol=1e-5), (measured, expected)
    return measured


def rejected(action, expected):
    try:
        action()
    except (ValueError, RuntimeError) as error:
        assert expected in str(error), str(error)
    else:
        raise AssertionError("Invalid subnet accepted: " + expected)


report = dict(result="running", host_checks=[], devices=[])
graph = ch.material_graph(builder)
emission_id = emission.userData("cycles_graph_id")
assert graph["surface"] == dict(node=emission_id, socket="emission")
assert not any(node["definition"].startswith("cycles/subnet") for node in graph["nodes"])
defaults = {node["id"]: node for node in graph["nodes"] if node["id"].endswith("/default")}
assert defaults[gain.userData("cycles_graph_id") + "/default"]["parameters"]["value"] == 2.0
assert defaults[tint.userData("cycles_graph_id") + "/default"]["parameters"]["value"] == (0.2, 0.4, 0.8)
output.setNamedInput("surface", optional, "surface")
optional_graph = ch.material_graph(builder)
assert len([link for link in optional_graph["links"] if link["to"]["node"] == add.userData("cycles_graph_id")]) == 1
output.setNamedInput("surface", outer, "surface")
report["host_checks"].append("Typed nested ports, nonzero closure outputs and owned default lowering")
report["host_checks"].append("Null BSDF input omits a link without inventing a shader")

before = ch.material_graph(builder)
inner.setName("renamed_inner")
gain.setName("renamed_gain_connector")
outer.setName("renamed_outer")
assert ch.material_graph(builder) == before
report["host_checks"].append("Nested network and connector renames preserve the exact graph")

notifications = []
observer = native_scene.SceneObserver(rop, lambda: notifications.append(True))
tint.parmTuple("colordef").set((0.25, 0.4, 0.8))
assert notifications
notifications.clear()
temporary = inner.createNode("cycles::value::1", "temporary")
assert notifications
observer.refresh()
notifications.clear()
temporary.parm("value").set(3.0)
assert notifications
temporary.destroy()
observer.close()
tint.parmTuple("colordef").set((0.2, 0.4, 0.8))
report["host_checks"].append("Observer includes nested parameters, creation and deletion")

for device in ("CPU", ch.default_device()):
    with ch.Session(device=device, width=64, height=64, samples=32, collect_diagnostics=True) as session:
        checks = []

        def render(name, expected, at_time=None):
            scene = native_scene.capture(rop, at_time=at_time)
            frame = session.wait(session.apply(scene, at_time=at_time))
            checks.append(dict(name=name, measured=expect(frame, expected)))
            return frame

        render("Nested input defaults and nonzero closure output", (0.4, 0.8, 1.6))
        conversions = session.status()["geometry_conversions"]
        inner.bypass(True)
        render("Muted nested shader output with no shader input contributes zero", (0, 0, 0))
        inner.bypass(False)
        render("Restoring a nested shader restores its material", (0.4, 0.8, 1.6))
        outer.setNamedInput("tint", color, "color")
        outer.setNamedInput("gain", scalar, "value")
        render("External values override nested defaults", (0.4, 1.2, 2.0))
        disconnect(outer, "tint")
        outer.setNamedInput("gain", separate, "z")
        render("Nonzero upstream scalar output remains z through both boundaries", (0.6, 1.2, 2.4))
        outer.setNamedInput("gain", feedback, "_gain")
        render("Input/output connector applies its internal computation to default input", (1.2, 2.4, 4.8))
        feedback.setNamedInput("gain", scalar, "value")
        render("Input/output connector applies its internal computation to external input", (2.4, 4.8, 9.6))
        disconnect(feedback, "gain")
        disconnect(outer, "gain")
        output.setNamedInput("surface", optional, "surface")
        render("Null closure in Add Closures leaves independently specified emission", (0.6, 1.2, 1.8))
        output.setNamedInput("surface", outer, "surface")

        # Port values may animate; the port interface itself stays static.
        gain.parm("floatdef").setExpression("$F + 1", hou.exprLanguage.Hscript)
        promoted = m.promote_parameter(outer, gain, "floatdef", name="gain", label="Nested Gain")
        assert gain.parm("floatdef").getReferencedParm() == promoted[0]
        render("Animated promoted subnet default at explicit time", (0.8, 1.6, 3.2), hou.frameToTime(3))
        assert hou.frame() == 1
        # Promote the subnet control to the root, retaining the original
        # connector -> subnet -> material chain of public channel references.
        root_control = m.promote_parameter(builder, outer, "gain", name="root_gain")
        root_control[0].deleteAllKeyframes()
        root_control[0].set(5.0)
        render("Root promotion of a nested connector default", (1.0, 2.0, 4.0))
        assert session.status()["geometry_conversions"] == conversions

        generation = session.status()["generation"]
        outer_surface.setInput(0, None)
        rejected(lambda: ch.material_graph(builder), "Connect a shader value")
        assert session.status()["generation"] == generation
        render("A disconnected assigned subnet output uses diagnostic shading", (1, 0, 1))
        assert any(issue["code"] == "material.invalid_override" for issue in session.status()["source_diagnostics"])
        outer_surface.setNamedInput(outer_surface.inputNames()[0], inner, "surface")
        render("Repairing the nested output restores its original material", (1.0, 2.0, 4.0))
        assert not session.status()["source_diagnostics"]

        allocations = []
        for iteration in range(20):
            root_control[0].set(3.0 if iteration % 2 else 2.0)
            scene = native_scene.capture(rop)
            frame = session.wait(session.apply(scene))
            expect(frame, (0.6, 1.2, 2.4) if iteration % 2 else (0.4, 0.8, 1.6))
            allocations.append(session.status()["device_allocation_bytes"])
        assert len(set(allocations[5:])) == 1
        assert session.status()["geometry_conversions"] == conversions
        checks.append(
            dict(name="Repeated nested edits retain geometry and stable tracked allocations", allocations=allocations)
        )

        # Restore independent defaults for the next device and saved fixture.
        gain.parm("floatdef").deleteAllKeyframes()
        gain.parm("floatdef").set(2.0)
        group = outer.parmTemplateGroup()
        group.remove("gain")
        outer.setParmTemplateGroup(group)
        group = builder.parmTemplateGroup()
        group.remove("root_gain")
        builder.setParmTemplateGroup(group)
        report["devices"].append(dict(device=device, checks=checks))
    assert session.status()["device_allocation_bytes"] == 0

# Negative interface cases use actual native nodes and must not silently
# coerce differently typed values, undefined outputs or a recursive portal.
output.setNamedInput("surface", optional, "surface")
optional_surface.setInput(0, empty)
rejected(lambda: ch.material_graph(builder), "Connect a shader, color or value")
optional_surface.setInput(0, add)
output.setNamedInput("surface", outer, "surface")
output.bypass(True)
rejected(lambda: ch.material_graph(builder), "Bypassed Cycles Material Output")
output.bypass(False)
outer_surface.parm("parmtype").set(0)
rejected(lambda: ch.material_graph(builder), "explicit type conversion")
outer_surface.parm("parmtype").set(outer_surface.parm("parmtype").parmTemplate().menuItems().index("bsdf"))
before_unused = ch.material_graph(builder)
duplicate = outer.createNode(m.CONNECTOR, "duplicate_input")
duplicate.parm("parmname").set("gain")
assert ch.material_graph(builder) == before_unused, "An unused declaration changed the reachable shader"
probe = outer.createNode("cycles::math::1", "duplicate_probe")
probe.setInput(0, gain)
probe.setInput(1, duplicate)
inner.setNamedInput("gain", probe, "value")
rejected(lambda: ch.material_graph(builder), "Duplicate Cycles subnet input")
inner.setNamedInput("gain", gain, gain.outputNames()[0])
probe.destroy()
duplicate.destroy()
rejected(lambda: m.create_connector(outer, "gain"), "already exists")
rejected(lambda: m.create_connector(outer, "unsupported", type="string"), "port type")
rejected(lambda: m.create_connector(outer, "bad_closure", type="bsdf", default="named"), "unconnected closure default")
outer.setNamedInput("gain", feedback, "_gain")
io.parm("parmtype").set(io.parm("parmtype").parmTemplate().menuItems().index("int"))
rejected(lambda: ch.material_graph(builder), "Unsupported Cycles subnet port")
io.parm("parmtype").set(0)
io.parm("parmname").setExpression('"gain"', hou.exprLanguage.Hscript)
rejected(lambda: ch.material_graph(builder), "cannot be animated")
io.parm("parmname").deleteAllKeyframes()
io.parm("parmname").set("gain")
cycle = m.create_subnet(builder, "portal_cycle")
cycle_connector = m.create_connector(cycle, "gain", kind="inputoutput")
cycle_connector.setInput(0, cycle_connector)
cycle.setNamedInput("gain", cycle, "_gain")
outer.setNamedInput("gain", cycle, "_gain")
rejected(lambda: ch.material_graph(builder), "Cyclic Cycles subnet interface")
disconnect(outer, "gain")
cycle.destroy()
report["host_checks"].append("Malformed type, duplicate, unsupported, animated interface and portal-cycle diagnostics")

before = ch.material_graph(builder)
hip = destination / "native-material-subnets.hiplc"
hou.hipFile.save(str(hip))
hou.hipFile.load(str(hip), suppress_save_prompt=True)
builder = hou.node("/mat/native_nested_surface")
assert ch.material_graph(builder) == before
report["host_checks"].append("Saved nested network retains stable identities, ports and exact flattened graph")
report["result"] = "passed"
report["scope"] = (
    "Native nested float, float3 and BSDF connectors, defaults, input/output semantics, promotion, selected shading and updates on CPU/4090; unsupported port types require an explicit adapter."
)
(destination / "houdini-material-subnet-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(
    json.dumps(
        dict(
            result="passed",
            host_checks=len(report["host_checks"]),
            checks_per_device=[len(row["checks"]) for row in report["devices"]],
        )
    )
)
