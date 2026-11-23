# SPDX-License-Identifier: Apache-2.0
"""Generate F1 pages from the registered Cycles interfaces without rendering.

Run under the qualified build's hython/package environment:
    hython scripts/generate-node-help.py HOST/help --coverage REPORT.json

The SideFX help API chooses the namespaced/versioned file paths. The generator
does not change node definitions, preferences, saved scenes or native binaries.
It fails on undocumented artist-facing controls so new sockets cannot silently
ship with a repeated node synopsis. Hidden implementation controls are excluded.
"""

from __future__ import annotations

import argparse
from collections import Counter
import importlib.util
import json
from pathlib import Path
import re

import hou
from houdinihelp import api as help_api

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "cycles_shader_help", ROOT / "houdini/python3.13libs/cycles_houdini/materials/shader_help.py"
)
help_data = importlib.util.module_from_spec(spec)
spec.loader.exec_module(help_data)


def metadata():
    """Reuse the authored node synopsis, not the private engine type catalog."""
    source = (ROOT / "src/houdini/material_vop.cpp").read_text(encoding="utf-8")
    from shader_help_metadata import shader_help_metadata

    return shader_help_metadata(source)


def visible_templates(group):
    """Include conditional controls, including inactive modes, but not internals."""
    return help_data.visible_parameters(group)


def escape_heading(text):
    return text.replace("\n", " ").replace(":", " ")


def card_header(title, category, kind, summary):
    return [
        f"= {title} =",
        "#type: node",
        f"#context: {category.lower()}",
        "#namespace: cycles",
        f"#internal: {kind}",
        "#version: 1",
        "",
        f'"""{summary}"""',
        "",
    ]


def parm_block(parm, description):
    # #id is essential: shortened labels such as Weight occur in several
    # Principled panels and are not reliable identifiers for hover tooltips.
    lines = [f"{escape_heading(parm.label())}:", f"    #id: {parm.name()}", ""]
    lines.extend("    " + paragraph for paragraph in description.splitlines())
    lines.append("")
    return lines


def node_page(node, authored):
    kind = node.type().name().split("::")[1]
    if kind in help_data.BUILDERS:
        title, summary, workflow = help_data.BUILDERS[kind]
        category, original_summary = "Material", ""
    elif kind in help_data.OUTPUTS:
        title, summary, workflow = help_data.OUTPUTS[kind]
        category, original_summary = "Output", ""
    else:
        label, category, original_summary = authored[kind]
        title, summary = "Cycles " + label, original_summary
        workflow = ""
    lines = card_header(title, "vop", kind, summary)
    if workflow:
        lines.extend(["== Usage ==", "", workflow, ""])
    if kind not in help_data.BUILDERS and kind not in help_data.OUTPUTS:
        lines.extend(
            [
                "== Usage ==",
                "",
                "Create this node inside a Cycles builder that supports its shading domain. Connect its output to a downstream input and finish the network at the matching Cycles output node. The shader guide below explains builder selection and assignment.",
                "",
                "Editable input values are defaults used when that input is unconnected. A wire replaces the corresponding default. Houdini animation and channel references are evaluated at render time. Controls for inactive modes remain documented here even when the parameter pane hides them.",
                "",
            ]
        )
    parms = []
    missing = []
    templates = visible_templates(node.parmTemplateGroup())
    if templates:
        lines.extend(["@parameters", ""])
    for parm in templates:
        existing = parm.help()
        if kind == "material" and parm.name() == "cycles_displacement_method":
            existing = ""  # Old whole-builder synopsis described this one control.
        description, origin = help_data.parameter_description(kind, parm.name(), existing, original_summary)
        if not description:
            missing.append({"type": node.type().name(), "name": parm.name(), "label": parm.label()})
            continue
        lines.extend(parm_block(parm, description))
        parms.append({"name": parm.name(), "label": parm.label(), "origin": origin})
    for direction, names, types in (
        ("Inputs", node.inputNames(), node.inputDataTypes()),
        ("Outputs", node.outputNames(), node.outputDataTypes()),
    ):
        if names:
            lines.extend([f"== {direction} ==", ""])
            lines.extend(f"* `{name}` ({data_type})" for name, data_type in zip(names, types))
            lines.append("")
    lines.extend(
        [
            "== See also ==",
            "",
            "[Cycles shader guide|/cycles/shaders]",
            "",
            "[Cycles Light|/nodes/obj/cycles--light-1]",
            "",
        ]
    )
    return "\n".join(lines), category, parms, missing


def light_page(node):
    return help_data.light_help_card(node.parmTemplateGroup())


