# SPDX-License-Identifier: Apache-2.0
"""Every Blender shader node type, setting and enum item through the Blender material import.

    hython tests/houdini_blend_node_coverage_smoke.py [--work DIR] [--blender EXE] [--report FILE]

Blender itself (the bridge's executable, factory settings) lists every node type it can create in a
material or world tree, every RNA setting of each (BOOLEAN, INT, FLOAT, STRING, ENUM) with its
enum items, and records each node as blend_material_export does: once with Blender's defaults and
once per setting with a non-default value (every enum item; for Mix and Map Range also per data
type). Houdini builds each record's node through blend_materials._Builder (settings and sockets,
as an import does) and checks:
- the exporter carried every setting (none dropped);
- the import neither fails nor leaves a setting unmapped, except for the listed node types and
  values it refuses by name (EXPECTED_UNIMPORTED_TYPES, EXPECTED_UNSUPPORTED);
- every available input socket of every variant, linked (hidden ones such as Normal and Tangent
  included), maps to a native input that takes a wire;
- the native node holds Blender's value: numbers and toggles equal (angles converted where the
  native parameter shows degrees), strings carried, and distinct enum items give distinct tokens.
A Blender node type, setting or enum item that is new (or a listed refusal that now imports) fails
the test, so the lists stay exact. --exporter/--importer run older copies of the two modules
(before-fix evidence). Prints a JSON summary; exits non-zero when a check failed.
"""

import argparse
import importlib.util
import json
import math
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

import hou

ROOT = Path(__file__).resolve().parents[1]
EXPORTER = ROOT / "blender/cycles_blender/blend_material_export.py"

# Blender shader node types the material import has no native node for: idname -> reason.
# (Function, bundle, closure and string nodes are not shader nodes and are not listed.)
EXPECTED_UNIMPORTED_TYPES = {
    "ShaderNodeBsdfRayPortal": "no Ray Portal BSDF translation yet",
    "ShaderNodeEeveeSpecular": "EEVEE only",
    "ShaderNodeLightAccumulation": "light tree node",
    "ShaderNodeLightEvaluation": "light tree node",
    "ShaderNodeLightInfo": "light tree node",
    "ShaderNodeLightIterInternalInput": "light tree node",
    "ShaderNodeLightIterInternalOutput": "light tree node",
    "ShaderNodeOutputAOV": "AOV outputs are not imported",
    "ShaderNodeRadialTiling": "no Radial Tiling translation yet",
    "ShaderNodeRaycast": "no Raycast translation yet",
    "ShaderNodeScript": "OSL scripts are not imported",
    "ShaderNodeShaderToRGB": "EEVEE only",
    "ShaderNodeShadowRaycast": "light tree node",
    "ShaderNodeSqueeze": "legacy node without a Cycles implementation",
    "ShaderNodeTexIES": "IES belongs to lights",
    "ShaderNodeUVAlongStroke": "Freestyle",
    "ShaderNodeVolumeInfo": "no Volume Info translation yet",
    "ShaderNodeGroup": "expanded by the exporter",
    "ShaderNodeParticleInfo": "the pinned engine catalogue has no graph adapter for Particle Info",
}
# Known gaps outside the import code: idname -> (text of the import error, reason). Every
# variant of the type must fail with that text; when one imports, the entry is stale.
# (Fresnel and Layer Weight Normal, the last entries, became native inputs on 2026-11-15.)
KNOWN_GAPS = {}
# Source nodes for the linked-input check: Blender socket type -> (native type, output).
LINK_SOURCES = {
    "SHADER": ("emission", "emission"),
    "VECTOR": ("combine_xyz", "vector"),
    "RGBA": ("color", "color"),
}
# Values the import refuses by name: (idname, setting, value) -> reason. Each must fail with
# a message naming the value; each must still be refused (a listed refusal that imports fails).
EXPECTED_UNSUPPORTED = {
    ("ShaderNodeAttribute", "attribute_type", "VIEW_LAYER"): "view-layer attributes have no Houdini source",
    ("ShaderNodeAttribute", "attribute_type", "LIGHT"): "light attributes: the pinned Cycles has none",
    ("ShaderNodeVectorTransform", "convert_from", "LIGHT"): "the pinned Cycles has no light space",
    ("ShaderNodeVectorTransform", "convert_to", "LIGHT"): "the pinned Cycles has no light space",
    ("ShaderNodeTexCoord", "from_instancer", True): "From Instancer needs a separate adapter",
    ("ShaderNodeUVMap", "from_instancer", True): "From Instancer needs a separate adapter",
}
# FLOAT settings with RNA subtype ANGLE: (idname, setting) -> how the native parameter shows them.
ANGLES = {
    ("ShaderNodeTexSky", "sun_elevation"): "degrees",
    ("ShaderNodeTexSky", "sun_size"): "degrees",
    ("ShaderNodeTexSky", "sun_rotation"): "degrees",
}
# Settings consumed without a parameter of their own: the Attribute type selects the
# attribute name's encoding (tests/pytest_cases/test_blend_object_values.py), the Map Range
# data type the native node (blend_materials.node_kind).
CONSUMED = {("ShaderNodeAttribute", "attribute_type"), ("ShaderNodeMapRange", "data_type")}

