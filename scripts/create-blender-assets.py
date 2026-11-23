# SPDX-License-Identifier: Apache-2.0
"""Author the Cycles Blender File SOP and Cycles Blender Hierarchy object assets.

    hython scripts/create-blender-assets.py OUTPUT_FOLDER

Writes OUTPUT_FOLDER/cycles_blender_file.hda and OUTPUT_FOLDER/cycles_blender_hierarchy.hda
(blend-import.md: Asset nodes). Needs the native cycles::blender_reference SOP
(the Cycles package environment). Idempotent: existing outputs are replaced. Any failure
exits non-zero. All behaviour lives in cycles_houdini.blender.blend_file_node; the assets'
PythonModule sections only forward to it.
"""

import os
import sys
import traceback
import types
from pathlib import Path

import hou

root = Path(__file__).resolve().parent.parent
# Authoring needs only the pure parts of the package (no renderer import).
package = types.ModuleType("cycles_houdini")
package.__path__ = [str(root / "houdini/python3.13libs/cycles_houdini")]
sys.modules["cycles_houdini"] = package
for name in [name for name in sys.modules if name.startswith("cycles_houdini.")]:
    del sys.modules[name]
from cycles_houdini.blender import blend_file_node as fn  # noqa: E402

PY = hou.scriptLanguage.Python
CALLBACK = "hou.phm().on_parm(kwargs)"
IMPL = "__import__('cycles_houdini.blender.blend_file_node', fromlist=['blend_file_node'])"
STATUS_LABEL = '`pythonexprs("' + IMPL + '.status_text()")`'
FILES_LABEL = '`pythonexprs("' + IMPL + '.files_text()")`'
SOURCE_EXPRESSION = IMPL + ".source_index(hou.pwd().parent())"
UNPACK_EXPRESSION = IMPL + ".unpack_index(hou.pwd().parent())"
PYTHON_MODULE = (
    "# Cycles Blender asset: all logic is in cycles_houdini.blender.blend_file_node.\n"
    "from cycles_houdini.blender import blend_file_node as _impl\n\n\n"
    "def on_parm(kwargs):\n"
    "    return _impl.on_parm(kwargs)\n"
)
ON_CREATED = "from cycles_houdini.blender import blend_file_node\nblend_file_node.on_created(kwargs)\n"
ON_LOADED = "from cycles_houdini.blender import blend_file_node\nblend_file_node.on_loaded(kwargs)\n"

HELP = {
    "file": "The .blend file. Editing it re-points a moved .blend; converted files stay where they are.",
    "kind": "Whether Name is a Blender object or a collection.",
    "target": "The Blender object or collection. The menu lists the names read from the file (Refresh reads them).",
    "mode": "Link reads the .blend while rendering. Converted loads geometry converted by Update "
    "(files under $HIP/assets_blender, or stashed in the .hip). Switching never deletes anything.",
    "status": "Up to date, Source changed (the .blend was saved since the last Update), Source missing, "
    "Not converted (press Update), or Options changed (press Update).",
    "frame": "Blender timeline frame of the linked geometry.",
    "viewport_shape": "Viewport preview of linked geometry: bounding box or convex hull. Does not change rendering.",
    "viewport_wireframe": "Draw hull edges without shaded faces. Does not change rendering.",
    "shape_frame": "Blender frame used only for the viewport preview.",
    "max_hull_vertices": "Maximum vertices of each viewport hull. Does not affect rendering.",
    "refresh": "Read the names in the .blend again and refresh the viewport preview.",
    "load_as": "Packed keeps one packed primitive per Blender object (name and path attributes). "
    "Unpacked unpacks them when the node cooks; no Update is needed to switch.",
    "unpack_iterations": "How many levels of packed primitives are unpacked. Raise it to unpack nested instances.",
    "stash": "Store the converted geometry in the .hip instead of a file (one frame). The .hip becomes "
    "larger and is saved again with every scene version. Takes effect on Update.",
    "frames_mode": "Single converts the .blend's scene frame. Range converts start to end by step, one file "
    "per frame. Takes effect on Update.",
    "frame_range": "Start, end and step of the converted frames.",
    "apply_subdivision": "Bake Blender's Subdivision Surface into the geometry instead of subdividing at render "
    "time. Takes effect on Update.",
    "nested_instances": "Keep instances inside instances as nested packed primitives. Takes effect on Update.",
    "include_disabled": "Also include child collections that are excluded in the .blend's view layer.",
    "part": "Render-time subdivision is set per object, so an asset with subdivided and plain shapes is "
    "split into parts, one object each. This node loads this part.",
    "update": "Export the asset from Blender with these options and replace the converted files. "
    "Linked materials are reloaded; editable materials are left alone.",
    "open_blender": "Open the .blend in Blender.",
    "files": "Folder of the converted files.",
    "selection": "Collections and objects of this hierarchy, written kind:name, e.g. collection:Props "
    'object:"Big Tree".',
    "edit_selection": "Choose the collections and objects from the .blend.",
}