def guide(inventory):
    lines = [
        "= Cycles shaders and materials =",
        "",
        '"""Build surface, volume, world and analytic-light shading networks with the Cycles VOP nodes."""',
        "",
        "== Choose a builder ==",
        "",
    ]
    for kind, (title, summary, workflow) in help_data.BUILDERS.items():
        lines.extend([f"[{title}|/nodes/vop/cycles--{kind}-1]:", "    " + summary, "", "    " + workflow, ""])
    lines.extend(
        [
            "== Connect and assign ==",
            "",
            "Create a builder in /mat, dive inside, and finish the network at its matching output. Assign a surface or volume builder using the Houdini material path. Select a world builder on the Cycles ROP; assign a light builder on the Cycles Light Emission Material control. Surface and volume shaders have different domains and cannot be interchanged.",
            "",
            "A connected VOP input replaces its editable default. Values and file expressions follow the render frame; procedural coordinates determine whether a pattern follows the object or remains fixed in the scene. UV Map reads Houdini uv or a named map. Mapping transforms a chosen coordinate signal. Most procedural textures use world position when Vector is unconnected; image textures use the default uv map. Environment textures use a world direction.",
            "",
            "== Color and data textures ==",
            "",
            "Color values are scene-linear in the active render working space. Image Auto interpretation follows Houdini OCIO, file rules and image metadata. Use Data for normal, displacement and other numeric maps so a color transform does not distort their values. A display transform or display exposure changes the viewed image, while shader emission strength and light power change illumination.",
            "",
            "== Displacement and volumes ==",
            "",
            "Bump changes the shading normal. True displacement moves mesh positions, and Both retains bump detail on displaced geometry. Supply sufficient geometry detail and select the method on the surface builder. For sparse volumes, use the correct grid names. Principled Volume can multiply density by a named grid; simple scatter and absorption nodes need explicit field connections. The constant Density value does not automatically read a VDB field.",
            "",
            "== Diagnostics ==",
            "",
            "Missing textures, fields and incompatible graph inputs are reported in the Cycles diagnostics. Inspect the reported node and resource before increasing samples. Shader graphs have a flattened limit of 256 nodes. Constant switches and disabled outputs can prune unused branches; varying choices retain the dependencies needed at shading time.",
            "",
            "== Node reference ==",
            "",
        ]
    )
    categories = sorted({entry["category"] for entry in inventory if entry["context"] == "vop"})
    for category in categories:
        lines.extend([f"=== {category} ===", ""])
        for entry in sorted(
            (e for e in inventory if e["context"] == "vop" and e["category"] == category), key=lambda e: e["title"]
        ):
            lines.extend([f"* [{entry['title']}|{entry['path']}]", ""])
    lines.extend(
        [
            "== Related guides ==",
            "",
            "[Cycles Light|/nodes/obj/cycles--light-1]",
            "",
            "[Cycles camera controls|/cycles/camera]",
            "",
            "[Cycles object controls|/cycles/objects]",
            "",
            "[Cycles Render Viewer|/cycles/viewer]",
            "",
        ]
    )
    return "\n".join(lines)


def generate(destination, coverage):
    destination.mkdir(parents=True, exist_ok=True)
    authored = metadata()
    inventory, missing = [], []
    vop_types = sorted(
        (name, node_type)
        for name, node_type in hou.vopNodeTypeCategory().nodeTypes().items()
        if name.startswith("cycles::")
    )
    if not vop_types:
        raise RuntimeError("No Cycles VOP types registered; run with the qualified host package environment")
    for name, node_type in vop_types:
        node = hou.node("/mat").createNode(name, "__cycles_help_inventory", run_init_scripts=False)
        try:
            content, category, parms, uncovered = node_page(node, authored)
            page_path = help_api.nodetype_to_path(node_type, snap_version=False)
            output = destination / (page_path.lstrip("/") + ".txt")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(content, encoding="utf-8")
            inventory.append(
                {
                    "type": name,
                    "context": "vop",
                    "category": category,
                    "title": node_type.description(),
                    "path": page_path,
                    "parameters": parms,
                }
            )
            missing.extend(uncovered)
        finally:
            node.destroy()
    light_type = hou.nodeType(hou.objNodeTypeCategory(), "cycles::light::1")
    if light_type is None:
        raise RuntimeError("Cycles Light HDA is not registered; generate/install it before generating help")
    light = hou.node("/obj").createNode(light_type.name(), "__cycles_light_help_inventory", run_init_scripts=False)
    try:
        content, parms, uncovered = light_page(light)
        page_path = help_api.nodetype_to_path(light_type, snap_version=False)
        output = destination / (page_path.lstrip("/") + ".txt")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8")
        inventory.append(
            {
                "type": light_type.name(),
                "context": "obj",
                "category": "Light",
                "title": "Cycles Light",
                "path": page_path,
                "parameters": parms,
            }
        )
        missing.extend(uncovered)
    finally:
        light.destroy()
    index = destination / "cycles/shaders.txt"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(guide(inventory), encoding="utf-8")
    report = {
        "schema_version": 1,
        "houdini_version": hou.applicationVersionString(),
        "node_count": len(inventory),
        "parameter_count": sum(len(e["parameters"]) for e in inventory),
        "description_origins": dict(sorted(Counter(p["origin"] for e in inventory for p in e["parameters"]).items())),
        "missing": missing,
        "nodes": inventory,
    }
    coverage.parent.mkdir(parents=True, exist_ok=True)
    coverage.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if missing:
        raise RuntimeError("Undocumented Cycles controls: " + json.dumps(missing))
    print(
        f"Generated {len(inventory)} Cycles node helpcards, {report['parameter_count']} parameter descriptions and /cycles/shaders"
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--coverage", required=True, type=Path)
    args = parser.parse_args()
    generate(args.destination.resolve(), args.coverage.resolve())