BLENDER_SCRIPT = r"""
# Blender: every shader node type with every setting varied, as blend_material_export records it.
import importlib.util
import json
import os
import sys
import tempfile

import bpy

exporter_path, out = sys.argv[sys.argv.index("--") + 1 :][:2]
# The exporter imports shared Apache-2.0 helpers from the Houdini package (<checkout>/houdini/python3.13libs).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(exporter_path)))), "houdini", "python3.13libs"))
spec = importlib.util.spec_from_file_location("blend_material_export", exporter_path)
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.context.scene.render.engine = "CYCLES"
staging = tempfile.mkdtemp(prefix="cycles_node_coverage_")
image = bpy.data.images.new("coverage", 4, 4)
image.filepath_raw = os.path.join(staging, "coverage.png")
image.file_format = "PNG"
image.save()
image.source = "FILE"
material = bpy.data.materials.new("Coverage")
material.use_nodes = True
world = bpy.data.worlds.new("Coverage")
world.use_nodes = True
trees = (("material", material.node_tree), ("world", world.node_tree))
SKIP_BASES = ("Node", "ShaderNode", "NodeInternal", "NodeCustomGroup", "ShaderNodeCustomGroup")
types = sorted(
    name
    for name in dir(bpy.types)
    if isinstance(getattr(bpy.types, name), type)
    and issubclass(getattr(bpy.types, name), bpy.types.Node)
    and name not in SKIP_BASES
    and name.startswith(("ShaderNode", "FunctionNode", "Node"))
)
# Settings that change which other settings and sockets apply: each value is a base state.
BASES = {
    "ShaderNodeMix": [{}, {"data_type": "RGBA"}, {"data_type": "VECTOR"}, {"data_type": "VECTOR", "factor_mode": "NON_UNIFORM"}],
    "ShaderNodeMapRange": [{}, {"data_type": "FLOAT_VECTOR"}],
}


def plain(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    try:
        return exporter._plain(value)
    except Exception:  # noqa: BLE001
        return str(value)


def describe(node):
    result = {}
    common = {p.identifier for p in bpy.types.ShaderNode.bl_rna.properties}
    for p in node.bl_rna.properties:
        if p.identifier in common or p.identifier == "rna_type":
            continue
        entry = dict(type=p.type, default=plain(getattr(node, p.identifier)) if p.type not in ("POINTER", "COLLECTION") else None)
        if p.type == "ENUM":
            entry["items"] = [i.identifier for i in p.enum_items]
            entry["flag"] = bool(p.is_enum_flag)
        if p.type in ("INT", "FLOAT"):
            entry.update(soft_min=p.soft_min, soft_max=p.soft_max, array=p.array_length, subtype=p.subtype)
        entry["readonly"] = bool(p.is_readonly)
        result[p.identifier] = entry
    return result


def other_value(entry):
    # A non-default value for a setting (None when it has none).
    kind, default = entry["type"], entry["default"]
    if kind == "BOOLEAN" and not entry.get("array"):
        return [not default]
    if kind == "STRING":
        return ["coverage_name"]
    if kind in ("INT", "FLOAT"):
        if entry.get("array"):
            return [[(v + 0.25) * 0.5 for v in default]] if kind == "FLOAT" else None
        low, high = entry["soft_min"], entry["soft_max"]
        value = default + (1 if kind == "INT" else 0.37)
        if value > high:
            value = default - (1 if kind == "INT" else 0.37)
        return [value] if low <= value <= high else None
    if kind == "ENUM" and not entry.get("flag"):
        return [item for item in entry["items"] if item != default]
    return None


def export(node):
    graph = exporter._Graph(exporter._Images(staging, "in_place"))
    outputs = [s for s in node.outputs if not getattr(s, "is_unavailable", False) and s.enabled]
    if not outputs:
        return None, "no outputs"
    try:
        graph.output(outputs[0], ())
        return graph.nodes[0], None
    except Exception as error:  # noqa: BLE001
        return None, str(error) or type(error).__name__


def create(idname, settings):
    for where, tree in trees:
        try:
            node = tree.nodes.new(idname)
        except Exception:  # noqa: BLE001
            continue
        if hasattr(node, "image"):
            node.image = image
        try:
            for name, value in settings.items():
                setattr(node, name, value)
        except Exception as error:  # noqa: BLE001
            tree.nodes.remove(node)
            return None, where, "cannot set " + json.dumps(settings) + ": " + str(error)
        return node, where, None
    return None, None, "cannot be created in a material or world"


result = dict(version=bpy.app.version_string, types={})
for idname in types:
    node, where, error = create(idname, {})
    if node is None:
        result["types"][idname] = dict(created=False, error=error)
        continue
    rna = describe(node)
    node.id_data.nodes.remove(node)
    variants = []
    for base in BASES.get(idname, [{}]):
        jobs = [(None, None)]
        for name, entry in rna.items():
            if entry["readonly"] or name in base:
                continue
            for value in other_value(entry) or ():
                jobs.append((name, value))
        for name, value in jobs:
            settings = dict(base, **({name: value} if name else {}))
            node, where, error = create(idname, settings)
            record = dict(base=base, setting=name, value=value)
            if node is None:
                record["error"] = error
            else:
                if name:
                    record["value"] = plain(getattr(node, name))  # what Blender kept
                record["item"], record["export_error"] = export(node)
                node.id_data.nodes.remove(node)
            variants.append(record)
    result["types"][idname] = dict(created=True, tree=where, settings=rna, variants=variants)
import PyOpenColorIO as ocio

working = ocio.GetCurrentConfig().getColorSpace(ocio.ROLE_SCENE_LINEAR)
result["color_management"] = dict(colorspace=working.getName(), color_interop_id=exporter._color_identity(working))
with open(out, "w", encoding="utf-8") as handle:
    json.dump(result, handle, separators=(",", ":"), default=str)
print("COVERAGE_DONE", len(result["types"]))
"""