def _string_menu(name, label, items, default, **options):
    return hou.StringParmTemplate(
        name,
        label,
        1,
        default_value=(default,),
        menu_items=[token for token, _ in items],
        menu_labels=[text for _, text in items],
        menu_type=hou.menuType.Normal,
        script_callback=CALLBACK,
        script_callback_language=PY,
        help=HELP[name],
        **options,
    )


def _toggle(name, label, default=False, **options):
    return hou.ToggleParmTemplate(
        name,
        label,
        default_value=default,
        script_callback=CALLBACK,
        script_callback_language=PY,
        help=HELP[name],
        **options,
    )


def _button(name, label, **options):
    return hou.ButtonParmTemplate(
        name, label, script_callback=CALLBACK, script_callback_language=PY, help=HELP[name], **options
    )


def _label(name, label, text):
    template = hou.LabelParmTemplate(name, label, column_labels=(text,), help=HELP[name])
    return template


def _frames():
    mode = _string_menu(
        "frames_mode", "Frames", (("single", "Single"), ("range", "Range")), "single", join_with_next=True
    )
    frames = hou.IntParmTemplate(
        "frame_range",
        "Range",
        3,
        default_value=(1, 100, 1),
        script_callback=CALLBACK,
        script_callback_language=PY,
        help=HELP["frame_range"],
    )
    frames.hideLabel(True)
    return [mode, frames]


def _file_parm():
    return hou.StringParmTemplate(
        "file",
        "Blender File",
        1,
        string_type=hou.stringParmType.FileReference,
        file_type=hou.fileType.Any,
        tags={"filechooser_pattern": "*.blend", "filechooser_mode": "read"},
        script_callback=CALLBACK,
        script_callback_language=PY,
        help=HELP["file"],
    )


def file_templates(data_template):
    """Parameter templates of the Blender File SOP in FILE_PARMS order."""
    templates = {
        "file": _file_parm(),
        "kind": _string_menu(
            "kind",
            "Object / Collection",
            (("object", "Object"), ("collection", "Collection")),
            "object",
            join_with_next=True,
        ),
        "target": hou.StringParmTemplate(
            "target",
            "Name",
            1,
            menu_type=hou.menuType.StringReplace,
            item_generator_script=IMPL + ".target_menu(kwargs)",
            item_generator_script_language=PY,
            script_callback=CALLBACK,
            script_callback_language=PY,
            help=HELP["target"],
        ),
        "mode": _string_menu("mode", "Mode", (("link", "Link"), ("converted", "Converted")), "link"),
        "status": _label("status", "Status", STATUS_LABEL),
        "frame": hou.FloatParmTemplate(
            "frame",
            "Source Frame",
            1,
            default_value=(1.0,),
            min=1.0,
            max=100.0,
            script_callback=CALLBACK,
            script_callback_language=PY,
            help=HELP["frame"],
        ),
        "viewport_shape": _string_menu(
            "viewport_shape",
            "Viewport Shape",
            (("bounds", "Bounding Box"), ("hull", "Convex Hull")),
            "bounds",
            join_with_next=True,
        ),
        "viewport_wireframe": _toggle("viewport_wireframe", "Wireframe", True),
        "shape_frame": hou.FloatParmTemplate(
            "shape_frame",
            "Shape Frame",
            1,
            default_value=(1.0,),
            min=1.0,
            max=100.0,
            script_callback=CALLBACK,
            script_callback_language=PY,
            help=HELP["shape_frame"],
        ),
        "max_hull_vertices": hou.IntParmTemplate(
            "max_hull_vertices",
            "Maximum Hull Vertices",
            1,
            default_value=(64,),
            min=8,
            max=256,
            script_callback=CALLBACK,
            script_callback_language=PY,
            help=HELP["max_hull_vertices"],
        ),
        "refresh": _button("refresh", "Refresh"),
        "load_as": _string_menu(
            "load_as", "Load As", (("packed", "Packed"), ("unpacked", "Unpacked")), "packed", join_with_next=True
        ),
        "unpack_iterations": hou.IntParmTemplate(
            "unpack_iterations",
            "Iterations",
            1,
            default_value=(1,),
            min=1,
            max=10,
            min_is_strict=True,
            script_callback=CALLBACK,
            script_callback_language=PY,
            help=HELP["unpack_iterations"],
        ),
        "stash": _toggle("stash", "Stash in .hip"),
        "apply_subdivision": _toggle("apply_subdivision", "Apply Subdivision"),
        "nested_instances": _toggle("nested_instances", "Keep Nested Instances", True),
        "include_disabled": _toggle("include_disabled", "Include Disabled Collections"),
        "part": _string_menu("part", "Part", fn.PART_ITEMS, ""),
        "update": _button("update", "Update", join_with_next=True),
        "open_blender": _button("open_blender", "Open in Blender"),
        "files": _label("files", "Files", FILES_LABEL),
    }
    frames_mode, frame_range = _frames()
    templates["frames_mode"], templates["frame_range"] = frames_mode, frame_range
    data = data_template.clone()
    data.setName(fn.DATA_PARM)
    data.setLabel("Geometry Data")
    data.hide(True)
    templates[fn.DATA_PARM] = data
    for name, condition in fn.HIDE_WHEN.items():
        templates[name].setConditional(hou.parmCondType.HideWhen, condition)
    return [templates[name] for name in fn.FILE_PARMS]


