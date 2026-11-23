# SPDX-License-Identifier: Apache-2.0
"""Ordinary HIP save -> plugin-absent edits/two Save As -> genuine native reload.

Run each mode in a separate owned hython process. Missing mode requires an
empty package directory and isolated preferences, with no Cycles Python path.
No recovery API, direct fallback API, review copy or replacement graph is used.
"""

import argparse
from array import array
import copy
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import time
import traceback

import hou

parser = argparse.ArgumentParser()
parser.add_argument("mode", choices=("prepare", "missing", "reload"))
parser.add_argument("--directory", type=Path, required=True)
parser.add_argument("--device", default="CPU")
args = parser.parse_args()
assert args.device == "CPU", "Fixed-seed native CPU reference contract"
destination = args.directory.resolve()
destination.mkdir(parents=True, exist_ok=True)
started = time.monotonic()
report = dict(
    result="running",
    mode=args.mode,
    scope=__doc__,
    pid=os.getpid(),
    houdini=hou.applicationVersionString(),
    checks=[],
    load_warnings=[],
)
original = destination / "authored.hiplc"
save_as_one = destination / "offline-save-as-one.hiplc"
save_as_two = destination / "offline-save-as-two.hiplc"
frames = (1, 4, 10)


def write(name, value):
    (destination / name).write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")


def load(path):
    try:
        hou.hipFile.load(str(path), suppress_save_prompt=True)
    except hou.LoadWarning as error:
        report["load_warnings"].append(str(error))


def value(item):
    if isinstance(item, hou.Ramp):
        return dict(
            basis=[str(x) for x in item.basis()], keys=list(item.keys()), values=[value(x) for x in item.values()]
        )
    if isinstance(item, (tuple, list)):
        return [value(x) for x in item]
    if isinstance(item, (str, float, int, bool)) or item is None:
        return item
    return str(item)


def signature():
    nodes = {}
    for node in sorted(hou.node("/mat").allSubChildren(), key=lambda n: n.path()):
        parms = {}
        for parm in node.parms():
            keys = parm.keyframes()
            item = dict(
                raw=parm.rawValue(), keys=[key.asCode() for key in keys], template_type=str(parm.parmTemplate().type())
            )
            if keys:
                item["at_frames"] = {str(frame): value(parm.evalAtFrame(frame)) for frame in frames}
            if isinstance(parm.parmTemplate(), hou.RampParmTemplate):
                item["ramp"] = value(parm.evalAsRamp())
            parms[parm.name()] = item
        wires, named_wires = [], []
        for connection in node.inputConnections():
            indirect = connection.subnetIndirectInput()
            source = node.parent() if indirect is not None else connection.inputNode()
            output_index = indirect.number() if indirect is not None else connection.outputIndex()
            source_path = source.path() if source is not None else None
            endpoint = (
                dict(indirect_input=output_index)
                if indirect is not None
                else (source.outputNames()[output_index] if source is not None else None)
            )
            wires.append([connection.inputIndex(), source_path, output_index, indirect is not None])
            named_wires.append([node.inputNames()[connection.inputIndex()], source_path, endpoint])
        nodes[node.path()] = dict(
            type=node.type().name(),
            network=node.isNetwork(),
            parms=parms,
            inputs=wires,
            input_names=list(node.inputNames()),
            output_names=list(node.outputNames()),
            input_types=list(node.inputDataTypes()),
            output_types=list(node.outputDataTypes()),
            named_inputs=named_wires,
            # Houdini rewrites this version stamp while a class is absent;
            # artist metadata and every other authored field remain strict.
            userdata={k: v for k, v in node.userDataDict().items() if k != "___Version___"},
            comment=node.comment(),
        )
        if node.type().name() == "subnet":
            language = node.shaderLanguageName()
            mask = node.parm("tabmenumask")
            assert mask is not None, (node.path(), "Missing Cycles authoring menu mask")
            nodes[node.path()]["subnet_authoring"] = dict(
                shader_language=language,
                material_flag=node.isMaterialFlagSet(),
                tab_menu_mask=mask.rawValue(),
                tab_menu_default=value(mask.parmTemplate().defaultValue()),
            )
    bindings = {}
    for path in (
        "/obj/roundtrip_receiver/shop_materialpath",
        "/obj/roundtrip_receiver/tx",
        "/obj/external_controls/tx",
        "/obj/external_reader/tx",
        "/obj/roundtrip_instance/instancepath",
    ):
        parm = hou.parm(path)
        bindings[path] = dict(
            raw=parm.rawValue(), at_frames={str(frame): value(parm.evalAtFrame(frame)) for frame in frames}
        )
    return dict(nodes=nodes, bindings=bindings)


