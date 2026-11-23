# SPDX-License-Identifier: Apache-2.0
"""Connected input defaults are disabled without losing values or graph semantics."""

from support.paths import artifacts_root
import argparse
import json
import os
import traceback
from pathlib import Path

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini.materials import create_world_material
from shader_bypass_preview_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "stage3/materials/input-states"
dest.mkdir(parents=True, exist_ok=True)
f = Fixture(device)
checks = []
report = dict(result="running", device=device, checks=checks)


def state(node, name, disabled):
    node.updateParmStates()
    parm = node.parmTuple(name)
    assert parm is not None and all(p.isDisabled() == disabled for p in parm), (
        node.path(),
        name,
        disabled,
        [p.isDisabled() for p in parm],
    )


try:
    # The old interface fails this first assertion on a normal connected Math.
    state(f.math, "value1", True)
    state(f.math, "value2", False)
    source = f.builder.createNode("cycles::value::1", "wire_value")
    source.parm("value").set(0.25)
    math = f.builder.createNode("cycles::math::1", "wire_math")
    math.setParms(dict(math_type="add", value1=0.125, value2=0.5))
    f.output.setNamedInput("surface", math, "value")
    first = True
    conversions = None
    with ch.Session(
        device=device, width=64, height=64, samples=8, threads=2, collect_diagnostics=True, use_texture_cache=False
    ) as session:

        def render(expected, name):
            global first, conversions
            update = dict(materials=[dict(key="surface", kind="graph", graph=ch.material_graph(f.builder))])
            if first:
                update.update(
                    geometry=[ch.geometry_request(f.geo, f.sop, material="surface")],
                    camera_path=f.camera.path(),
                    background=(0, 0, 0),
                )
                first = False
            frame = session.wait(session.apply(update))
            status = session.status()
            assert (
                not status["error"] and not status["source_diagnostics"] and status["render_device_ids"] == [device]
            ), status
            if conversions is None:
                conversions = status["geometry_conversions"]
            assert status["geometry_conversions"] == conversions
            checks.append(dict(name=name, measured=f.expect(frame, expected)))

        render((0.625,) * 3, "Unconnected Math uses its authored defaults")
        with hou.undos.group("Wire shader input"):
            math.setNamedInput("value1", source, "value")
        state(math, "value1", True)
        state(math, "value2", False)
        assert math.evalParm("value1") == 0.125
        render((0.75,) * 3, "Connected Math uses the wire while retaining its disabled default")
        source.parm("value").set(0.5)
        render((1,) * 3, "Upstream edits drive the disabled input")
        math.parm("value1").set(float("nan"))
        render((1,) * 3, "Invalid dormant default is ignored while a valid wire supplies the input")
        math.parm("value1").set(0.125)
        with hou.undos.group("Unwire shader input"):
            math.setInput(math.inputNames().index("value1"), None)
        state(math, "value1", False)
        assert math.evalParm("value1") == 0.125
        render((0.625,) * 3, "Disconnect restores editability and the authored value")
        # Undo is certified in the real GUI workflow; headless Undo is disabled.
        vector = f.builder.createNode("cycles::vector_math::1", "wire_vector")
        vector.setParms(dict(math_type="scale", scale=2))
        vector.parmTuple("vector1").set((0.4, 0.5, 0.6))
        vector.setNamedInput("vector1", f.color, "color")
        state(vector, "vector1", True)
        state(vector, "scale", False)
        f.output.setNamedInput("surface", vector, "vector")
        render((0.4, 0.8, 1.6), "Connected vector disables all components and uses the upstream color")
        vector.setNamedInput("scale", source, "value")
        state(vector, "scale", True)
        render((0.1, 0.2, 0.4), "Connected Scale disables its scalar default")
        vector.parm("math_type").set("round")
        state(vector, "scale", True)
        assert vector.parm("scale").isHidden() and vector.evalParm("scale") == 2
        render((0, 0, 1), "Dormant wired Scale remains stored while Round ignores it")
        vector.parm("math_type").set("scale")
        state(vector, "scale", True)
        render((0.1, 0.2, 0.4), "Returning to Scale restores its wire and disabled field")
        vector.setInput(vector.inputNames().index("scale"), None)
        state(vector, "scale", False)
        render((0.4, 0.8, 1.6), "Removing the Scale wire restores the saved value")
        ramp = f.builder.createNode("cycles::color_ramp::1", "wire_ramp")
        ramp.parm("factor").set(0.125)
        ramp.setNamedInput("factor", source, "value")
        state(ramp, "factor", True)
        assert not ramp.parm("ramp").isDisabled()
        f.output.setNamedInput("surface", ramp, "color")
        render((0.5,) * 3, "Ramp factor is disabled while its curve remains editable")
        ramp.setInput(0, None)
        state(ramp, "factor", False)
        render((0.125,) * 3, "Disconnecting ramp factor restores its value")

    # Exercise every authored linkable field in the reflected native catalogue,
    # including currently hidden inputs. Values and expressions remain unchanged.
    hosts = {}
    counts = {}
    world = create_world_material(name="wired_world_ui")
    for definition in ch.shader_catalogue()["definitions"]:
        if not any(definition[k] for k in ("surface_graph_adapter", "volume_graph_adapter", "world_graph_adapter")):
            continue
        kind = definition["engine_type"]
        host = world if kind == "background_shader" else f.builder
        if kind in ("mix", "mix_float", "mix_vector", "mix_vector_non_uniform"):
            assert hou.nodeType(hou.vopNodeTypeCategory(), "cycles::" + kind + "::1") is None
            continue
        node = host.createNode("cycles::" + kind + "::1", "states_" + kind)
        node.updateParmStates()
        sources = hosts.get(host.path())
        if sources is None:
            scalar = host.createNode("cycles::value::1", "states_value")
            color = host.createNode("cycles::color::1", "states_color")
            # The default this test was written for (new nodes now take Blender's).
            color.setParms(dict(value=(0.0, 0.0, 0.0)))
            sources = hosts[host.path()] = {"float": (scalar, "value"), "vector": (color, "color")}
        sockets = {s["id"]: s for s in definition["inputs"]}
        for name in node.inputNames():
            parm = node.parmTuple(name)
            if parm is None:
                continue
            if parm.parmTemplate().type() == hou.parmTemplateType.Label:
                # Closure Index Switch uses visible multiparm item labels in
                # place of values (material_vop.cpp's index_item_templates).
                # Static implicit-input grouping metadata remains invisible.
                closure_item = kind == "index_switch_shader" and name.startswith("item") and name[4:].isdigit()
                assert parm.isHidden() == (not closure_item), (kind, name)
                continue  # Labels never supply an editable input default.
            source_type = "float" if node.inputDataTypes()[node.inputIndex(name)] == "float" else "vector"
            source_node, output = sources[source_type]
            before = parm.eval()
            index = node.inputNames().index(name)
            node.setNamedInput(name, source_node, output)
            state(node, name, True)
            assert parm.eval() == before, (kind, name, before, parm.eval())
            node.setInput(index, None)
            state(node, name, False)
            assert parm.eval() == before
            counts[kind] = counts.get(kind, 0) + 1
    # HIP reload refreshes flags before a pane has selected these nodes.
    math.setNamedInput("value1", source, "value")
    path = math.path()
    source_path = source.path()
    hip = dest / "connected-inputs.hiplc"
    hou.hipFile.save(str(hip))
    hou.hipFile.load(str(hip), suppress_save_prompt=True, ignore_load_warnings=True)
    math = hou.node(path)
    state(math, "value1", True)
    assert math.input(0) == hou.node(source_path)
    math.setInput(0, None)
    state(math, "value1", False)
    assert math.evalParm("value1") == 0.125
    report.update(
        result="passed",
        catalogue_fields=sum(counts.values()),
        catalogue_nodes=len(counts),
        fields_by_type=counts,
        hip=str(hip),
    )
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    (dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result="passed", checks=len(checks), catalogue_fields=report["catalogue_fields"])))