def hierarchy_templates():
    templates = {
        "file": _file_parm(),
        "selection": hou.StringParmTemplate(
            "selection",
            "Selection",
            1,
            join_with_next=True,
            script_callback=CALLBACK,
            script_callback_language=PY,
            help=HELP["selection"],
        ),
        "edit_selection": _button("edit_selection", "Edit..."),
        "stash": _toggle("stash", "Stash in .hip"),
        "apply_subdivision": _toggle("apply_subdivision", "Apply Subdivision"),
        "nested_instances": _toggle("nested_instances", "Keep Nested Instances", True),
        "include_disabled": _toggle("include_disabled", "Include Disabled Collections"),
        "status": _label("status", "Status", STATUS_LABEL),
        "update": _button("update", "Update", join_with_next=True),
        "open_blender": _button("open_blender", "Open in Blender"),
    }
    frames_mode, frame_range = _frames()
    templates["frames_mode"], templates["frame_range"] = frames_mode, frame_range
    for name, condition in fn.HIERARCHY_HIDE_WHEN.items():
        templates[name].setConditional(hou.parmCondType.HideWhen, condition)
    return [templates[name] for name in fn.HIERARCHY_PARMS]


def help_card(title, summary, templates):
    lines = [f"= {title} =", "", "#type: node", "#namespace: cycles", "", f'"""{summary}"""', "", "@parameters", ""]
    for template in templates:
        if template.isHidden():
            continue
        lines += [f"{template.label()}:", f"    #id: {template.name()}", "", "    " + template.help(), ""]
    return "\n".join(lines)


def tool(context, net_type):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<shelfDocument>
  <tool name="$HDA_DEFAULT_TOOL" label="$HDA_LABEL" icon="$HDA_ICON">
    <toolMenuContext name="viewer">
      <contextNetType>{net_type}</contextNetType>
    </toolMenuContext>
    <toolMenuContext name="network">
      <contextOpType>$HDA_TABLE_AND_NAME</contextOpType>
    </toolMenuContext>
    <toolSubmenu>Cycles</toolSubmenu>
    <script scriptType="python"><![CDATA[import {context}toolutils

{context}toolutils.genericTool(kwargs, '$HDA_NAME')]]></script>
  </tool>