def equal(actual, expected, label):
    if args.mode == "missing":
        # Keep complete observations before the narrow missing-DSO allowances,
        # so a failure can distinguish wire loss from host language/type state.
        write("missing-last-full-actual-signature.json", actual)
        write("missing-last-full-expected-signature.json", expected)
        # Static compiled class sockets retain exact names, order and numerical
        # wire indices while absent. Only their transport types are neutral.
        # Built-in dynamic subnet interfaces retain Houdini's own persistence;
        # their Cycles language can be unavailable without the renderer DSO.
        actual, expected = copy.deepcopy(actual), copy.deepcopy(expected)
        for path in set(actual["nodes"]) & set(expected["nodes"]):
            a, e = actual["nodes"][path], expected["nodes"][path]
            if e["type"] == "subnet":
                report.setdefault("offline_subnet_languages", []).append(
                    dict(
                        path=path,
                        actual=a["subnet_authoring"]["shader_language"],
                        authored=e["subnet_authoring"]["shader_language"],
                        check=label,
                    )
                )
                del a["subnet_authoring"]["shader_language"]
                del e["subnet_authoring"]["shader_language"]
            if not e["type"].startswith("cycles::"):
                continue
            for direction in ("input", "output"):
                del a[direction + "_types"]
                del e[direction + "_types"]
    if actual != expected:
        write(args.mode + "-mismatch-actual.json", actual)
        write(args.mode + "-mismatch-expected.json", expected)
        differences = []
        for path in sorted(set(actual["nodes"]) | set(expected["nodes"])):
            if actual["nodes"].get(path) != expected["nodes"].get(path):
                differences.append(path)
        raise AssertionError((label, differences, actual["bindings"], expected["bindings"]))
    report["checks"].append(label)


def keyframes(parm, pairs):
    for frame, number in pairs:
        key = hou.Keyframe()
        key.setFrame(frame)
        key.setValue(number)
        key.setExpression("linear()", hou.exprLanguage.Hscript)
        parm.setKeyframe(key)


def declared_inputs(subnet, names):
    # Built-in VOP subnets publish an unwired trailing insertion placeholder.
    # It is authored host structure and remains part of strict saved signatures.
    assert subnet.inputNames() == (*names, "next"), (subnet.path(), subnet.inputNames(), names)
    placeholder = subnet.inputNames().index("next")
    assert all(c.inputIndex() != placeholder for c in subnet.inputConnections()), subnet.path()


