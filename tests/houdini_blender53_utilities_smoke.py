# SPDX-License-Identifier: Apache-2.0
"""Native/portable integer, Boolean and typed selector rendering and HIP wiring persistence."""

import argparse
import json
import os
from pathlib import Path

import hou
import numpy as np
import cycles_houdini as ch
from shader_bypass_preview_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.add_argument("--out", default="")
args = parser.parse_args()
dest = Path(args.out or Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"]).parent / "qa/native-utilities")
dest.mkdir(parents=True, exist_ok=True)
f = Fixture(args.device)
checks = []
first = True
with ch.Session(
    device=args.device, width=64, height=64, samples=4, threads=2, collect_diagnostics=True, use_texture_cache=False
) as session:

    def render(expected, label, graph=None):
        global first
        update = dict(materials=[dict(key="surface", kind="graph", graph=graph or ch.material_graph(f.builder))])
        if first:
            update.update(
                geometry=[ch.geometry_request(f.geo, f.sop, material="surface")],
                camera_path=f.camera.path(),
                background=(0, 0, 0),
            )
            first = False
        frame = session.wait(session.apply(update))
        status = session.status()
        assert not status["error"], status
        checks.append(dict(name=label, measured=f.expect(frame, (expected,) * 3)))
        return status

    integer = f.builder.createNode("cycles::integer_math::1")
    offset = f.builder.createNode("cycles::math::1")
    offset.setParms(dict(math_type="add", value2=40))
    offset.setNamedInput("value1", integer, "value")
    f.output.setNamedInput("surface", offset, "value")
    cases = [
        ("add", 7, -3, 0, 4),
        ("subtract", 7, -3, 0, 10),
        ("multiply", 7, -3, 0, -21),
        ("divide", -7, 3, 0, -2),
        ("multiply_add", 7, -3, 2, -19),
        ("power", 2, 5, 0, 32),
        ("floored_modulo", -7, 3, 0, 2),
        ("absolute", -7, 0, 0, 7),
        ("minimum", 7, -3, 0, -3),
        ("maximum", 7, -3, 0, 7),
        ("gcd", 18, 12, 0, 6),
        ("lcm", 18, 12, 0, 36),
        ("negate", 7, 0, 0, -7),
        ("sign", -7, 0, 0, -1),
        ("divide_floor", -7, 3, 0, -3),
        ("divide_ceil", -7, 3, 0, -2),
        ("divide_round", -8, 3, 0, -3),
        ("modulo", -7, 3, 0, -1),
    ]
    for op, a, b, c, expected in cases:
        integer.setParms(dict(math_type=op, value1=a, value2=b, value3=c))
        render(expected + 40, "integer-" + op)
    integer.setParms(dict(math_type="subtract", value1=16777217, value2=16777216))
    render(41, "exact-integer-authored-defaults")
    boolean = f.builder.createNode("cycles::boolean_math::1")
    f.output.setNamedInput("surface", boolean, "boolean")
    for op, expected in [
        ("and", 0),
        ("or", 1),
        ("not", 0),
        ("nand", 1),
        ("nor", 0),
        ("xnor", 0),
        ("xor", 1),
        ("imply", 0),
        ("nimply", 1),
    ]:
        boolean.setParms(dict(math_type=op, boolean1=2, boolean2=-7))
        render(expected, "boolean-" + op)
    switch = f.builder.createNode("cycles::switch_integer::1")
    switch.setParms(dict(switch=1, false=1, true=16777217))
    integer.setParms(dict(math_type="subtract", value2=16777216))
    integer.setNamedInput("value1", switch, "output")
    f.output.setNamedInput("surface", integer, "value")
    render(1, "static-switch-retains-int32")
    constant_integer = f.builder.createNode("cycles::integer_math::1")
    constant_integer.setParms(dict(math_type="add", value1=1, value2=0))
    switch.setNamedInput("switch", constant_integer, "value")
    render(1, "constant-integer-expression-selector-retains-int32")
    float_switch = f.builder.createNode("cycles::switch_float::1")
    float_switch.setParms({"false": 0.25, "true": 0.75})
    constant_math = f.builder.createNode("cycles::math::1")
    constant_math.setParms(dict(math_type="add", value1=1, value2=0))
    float_switch.setNamedInput("switch", constant_math, "value")
    float_switch.setNamedInput("false", f.texture, "color")
    f.output.setNamedInput("surface", float_switch, "output")
    status = render(0.75, "constant-math-selector-prunes-broken-texture")
    assert not status["source_diagnostics"], status
    float_switch.setInput(float_switch.inputNames().index("false"), None)
    vector_selector = f.builder.createNode("cycles::vector_math::1")
    vector_selector.setParms(dict(math_type="add"))
    vector_selector.parmTuple("vector2").set((1, -1, 0))
    runtime_info = f.builder.createNode("cycles::object_info::1")
    vector_selector.setNamedInput("vector1", runtime_info, "location")
    float_switch.setNamedInput("switch", vector_selector, "vector")
    render(0.75, "varying-vector-selector-uses-length")
    index = f.builder.createNode("cycles::index_switch_float::1")
    index.parm("item_count").set(5)
    assert index.inputNames() == ("index", "item0", "item1", "item2", "item3", "item4"), index.inputNames()
    for i in range(5):
        index.parm("item" + str(i)).set((i + 1) * 0.1)
    index.parm("index").set(3)
    f.output.setNamedInput("surface", index, "output")
    render(0.4, "native-multiparm-index")
    # A dormant broken texture must not be evaluated or diagnosed.
    index.setNamedInput("item4", f.texture, "color")
    render(0.4, "constant-index-prunes-broken-texture")
    index.setInput(index.inputNames().index("item4"), None)
    varying = f.builder.createNode("cycles::math::1")
    varying.setParms(dict(math_type="add", value2=0))
    index.setNamedInput("index", varying, "value")
    object_info = f.builder.createNode("cycles::object_info::1")
    varying.setNamedInput("value2", object_info, "object_index")
    for value, expected in [(-2, 0), (-0.9, 0.1), (0.9, 0.1), (1.9, 0.2), (4.9, 0.5), (5.1, 0)]:
        varying.parm("value1").set(value)
        render(expected, "varying-index-" + str(value))
    enabled = f.builder.createNode("cycles::enable_output_float::1")
    enabled.setParms({"enable": False, "value": 0.4})
    enabled.setNamedInput("value", f.texture, "color")
    f.output.setNamedInput("surface", enabled, "output")
    status = render(0, "disabled-output-prunes-broken-texture")
    assert not status["source_diagnostics"], status
    enabled.setInput(enabled.inputNames().index("value"), None)
    enabled.parm("enable").set(True)
    render(0.4, "enabled-static-output")
    enabled.setNamedInput("enable", varying, "value")
    varying.parm("value1").set(0.5)
    render(0.4, "enabled-varying-output")
    varying.parm("value1").set(-0.5)
    render(0, "disabled-varying-output")
    warning = f.builder.createNode("cycles::warning::1")
    warning.setParms(dict(show=True, message="Fixture warning", severity="info"))
    status = render(0, "unconnected-warning-side-effect")
    assert any(
        d["code"] == "material.warning" and d["message"] == "Fixture warning" for d in status["source_diagnostics"]
    ), status
    warning.parm("show").set(False)
    status = render(0, "warning-clears-after-edit")
    assert not status["source_diagnostics"], status
    warning.setNamedInput("show", varying, "value")
    status = render(0, "varying-warning-is-observable-limitation")
    assert any(d["code"] == "material.warning_requires_constant" for d in status["source_diagnostics"]), status
    warning.setInput(0, None)
    warning.parm("show").set(False)
    # Ordinary portable entry point uses the same inliner and rejects an edit
    # above Houdini's 9999-input limit (including the selector). The worker
    # reports renderer validation asynchronously; reserve/request generation is
    # distinct from the last committed scene and retained image.
    graph = dict(
        schema_version=1,
        nodes=[
            dict(
                id="pick",
                definition="cycles/index_switch_float/1",
                parameters=dict(index=0, item_count=4097, item0=0.625),
            ),
            dict(id="inactive", definition="unavailable/inactive/1", parameters={}),
        ],
        links=[{"from": dict(node="inactive", socket="bad"), "to": dict(node="pick", socket="item4096")}],
        surface=dict(node="pick", socket="output"),
    )
    render(0.625, "portable-large-static-index", graph)
    accepted = session.status()["generation"]
    broken = dict(
        graph,
        nodes=[dict(id="pick", definition="cycles/index_switch_float/1", parameters=dict(index=0, item_count=9999))],
    )
    rejected = None
    try:
        session.wait(session.apply(dict(materials=[dict(key="surface", kind="graph", graph=broken)])))
    except (ValueError, RuntimeError) as error:
        rejected = str(error)
    assert rejected and "9999-input limit" in rejected, rejected
    rejected_status = session.status()
    retained = session.frame_info()
    assert retained and retained["generation"] == accepted, (retained, rejected_status)
    if "committed_generation" in rejected_status:
        assert rejected_status["update_rejected"] and rejected_status["backend_alive"], rejected_status
        assert rejected_status["committed_generation"] == accepted, rejected_status
        assert rejected_status["generation"] > accepted, rejected_status
    else:
        assert rejected_status["generation"] == accepted, rejected_status
    checks.append(
        dict(
            name="portable-over-limit-rejection-retains-accepted-scene",
            error=rejected,
            accepted_generation=accepted,
            status=rejected_status,
        )
    )
    render(0.625, "portable-failed-edit-repair", graph)
    # An AOV writer is an independent observable root; its selector cannot be
    # dropped just because the surface does not depend on it.
    aov = dict(
        graph,
        nodes=graph["nodes"]
        + [
            dict(
                id="aov_pick",
                definition="cycles/switch_float/1",
                parameters={"switch": True, "false": 0.1, "true": 0.7},
            ),
            dict(id="aov", definition="cycles/aov_output/1", parameters={"name": "selected"}),
        ],
        links=graph["links"]
        + [{"from": dict(node="aov_pick", socket="output"), "to": dict(node="aov", socket="value")}],
    )
    frame = session.wait(
        session.apply(
            dict(materials=[dict(key="surface", kind="graph", graph=aov)], aovs=[dict(name="selected", type="value")])
        )
    )
    assert "selected" in frame["planes"], frame["planes"].keys()
    selected = np.frombuffer(frame["planes"]["selected"]["pixels"], np.float32).reshape(64, 64, -1)
    assert np.allclose(selected[30:34, 30:34], 0.7, atol=2e-5), selected[32, 32]
    checks.append(dict(name="portable-AOV-selector-root"))
    # Save/reload must preserve every dynamic socket token and input index.
    index.setInput(0, None)
    index.parm("index").set(3)
    index.setNamedInput("item3", f.value, "value")
    f.output.setNamedInput("surface", index, "output")
    paths = f.builder.path(), index.path()
    wires = [(c.outputName(), c.inputIndex(), c.inputNode().path()) for c in index.inputConnections()]
    hip = dest / "selector-wiring.hip"
    hou.hipFile.save(str(hip))
    hou.hipFile.load(str(hip), suppress_save_prompt=True)
    restored = hou.node(paths[1])
    assert restored.inputNames() == ("index", "item0", "item1", "item2", "item3", "item4")
    assert [(c.outputName(), c.inputIndex(), c.inputNode().path()) for c in restored.inputConnections()] == wires
    render(0.3, "saved-native-selector-wiring", ch.material_graph(hou.node(paths[0])))
(dest / "report.json").write_text(json.dumps(dict(result="passed", device=args.device, checks=checks), indent=2))
print(json.dumps(dict(result="passed", checks=len(checks))))