def load(name, path):
    spec = importlib.util.spec_from_file_location("cycles_houdini.blender." + name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def blender_dump(executable, exporter, work):
    from cycles_houdini.blender.blend_worker import blender_environment

    script, out = work / "node_coverage_blender.py", work / "node_coverage.json"
    script.write_text(BLENDER_SCRIPT, encoding="utf-8")
    completed = subprocess.run(
        [executable, "-b", "--factory-startup", "--python", str(script), "--", str(exporter), str(out)],
        timeout=600,
        capture_output=True,
        text=True,
        env=blender_environment(hou.getenv("CYCLES_HOUDINI_BLENDER_OCIO") or None),
    )
    if "COVERAGE_DONE" not in completed.stdout:
        raise RuntimeError("Blender dump failed:\n" + completed.stdout[-3000:] + completed.stderr[-3000:])
    return json.loads(out.read_text(encoding="utf-8"))


def native_value(host, key):
    parm = host.parmTuple(key)
    if parm is None:
        return None
    if parm.parmTemplate().type() in (hou.parmTemplateType.String, hou.parmTemplateType.Menu):
        values = [p.evalAsString() for p in parm]
    else:
        values = list(parm.eval())
    return values[0] if len(values) == 1 else values


def same_number(blender, native, tolerance=1e-5):
    if isinstance(blender, (list, tuple)):
        return (
            isinstance(native, (list, tuple))
            and len(native) >= len(blender)
            and all(same_number(a, b, tolerance) for a, b in zip(blender, native))
        )
    if isinstance(blender, bool):
        return bool(native) == blender
    try:
        return abs(float(native) - float(blender)) <= tolerance * max(1.0, abs(float(blender)))
    except (TypeError, ValueError):
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--blender", default=None)
    parser.add_argument("--exporter", type=Path, default=EXPORTER, help="blend_material_export.py to run in Blender")
    parser.add_argument("--importer", type=Path, default=None, help="a blend_materials.py copy to build with")
    args = parser.parse_args()
    import cycles_houdini
    from cycles_houdini.blender import blend_file_node

    if args.importer:
        bm = load("coverage_importer", args.importer.resolve())
    else:
        from cycles_houdini.blender import blend_materials as bm
    node_kind = getattr(bm, "node_kind", None) or (
        lambda item, world: bm._TYPES.get(item["type"].removeprefix("ShaderNode"))
        or (bm._WORLD_TYPES.get(item["type"].removeprefix("ShaderNode")) if world else None)
    )
    unread = getattr(bm, "UNREAD_SETTINGS", {})
    work = (args.work or Path(tempfile.mkdtemp(prefix="cycles_node_coverage_"))).resolve()
    work.mkdir(parents=True, exist_ok=True)
    dump = blender_dump(args.blender or blend_file_node.blender_executable(), args.exporter.resolve(), work)
    catalogue = {d["engine_type"]: d for d in cycles_houdini.shader_catalogue()["definitions"]}
    surface = {k: d for k, d in catalogue.items() if d.get("surface_graph_adapter") or d.get("volume_graph_adapter")}
    world = {k: d for k, d in catalogue.items() if d.get("world_graph_adapter")}
    failures, refused, tokens, unimported, gaps = [], {}, {}, set(), {}
    checked = dict(types=0, variants=0, settings=0, linked_inputs=0)
    linked_seen = set()

    def fail(*parts):
        failures.append(" | ".join(str(p) for p in parts))

    mat = hou.node("/mat")
    for idname, entry in sorted(dump["types"].items()):
        if not entry["created"] or not idname.startswith("ShaderNode"):
            continue
        variants = [v for v in entry["variants"] if v.get("item")]
        if not variants:
            continue  # nothing an export can record (output nodes have no outputs)
        is_world = idname.removeprefix("ShaderNode") in bm._WORLD_TYPES
        definitions = world if is_world else surface
        kinds = {node_kind(v["item"], is_world) for v in variants}
        if None in kinds or any(k not in definitions and k != "color_ramp" for k in kinds):
            unimported.add(idname)
            continue
        checked["types"] += 1
        record = dict(
            name="coverage", key="coverage_" + idname, nodes=[], outputs={}, color_management=dump["color_management"]
        )
        builder = bm._Builder(record, str(work), mat, definitions, world=is_world)
        settings = entry["settings"]
        plain = {n for n, s in settings.items() if s["type"] in ("BOOLEAN", "INT", "FLOAT", "STRING", "ENUM")}
        for name, setting in settings.items():
            if setting["type"] == "FLOAT" and setting.get("subtype") == "ANGLE" and (idname, name) not in ANGLES:
                fail(idname, name, "angle setting without a decided native unit (add it to ANGLES)")
        for variant in variants:
            checked["variants"] += 1
            item = dict(variant["item"], id="n0")
            label = (idname, json.dumps(variant["base"], sort_keys=True), variant["setting"], variant["value"])
            dropped = sorted(plain - set(item["properties"]))
            if dropped:
                fail(*label, "exporter dropped settings: " + ", ".join(dropped))
            value_key = tuple(variant["value"]) if isinstance(variant["value"], list) else variant["value"]
            expected_refusal = (idname, variant["setting"], value_key) in EXPECTED_UNSUPPORTED
            kind = node_kind(item, is_world)
            builder.items, builder.kinds, builder.applied = {"n0": item}, {"n0": kind}, {}
            host = builder.host.createNode("cycles::" + kind + "::1", "probe")
            builder.nodes = {"n0": host}
            try:
                builder.properties(item)
                inputs = builder.sockets(item, "inputs")
                builder.sockets(item, "outputs")
                # Unconnected input values land on the parameter the import writes.
                for socket in item["inputs"]:
                    name = inputs.get(socket["index"])
                    native = (
                        next((s for s in definitions[kind]["inputs"] if s.get("host_id", s["id"]) == name), {})
                        if kind in definitions
                        else {}
                    )
                    if (
                        name is None
                        or "value" not in socket
                        or socket["type"] == "SHADER"
                        or native.get("default_links")
                    ):
                        continue
                    value = socket["value"]
                    value = value[:3] if isinstance(value, list) and len(value) == 4 else value
                    builder.parameter(host, builder.parameter_name(item, name), value)
                # Every available input, linked: it maps to a native input that takes the wire.
                for socket in item["inputs"]:
                    key = (idname, socket["identifier"], socket["type"])
                    if not socket.get("enabled", True) or key in linked_seen:
                        continue
                    linked_seen.add(key)
                    linked = dict(item, inputs=[dict(socket, source=dict(node="n_source", socket=0))])
                    try:
                        name = builder.sockets(linked, "inputs").get(socket["index"])
                    except Exception as caught:  # noqa: BLE001
                        fail(idname, "linked input", socket["name"], socket["identifier"], str(caught))
                        continue
                    kind_out = LINK_SOURCES.get(socket["type"], ("value", "value"))
                    if is_world and kind_out[0] == "emission":
                        kind_out = ("background_shader", "background")
                    source = builder.host.createNode("cycles::" + kind_out[0] + "::1", "link_source")
                    try:
                        host.setNamedInput(name, source, kind_out[1])
                        index = host.inputIndex(name)
                        if index < 0 or host.input(index) != source:
                            fail(idname, "linked input", socket["name"], name, "the native input holds no wire")
                    except Exception as caught:  # noqa: BLE001
                        fail(idname, "linked input", socket["name"], name, "wire refused: " + str(caught))
                    finally:
                        source.destroy()
                    checked["linked_inputs"] += 1
                error = None
            except Exception as caught:  # noqa: BLE001
                error = str(caught) or type(caught).__name__
            if idname in KNOWN_GAPS:
                gaps.setdefault(idname, []).append(error)
                if error is None or KNOWN_GAPS[idname][0] not in error:
                    fail(*label, "listed in KNOWN_GAPS, but: " + str(error))
                host.destroy()
                continue
            try:
                if expected_refusal:
                    refused[label] = error
                    if error is None:
                        fail(*label, "listed as unsupported, but it imports: update EXPECTED_UNSUPPORTED")
                    elif str(variant["value"]) not in error and str(variant["setting"]) not in error:
                        fail(*label, "refusal does not name the setting or value: " + error)
                    continue
                if error is not None:
                    fail(*label, "import failed: " + error)
                    continue
                applied = builder.applied.get("n0", {})
                for name, value in item["properties"].items():
                    if name not in plain:
                        continue
                    checked["settings"] += 1
                    if name not in applied:
                        fail(*label, name, "setting not consumed by the import")
                        continue
                    key = applied[name]
                    if key is None:
                        if (idname, name) not in unread and (idname, name) not in CONSUMED:
                            fail(*label, name, "consumed without a parameter, not listed as unread")
                        continue
                    native = native_value(host, key)
                    if settings[name]["type"] == "ENUM":
                        tokens.setdefault((idname, label[1], name), {}).setdefault(value, set()).add(native)
                    elif settings[name]["type"] == "STRING":
                        if value and value not in str(native):
                            fail(*label, name, f"string {value!r} not carried: {native!r}")
                    else:
                        expected = math.degrees(value) if ANGLES.get((idname, name)) == "degrees" else value
                        if not same_number(expected, native):
                            fail(*label, name, f"native {key} = {native!r}, Blender {value!r} (expected {expected!r})")
            finally:
                host.destroy()
        builder.host.destroy()
    # Each Blender enum item gives one native token, and distinct items distinct tokens.
    for (idname, base, name), by_item in sorted(tokens.items()):
        seen = {}
        for item, natives in by_item.items():
            if len(natives) != 1:
                fail(idname, base, name, item, "several native tokens: " + str(sorted(map(str, natives))))
            for native in natives:
                seen.setdefault(native, []).append(item)
        for native, items in seen.items():
            if len(items) > 1:
                fail(idname, base, name, "items share native token " + repr(native) + ": " + ", ".join(items))
    for idname in sorted(unimported - set(EXPECTED_UNIMPORTED_TYPES)):
        fail(idname, "Blender node type the import has no native node for (new? translate it or list it)")
    for idname in sorted(set(EXPECTED_UNIMPORTED_TYPES) - unimported):
        entry = dump["types"].get(idname) or {}
        if entry.get("created") and any(v.get("item") for v in entry["variants"]):
            fail(idname, "listed as unimported, but it imports: update EXPECTED_UNIMPORTED_TYPES")
    summary = dict(
        blender=dump["version"],
        checked=checked,
        unimported_types={k: EXPECTED_UNIMPORTED_TYPES.get(k, "NEW") for k in sorted(unimported)},
        refused={" ".join(map(str, (k[0], k[2], k[3]))): v for k, v in refused.items()},
        known_gaps={k: KNOWN_GAPS[k][1] for k in sorted(gaps)},
        failures=failures,
        work=str(work).replace("\\", "/"),
    )
    text = json.dumps(summary, indent=1, default=str)
    print("SUMMARY:" + text)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        traceback.print_exc()
        code = 1
    sys.stdout.flush()
    hou.exit(code, suppress_save_prompt=True)
    os._exit(code)