def rich_graph(ch, material, texture):
    """Identical node types with deliberately different per-instance interfaces."""
    outer = ch.create_subnet(material, "first_interface")
    hue = ch.create_connector(outer, "hue", type="color", default=(0.25, 0.5, 0.75))
    amount = ch.create_connector(outer, "amount", default=0.25)
    inner = ch.create_subnet(outer, "second_interface")
    gain = ch.create_connector(inner, "gain", default=0.9)
    pigment = ch.create_connector(inner, "pigment", type="color", default=(0.9, 0.8, 0.7))
    inner.setNamedInput("gain", amount, amount.outputNames()[0])
    inner.setNamedInput("pigment", hue, hue.outputNames()[0])
    palette = inner.createNode("cycles::color_ramp::1", "native_color_ramp")
    palette.parm("ramp_interpolation").set("NATIVE")
    palette.parm("ramp").set(
        hou.Ramp((hou.rampBasis.Linear,) * 2, (0.0, 1.0), ((0.125, 0.25, 0.5), (0.625, 0.75, 0.0)))
    )
    ch.promote_parameter(material, palette, "ramp", name="promoted_palette")
    palette.setNamedInput("factor", gain, gain.outputNames()[0])
    scalar = inner.createNode("cycles::float_curve::1", "native_scalar_ramp")
    scalar.parmTuple("curve_domain").set((0.0, 1.0))
    scalar.parm("curve_extension").set(1)
    scalar.parm("fac").set(1.0)
    scalar.parm("curve_value").set(
        hou.Ramp(
            (hou.rampBasis.Constant, hou.rampBasis.Linear, hou.rampBasis.Linear), (0.0, 0.35, 1.0), (0.2, 0.8, 0.4)
        )
    )
    scalar.setNamedInput("value", gain, gain.outputNames()[0])
    curve = inner.createNode("cycles::float_curve::1", "native_ranged_curve")
    curve.parmTuple("curve_domain").set((-1.0, 2.0))
    curve.parm("fac").set(0.75)
    curve.parm("curve_value").set(
        hou.Ramp(
            (hou.rampBasis.Linear, hou.rampBasis.CatmullRom, hou.rampBasis.Linear), (0.0, 0.4, 1.0), (-0.25, 1.5, 0.625)
        )
    )
    curve.setNamedInput("value", scalar, "value")
    alternate = inner.createNode("cycles::color::1", "offline_alternate")
    alternate.parmTuple("value").set((0.125, 0.125, 0.125))
    image = inner.createNode("cycles::image_texture::1", "multiple_output_image")
    image.setParms(dict(filename=str(texture), colorspace="data", alpha_type="ignore"))
    rgb = ch.create_connector(inner, "rgb_result", kind="output", type="color")
    rgb.setNamedInput(rgb.inputNames()[0], palette, "color")
    alpha = ch.create_connector(inner, "alpha_result", kind="output")
    alpha.setNamedInput(alpha.inputNames()[0], image, "alpha")
    opacity = ch.create_connector(outer, "opacity", kind="output")
    opacity.setNamedInput(opacity.inputNames()[0], inner, "alpha_result")
    shaded = ch.create_connector(outer, "shaded", kind="output", type="color")
    shaded.setNamedInput(shaded.inputNames()[0], inner, "rgb_result")
    # Preserve the image's other output independently of its alpha output.
    probe = inner.createNode("cycles::emission::1", "image_color_probe")
    # The default this test was written for (new nodes now take Blender's).
    probe.setParms(dict(strength=10.0))
    probe.setNamedInput("color", image, "color")
    factor = material.createNode("cycles::value::1", "absolute_external_reference")
    factor.parm("value").setExpression('ch("/obj/external_controls/tx") + $F * .0', hou.exprLanguage.Hscript)
    outer.setNamedInput("amount", factor, "value")
    ch.promote_parameter(material, amount, "floatdef", name="promoted_amount")

    # An unconnected BSDF input travels through both built-in groups without
    # inventing a closure, alongside an explicit emissive closure output chain.
    optional = ch.create_connector(outer, "optional_bsdf", type="bsdf")
    inner_optional = ch.create_connector(inner, "optional_closure", type="bsdf")
    inner.setNamedInput("optional_closure", optional, optional.outputNames()[0])
    inner_null = ch.create_connector(inner, "null_bsdf", kind="output", type="bsdf")
    inner_null.setInput(0, inner_optional)
    outer_null = ch.create_connector(outer, "optional_surface", kind="output", type="bsdf")
    outer_null.setNamedInput(outer_null.inputNames()[0], inner, "null_bsdf")
    closure = inner.createNode("cycles::emission::1", "nested_closure_emission")
    closure.setNamedInput("color", palette, "color")
    closure.parm("strength").set(2.0)
    inner_live = ch.create_connector(inner, "active_bsdf", kind="output", type="bsdf")
    inner_live.setNamedInput(inner_live.inputNames()[0], closure, "emission")
    outer_live = ch.create_connector(outer, "surface", kind="output", type="bsdf")
    outer_live.setNamedInput(outer_live.inputNames()[0], inner, "active_bsdf")
    declared_inputs(outer, ("hue", "amount", "optional_bsdf"))
    assert outer.outputNames() == ("opacity", "shaded", "optional_surface", "surface"), outer.outputNames()
    declared_inputs(inner, ("gain", "pigment", "optional_closure"))
    assert inner.outputNames() == ("rgb_result", "alpha_result", "null_bsdf", "active_bsdf"), inner.outputNames()
    assert not any(c.inputIndex() == outer.inputNames().index("optional_bsdf") for c in outer.inputConnections())
    if material.name() == "roundtrip_surface":
        emission = material.node("surface_shader")
        emission.setNamedInput("color", outer, "shaded")
        material.node("surface_output").setNamedInput("surface", outer, "surface")
        ch.promote_parameter(material, closure, "strength", name="animated_strength")
        keyframes(material.parm("animated_strength"), ((1, 2.0), (10, 4.0)))
    group = material.parmTemplateGroup()
    group.append(hou.FloatParmTemplate("archive_animation", "Authored animation", 1))
    material.setParmTemplateGroup(group)
    keyframes(material.parm("archive_animation"), ((1, 1.25), (10, 2.5)))