</shelfDocument>
"""


def _sections(definition, help_text, tool_text):
    definition.addSection("PythonModule", PYTHON_MODULE)
    definition.setExtraFileOption("PythonModule/IsPython", True)
    definition.addSection("OnCreated", ON_CREATED)
    definition.setExtraFileOption("OnCreated/IsPython", True)
    definition.addSection("OnLoaded", ON_LOADED)
    definition.setExtraFileOption("OnLoaded/IsPython", True)
    definition.addSection("Help", help_text)
    definition.addSection("Tools.shelf", tool_text)


def _fresh(path):
    """Replace an earlier output: uninstall it and remove the file."""
    try:
        hou.hda.uninstallFile(str(path))
    except Exception:
        pass
    if path.exists():
        path.unlink()


def build_file_asset(path):
    native = hou.sopNodeTypeCategory().nodeType(fn.NATIVE_REFERENCE_TYPE)
    if native is None:
        raise RuntimeError("cycles::blender_reference is not registered: run under the Cycles package environment")
    _fresh(path)
    container = hou.node("/obj").createNode("geo", "cycles_blender_file_authoring")
    subnet = container.createNode("subnet", "blender_file")
    reference = subnet.createNode(fn.NATIVE_REFERENCE_TYPE, fn.REFERENCE_NODE)
    reader = subnet.createNode("file", fn.FILE_NODE)
    reader.parm("file").set("")
    stash = subnet.createNode("stash", fn.STASH_NODE)
    source = subnet.createNode("switch", fn.SOURCE_SWITCH)
    for index, node in enumerate((reference, reader, stash)):
        source.setInput(index, node)
    unpack = subnet.createNode("unpack", fn.UNPACK_NODE)
    unpack.setInput(0, source)
    load = subnet.createNode("switch", fn.LOAD_SWITCH)
    load.setInput(0, source)
    load.setInput(1, unpack)
    output = subnet.createNode("output", "output0")
    output.setInput(0, load)
    output.setDisplayFlag(True)
    output.setRenderFlag(True)
    subnet.layoutChildren()
    data_template = stash.parm("stash").parmTemplate()
    asset = subnet.createDigitalAsset(
        name=fn.FILE_TYPE,
        hda_file_name=str(path),
        description="Cycles Blender File",
        min_num_inputs=0,
        max_num_inputs=0,
    )
    definition = asset.type().definition()
    templates = file_templates(data_template)
    group = hou.ParmTemplateGroup(templates)
    definition.setParmTemplateGroup(group)
    asset.setParmTemplateGroup(group) if not asset.isLockedHDA() else None
    # Inner wiring needs the asset's own parameters (blend-import.md: Asset nodes).
    reference = asset.node(fn.REFERENCE_NODE)
    reference.parm("multiple_references").set(1)
    reference.parm("references").set(1)
    reference.parm("references_initialized").set(1)
    reference.parm("ref_frame_initialized1").set(1)
    reference.parm("ref_enabled1").set(1)
    reference.parm("ref_frame1").setExpression('ch("../frame")', hou.exprLanguage.Hscript)
    reference.parm("viewport_shape").setExpression('chs("../viewport_shape")', hou.exprLanguage.Hscript)
    for name in ("viewport_wireframe", "shape_frame", "max_hull_vertices"):
        reference.parm(name).setExpression(f'ch("../{name}")', hou.exprLanguage.Hscript)
    reference.parm("ref_include_disabled_collections1").setExpression(
        'ch("../include_disabled")', hou.exprLanguage.Hscript
    )
    asset.node(fn.STASH_NODE).parm("stash").set(asset.parm(fn.DATA_PARM))
    unpack = asset.node(fn.UNPACK_NODE)
    unpack.parm("limit_iterations").set(1)
    unpack.parm("iterations").setExpression('ch("../unpack_iterations")', hou.exprLanguage.Hscript)
    unpack.parm("transfer_attributes").set("name path")
    asset.node(fn.SOURCE_SWITCH).parm("input").setExpression(SOURCE_EXPRESSION, hou.exprLanguage.Python)
    asset.node(fn.LOAD_SWITCH).parm("input").setExpression(UNPACK_EXPRESSION, hou.exprLanguage.Python)
    definition.addSection("EditableNodes", " ".join(fn.EDITABLE_NODES))
    _sections(
        definition,
        help_card(
            "Cycles Blender File",
            "One object or collection of a .blend file, linked or converted, with its own Update.",
            templates,
        ),
        tool("sop", "SOP"),
    )
    definition.updateFromNode(asset)
    definition.save(str(path), asset)
    container.destroy()
    return path


def build_hierarchy_asset(path):
    _fresh(path)
    subnet = hou.node("/obj").createNode("subnet", "cycles_blender_hierarchy_authoring")
    asset = subnet.createDigitalAsset(
        name=fn.HIERARCHY_TYPE,
        hda_file_name=str(path),
        description="Cycles Blender Hierarchy",
        min_num_inputs=0,
        max_num_inputs=1,
    )
    definition = asset.type().definition()
    options = definition.options()
    options.setUnlockNewInstances(True)  # editable contents, like Alembic Archive
    options.setLockContents(False)
    definition.setOptions(options)
    group = asset.parmTemplateGroup()
    templates = hierarchy_templates()
    folder = hou.FolderParmTemplate("blender", "Blender", parm_templates=templates, folder_type=hou.folderType.Tabs)
    entries = group.entries()
    if entries:
        group.insertBefore(entries[0], folder)
    else:
        group.append(folder)
    definition.setParmTemplateGroup(group)
    _sections(
        definition,
        help_card(
            "Cycles Blender Hierarchy",
            "Collections and objects of a .blend file as an object hierarchy, with its own Update. "
            "The node's transform moves the whole asset; dive in to edit the objects.",
            templates,
        ),
        tool("obj", "OBJ"),
    )
    definition.updateFromNode(asset)
    definition.save(str(path), asset)
    asset.destroy()
    return path


def verify(file_path, hierarchy_path):
    """Install both outputs and check what the asset nodes rely on (fails loudly)."""
    for path in (file_path, hierarchy_path):
        hou.hda.installFile(str(path))
    geo = hou.node("/obj").createNode("geo", "cycles_blender_assets_check")
    node = geo.createNode(fn.FILE_TYPE, "check")
    missing = [name for name in fn.FILE_PARMS if node.parm(name) is None and node.parmTuple(name) is None]
    if missing:
        raise RuntimeError("Blender File asset lacks parameters: " + ", ".join(missing))
    if not node.isLockedHDA():
        raise RuntimeError("Blender File asset instance is not locked")
    raw = node.node(fn.STASH_NODE).parm("stash").rawValue()
    if fn.DATA_PARM not in raw:
        raise RuntimeError("Stash SOP is not linked to " + fn.DATA_PARM + ": " + raw)
    for name in fn.EDITABLE_NODES:
        if not node.node(name).isEditableInsideLockedHDA():
            raise RuntimeError(name + " is not an Editable Node")
    node.node(fn.REFERENCE_NODE).parm("saved_catalogue").set("")  # an editable node: must not raise
    group = node.type().definition().parmTemplateGroup()
    for name, condition in fn.HIDE_WHEN.items():
        template = group.find(name)
        actual = template.conditionals().get(hou.parmCondType.HideWhen, "")
        if actual.strip() != condition:
            raise RuntimeError(f"{name}: hide-when {actual!r} != {condition!r}")
    node.cook(force=True)
    hierarchy = hou.node("/obj").createNode(fn.HIERARCHY_TYPE, "check_hierarchy")
    if hierarchy.isLockedHDA():
        raise RuntimeError("Blender Hierarchy instances must be unlocked")
    hierarchy.createNode("geo", "child").destroy()
    missing = [n for n in fn.HIERARCHY_PARMS if hierarchy.parm(n) is None and hierarchy.parmTuple(n) is None]
    if missing or hierarchy.parm("tx") is None:
        raise RuntimeError("Blender Hierarchy asset lacks parameters: " + ", ".join(missing or ["tx"]))
    hierarchy.destroy()
    geo.destroy()


def main(argv):
    if len(argv) != 2:
        raise SystemExit("usage: hython create-blender-assets.py OUTPUT_FOLDER")
    folder = Path(argv[1]).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    file_path = build_file_asset(folder / "cycles_blender_file.hda")
    hierarchy_path = build_hierarchy_asset(folder / "cycles_blender_hierarchy.hda")
    verify(file_path, hierarchy_path)
    hou.hipFile.clear(suppress_save_prompt=True)
    print("Created Cycles Blender assets: " + str(file_path) + ", " + str(hierarchy_path))


if __name__ == "__main__":
    try:
        main(sys.argv)
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc()
        sys.stdout.flush()
        os._exit(1)