def edit_scene():
    """Run the same authored edits once natively and once without the plugin.

    Native prepare produces the independent expected edited signature; missing
    mode must reproduce it using only Houdini's retained dummy parameters/wires.
    """
    surface = hou.node("/mat/roundtrip_surface")
    inner = surface.node("first_interface/second_interface")
    output = inner.node("rgb_result_output")
    # Address independently retained original endpoints by name; both native
    # classes and missing-class persistence must keep their exact socket order.
    authored = json.loads((destination / "authored-signature.json").read_text(encoding="utf-8"))
    alternate = inner.node("offline_alternate")
    input_name = authored["nodes"][output.path()]["input_names"][0]
    output_name = authored["nodes"][alternate.path()]["output_names"][0]
    output.setNamedInput(input_name, alternate, output_name)
    inner.node("nested_closure_emission").setNamedInput("color", alternate, output_name)
    inner.node("offline_alternate").parmTuple("value").set((0.375, 0.5, 0.25))
    factor = surface.node("absolute_external_reference").parm("value")
    factor.setExpression('ch("/obj/external_controls/tx") + .125 + $F * .0', hou.exprLanguage.Hscript)
    strength = surface.parm("animated_strength")
    strength.deleteAllKeyframes()
    strength.set(3.0)
    hou.node("/obj/external_controls").parm("tx").set(0.375)
    hou.node("/obj/roundtrip_receiver").parm("tx").set(0.0625)
    surface.setComment("Intentional offline graph edits")
    surface.setUserData("artist_keep", "saved twice with Cycles absent")
    duplicate = hou.copyNodesTo((surface,), hou.node("/mat"))[0]
    duplicate.setName("intentional_duplicate_identity", unique_name=False)
    marker = surface.userData("cycles.offline.id")
    # Houdini copies user data, so the copy starts with the original's identity;
    # with the integration loaded, its next check gives the copy its own.
    assert marker and duplicate.userData("cycles.offline.id") == marker
    hou.node("/mat/intentional_delete").destroy()
    previous = hou.node("/mat/intentional_type_change")
    marker = previous.userData("cycles.offline.id")
    assert marker
    changed = previous.changeNodeType("add", keep_name=True, keep_parms=False, keep_network_contents=False)
    assert changed.type().name() == "add" and changed.userData("cycles.offline.id") == marker


def render_target(ch, expected, name):
    from cycles_houdini import device_selection, native_scene

    rop = hou.node("/out").createNode("cycles_ipr", "transient_roundtrip_render")
    try:
        device_selection.set_devices(rop, "CPU")
        rop.setParms(
            dict(
                source_mode="objects",
                objectroot="/obj",
                vobject="roundtrip_receiver",
                lights="",
                use_material_assignments=1,
                materialpath="",
                camera="/obj/roundtrip_camera",
                resx=32,
                resy=32,
                samples=8,
                seed=1701,
                animate_seed=0,
                use_denoise=0,
                adaptive_sampling=0,
                viewer_interactive=0,
                world_source="color",
            )
        )
        rop.parmTuple("background").set((0, 0, 0))
        with ch.Session(
            device="CPU",
            width=32,
            height=32,
            samples=8,
            interactive=False,
            threads=2,
            execution="worker",
            use_texture_cache=False,
        ) as session:
            frame = session.wait(session.apply(native_scene.capture(rop)), timeout=120)
            assert not session.status()["source_diagnostics"], session.status()
            pixels = bytes(frame["planes"]["combined"]["pixels"])
            numbers = array("f")
            numbers.frombytes(pixels)
            assert all(math.isfinite(x) for x in numbers)
            measured = list(numbers[(16 * 32 + 16) * 4 : (16 * 32 + 16) * 4 + 3])
            error = max(abs(a - b) for a, b in zip(measured, expected))
            assert error <= 2e-5, (name, measured, expected)
            report.setdefault("renders", []).append(
                dict(
                    name=name,
                    target="/mat/roundtrip_surface",
                    assignment=hou.node("/obj/roundtrip_receiver").evalParm("shop_materialpath"),
                    measured=measured,
                    expected=list(expected),
                    maximum_error=error,
                    tolerance=2e-5,
                    sha256=hashlib.sha256(pixels).hexdigest(),
                )
            )
            return pixels
    finally:
        rop.destroy()


try:
    if args.mode == "prepare":
        import numpy as np
        import OpenImageIO as oiio
        import cycles_houdini as ch
        import cycles_houdini_offline as offline

        assert offline._installed, "Use ordinary installed BeforeSave protection"
        hou.hipFile.clear(suppress_save_prompt=True)
        hou.setFrame(1)
        texture = destination / "native-alpha.exr"
        out = oiio.ImageOutput.create(str(texture))
        assert out.open(str(texture), oiio.ImageSpec(4, 4, 4, oiio.FLOAT))
        assert out.write_image(np.broadcast_to(np.array((0.2, 0.4, 0.6, 0.75), np.float32), (4, 4, 4)).copy())
        assert out.close()
        controls = hou.node("/obj").createNode("null", "external_controls")
        controls.parm("tx").set(0.25)
        roots = (
            ch.create_material(name="roundtrip_surface", shader="emission"),
            ch.create_material(name="roundtrip_volume", shader="principled_volume"),
            ch.create_light_material(name="roundtrip_light"),
            ch.create_world_material(name="roundtrip_world"),
        )
        assert {n.type().name() for n in roots} == set(offline.BUILDERS)
        for root in roots:
            rich_graph(ch, root, texture)
        ch.create_light_material(name="intentional_delete")
        ch.create_world_material(name="intentional_type_change")
        obj = hou.node("/obj").createNode("geo", "roundtrip_receiver", run_init_scripts=False)
        grid = obj.createNode("grid", "grid")
        grid.setParms(dict(type=0, orient=0, rows=2, cols=2))
        grid.parmTuple("size").set((8, 8))
        grid.setRenderFlag(True)
        obj.parm("shop_materialpath").set("/mat/roundtrip_surface")
        reader = hou.node("/obj").createNode("null", "external_reader")
        reader.parm("tx").setExpression('ch("/mat/roundtrip_surface/animated_strength")', hou.exprLanguage.Hscript)
        instance = hou.node("/obj").createNode("instance", "roundtrip_instance")
        instance.parm("instancepath").set(obj.path())
        camera = hou.node("/obj").createNode("cam", "roundtrip_camera")
        camera.setParms(dict(tz=5, projection=1, orthowidth=4))
        pixels = render_target(ch, (0.5, 0.75, 0.75), "authored native target")
        (destination / "authored.rgba.f32").write_bytes(pixels)
        hou.hipFile.save(str(original), save_to_recent_files=False)
        before = offline.statistics()
        save_seconds = []
        for _ in range(2):
            began = time.perf_counter()
            hou.hipFile.save(str(original), save_to_recent_files=False)
            save_seconds.append(time.perf_counter() - began)
        after = offline.statistics()
        report["unchanged_secondary_archive"] = dict(
            save_seconds=save_seconds, statistics={key: after[key] - before[key] for key in after if key in before}
        )
        assert after["serialized_roots"] - before["serialized_roots"] >= 12, report["unchanged_secondary_archive"]
        assert (
            (after["unchanged_roots"] - before["unchanged_roots"])
            + (after["refreshed_roots"] - before["refreshed_roots"])
        ) >= 12, report["unchanged_secondary_archive"]
        assert after["temporary_directories"] - before["temporary_directories"] == 2, report[
            "unchanged_secondary_archive"
        ]
        authored = signature()
        write("authored-signature.json", authored)
        load(original)
        equal(signature(), authored, "Ordinary save/load retains authored native graph")
        edit_scene()
        pixels = render_target(ch, (1.125, 1.5, 0.75), "independent edited native target")
        (destination / "edited-expected.rgba.f32").write_bytes(pixels)
        write("edited-expected-signature.json", signature())
        report["checks"].append(
            "Independent native edited oracle includes deletion, replacement and duplicate identities"
        )
        report["authored_nodes"] = len(authored["nodes"])
    elif args.mode == "missing":
        assert "cycles_houdini" not in sys.modules and "cycles_houdini_offline" not in sys.modules
        assert importlib.util.find_spec("cycles_houdini") is None
        assert importlib.util.find_spec("cycles_houdini_offline") is None
        assert hou.nodeType(hou.vopNodeTypeCategory(), "cycles::material::1") is None
        package_dirs = [Path(p) for p in os.environ.get("HOUDINI_PACKAGE_DIR", "").split(os.pathsep) if p]
        assert package_dirs and all(not list(p.glob("*.json")) for p in package_dirs), package_dirs
        report["plugin_absence"] = dict(
            python_module=False,
            offline_module=False,
            registered_material_type=False,
            package_directories=[str(p) for p in package_dirs],
        )
        load(original)
        authored = json.loads((destination / "authored-signature.json").read_text(encoding="utf-8"))
        equal(
            signature(), authored, "Plugin-absent load preserves exact original dummy types, ports, channels and wires"
        )
        for path, row in authored["nodes"].items():
            if row["type"].startswith("cycles::"):
                kind = hou.node(path).type()
                assert kind.name() == row["type"]
                definition = kind.definition()
                library = definition.libraryFilePath() if definition is not None else None
                report.setdefault("missing_definition_libraries", {})[path] = library
                # Houdini may expose the HIP's reduced native definition as an
                # Embedded HDADefinition. An external HDA or package would not
                # establish the genuinely plugin-absent persistence contract.
                assert library is None or library.casefold() == "embedded", (
                    path,
                    "Unexpected external operator definition",
                    library,
                )
        edit_scene()
        expected = json.loads((destination / "edited-expected-signature.json").read_text(encoding="utf-8"))
        equal(signature(), expected, "Offline edits match independently authored native expected graph")
        for target in (save_as_one, save_as_two):
            hou.hipFile.save(str(target), save_to_recent_files=False)
            load(target)
            equal(signature(), expected, "Plugin-absent Save As and reload: " + target.name)
        assert "cycles_houdini" not in sys.modules and "cycles_houdini_offline" not in sys.modules
    else:
        import cycles_houdini as ch

        load(save_as_two)
        expected = json.loads((destination / "edited-expected-signature.json").read_text(encoding="utf-8"))
        actual = signature()
        # The plugin-absent saves kept the copy's duplicated identity; loading with the
        # integration gives the copy its own. Every other authored field stays strict.
        copied = "/mat/intentional_duplicate_identity"
        expected_marker = expected["nodes"][copied]["userdata"].pop("cycles.offline.id")
        actual_marker = actual["nodes"][copied]["userdata"].pop("cycles.offline.id")
        assert actual_marker and actual_marker != expected_marker, (actual_marker, expected_marker)
        equal(actual, expected, "Real native reload retains exact edited graph in its original paths")
        for node in hou.node("/mat").allSubChildren():
            if node.type().name() == "subnet" or node.type().name().startswith("cycles::"):
                assert node.shaderLanguageName() == "Cycles", (node.path(), node.shaderLanguageName())
        capabilities = ch.capabilities()
        assert capabilities["image_iris"], "Image Iris must remain enabled"
        report["native_runtime"] = dict(
            python=ch.__file__,
            native=ch._native.__file__,
            capabilities={k: capabilities[k] for k in ("native_ipr", "image_iris", "engine_commit")},
        )
        assert hou.node("/mat/intentional_delete") is None
        assert hou.node("/mat/intentional_type_change").type().name() == "add"
        # The original at the archived path keeps its identity.
        original_marker = hou.node("/mat/roundtrip_surface").userData("cycles.offline.id")
        copy_marker = hou.node("/mat/intentional_duplicate_identity").userData("cycles.offline.id")
        assert original_marker and copy_marker and original_marker != copy_marker
        pixels = render_target(ch, (1.125, 1.5, 0.75), "restored genuine target after plugin-absent Save As twice")
        assert pixels == (destination / "edited-expected.rgba.f32").read_bytes(), (
            "Fixed-seed target differs from independent edited native reference"
        )
        report["checks"].append("Genuine assigned target pixels exactly match independent edited native reference")
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    report["wall_seconds"] = time.monotonic() - started
    write(args.mode + ".json", report)
    print(json.dumps(dict(result=report["result"], mode=args.mode, report=str(destination / (args.mode + ".json")))))
