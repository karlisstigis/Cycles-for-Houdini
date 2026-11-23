# SPDX-License-Identifier: Apache-2.0
"""Gather the file paths of a scene for Collect, Relink and Move into Project.

The ``hou`` part is thin: it turns nodes into ``PathEntry`` records (plain data) that
the pure planners in ``collect``, ``relink`` and ``move_project`` work on, and writes
the planned parameter texts back in one undo group (``apply_rewrites``). Every HOM
call that may meet an unusual node is guarded.

Covered parameters:
- texture: ``filename`` of every Cycles Image / Environment Texture node (inside
  material, world, volume and light builders, recursively) and ``env_map`` of
  Environment Lights;
- geometry: ``file`` of File SOPs;
- blend: the .blend path (``file``) of ``cycles::blender_reference`` SOPs,
  ``cycles::blender_file`` SOPs and ``cycles::blender_hierarchy`` objects (looked up by name).
  The reference SOP inside a Blender File node is not listed: its path follows the node's
  ``file`` (``apply_rewrites`` pushes it). The File SOP inside a Blender File node
  (``converted_file``, an Editable Node) is a geometry entry like any File SOP.
"""

import json
import re
from dataclasses import dataclass

from . import texture_files

TEXTURE_TYPES = frozenset(("image_texture", "environment_texture"))
# materials.BUILDER, VOLUME_BUILDER, WORLD_BUILDER, LIGHT_BUILDER (not imported: keeps this module light).
BUILDER_TYPES = frozenset(
    ("cycles::material::1", "cycles::volume_material::1", "cycles::world_material::1", "cycles::light_material::1")
)
ENVLIGHT = "envlight"
LIBRARY_SOURCE_USER_DATA = "cycles_blender_library_source"
LIBRARY_MATERIAL_USER_DATA = "cycles_blender_library_material"
# blend_worlds.WORLD_USER_DATA: {"source", "name", "route"} on every Environment Light and
# World Material imported from a Blender world (linked or appended).
WORLD_USER_DATA = "cycles_blender_world"
REFERENCE_SOP = "cycles::blender_reference"
# Blender asset node types and the parameters holding their .blend (blend-import.md: Asset nodes).
# Types and parms are looked up defensively (builds without the node assets).
BLEND_NODE_PARMS = {
    REFERENCE_SOP: ("file",),
    "cycles::blender_file": ("file",),
    "cycles::blender_hierarchy": ("file",),
}
LINKED_FILE_NODE = "cycles::blender_file"


@dataclass(frozen=True)
class Owner:
    """The material, world or light a texture belongs to."""

    path: str
    name: str
    blender: bool = False  # imported from a .blend (link data or library user data)
    source: str = None  # the .blend; None when the import record is lost


@dataclass(frozen=True)
class PathEntry:
    """One file parameter: node path, parm name, unexpanded text and expanded path
    (pattern tokens kept)."""

    node: str
    parm: str
    raw: str
    path: str
    owner: Owner = None
    kind: str = "texture"  # "texture" | "geometry" | "blend"


def _hou():
    import hou

    return hou


def _guarded(call, *args, default=None):
    try:
        return call(*args)
    except Exception:
        return default


def _type_name(node):
    return _guarded(lambda: node.type().name(), default="") or ""


def is_texture_node(node):
    return bool(TEXTURE_TYPES.intersection(_type_name(node).split("::")))


def is_envlight(node):
    return _type_name(node).split("::")[0] == ENVLIGHT


def is_builder(node):
    return _type_name(node) in BUILDER_TYPES


def expand(raw):
    """Expanded parameter text with the pattern tokens kept."""
    hou = _hou()
    return texture_files.expand_keeping_tokens(raw, hou.text.expandString)


def hip_folder():
    return texture_files.posix(_hou().text.expandString("$HIP"))


def _world_source(node):
    """The .blend of an imported Blender world ("" when not recorded), or None for any other node."""
    raw = _guarded(node.userData, WORLD_USER_DATA)
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    source = info.get("source") if isinstance(info, dict) else None
    return source if isinstance(source, str) else ""


def _link_source(node):
    from ..blender.blend_library_materials import link_info

    info = _guarded(link_info, node)
    if info is not None:
        return True, info.get("source") or None
    source = _guarded(node.userData, LIBRARY_SOURCE_USER_DATA)
    if source:
        return True, source
    world = _world_source(node)
    if world is not None:
        return True, world or None
    if _guarded(node.userData, LIBRARY_MATERIAL_USER_DATA):
        return True, None  # imported from Blender, source lost
    return False, None


def owner_of(node):
    """Owner of a texture node or Environment Light: the nearest node up the parents that
    carries Blender import data, else the nearest material builder (or the node's parent)."""
    named = None
    current = node
    while current is not None:
        blender, source = _link_source(current)
        if blender:
            return Owner(current.path(), current.name(), True, source)
        if named is None and (is_builder(current) or is_envlight(current)):
            named = current
        current = _guarded(current.parent)
    named = named or _guarded(node.parent) or node
    return Owner(named.path(), named.name())


def _entry(node, parm_name, kind, owner=None):
    parm = _guarded(node.parm, parm_name)
    if parm is None:
        return None
    raw = _guarded(parm.unexpandedString, default="") or ""
    if not raw.strip() or raw.startswith("op:"):
        return None
    return PathEntry(node.path(), parm_name, raw, expand(raw), owner, kind)


def _texture_entry(node):
    if is_texture_node(node):
        return _entry(node, "filename", "texture", owner_of(node))
    if is_envlight(node):
        return _entry(node, "env_map", "texture", owner_of(node))
    return None


def texture_entries(nodes):
    """Texture entries of the given nodes and everything inside them, once per parameter."""
    seen, entries = set(), []
    for node in nodes:
        if node is None:
            continue
        for candidate in [node, *(_guarded(node.allSubChildren, default=()) or ())]:
            path = _guarded(candidate.path)
            if path in seen:
                continue
            seen.add(path)
            entry = _texture_entry(candidate)
            if entry is not None:
                entries.append(entry)
    return entries


def base_type(name):
    """Type name without its version: ``cycles::blender_file::1`` -> ``cycles::blender_file``."""
    return re.sub(r"::[0-9.]+$", "", name or "")


def _categories(kinds):
    hou = _hou()
    getters = [getattr(hou, kind + "NodeTypeCategory", None) for kind in kinds]
    return [_guarded(getter) for getter in getters if callable(getter)]


def instances(predicate, kinds=("vop", "obj", "sop")):
    """Every node whose type name satisfies ``predicate``, over the given type categories."""
    nodes = []
    for category in _categories(kinds):
        types = _guarded(category.nodeTypes, default={}) if category is not None else {}
        for name, node_type in sorted((types or {}).items()):
            if predicate(name):
                nodes.extend(_guarded(node_type.instances, default=()) or ())
    return nodes


def _texture_type(name):
    return bool(TEXTURE_TYPES.intersection(name.split("::"))) or name.split("::")[0] == ENVLIGHT


def all_texture_entries():
    """Texture entries of the whole scene."""
    return texture_entries(instances(_texture_type))


def geometry_entries():
    """``file`` of every File SOP."""
    entries = []
    for node in instances(lambda name: name == "file", kinds=("sop",)):
        entry = _entry(node, "file", "geometry")
        if entry is not None:
            entries.append(entry)
    return entries


def inside_blender_file(node):
    """True for the nodes inside a Cycles Blender File node (its inner reference, File SOP, ...)."""
    parent = _guarded(node.parent)
    return parent is not None and base_type(_type_name(parent)) == LINKED_FILE_NODE


def blend_entries():
    """The .blend path parameter of every Blender asset node (types that exist), without the
    reference SOP inside a Blender File node (it follows the node's own ``file``)."""
    entries = []
    for node in instances(lambda name: base_type(name) in BLEND_NODE_PARMS, kinds=("obj", "sop")):
        if base_type(_type_name(node)) == REFERENCE_SOP and inside_blender_file(node):
            continue
        for parm in BLEND_NODE_PARMS[base_type(_type_name(node))]:
            entry = _entry(node, parm, "blend")
            if entry is not None:
                entries.append(entry)
    return entries


def linked_asset_count():
    """Blender asset nodes that still read their .blend at render time: plain reference SOPs
    (old scenes) and Blender File nodes whose ``mode`` is ``link``. The reference SOP inside a
    Blender File node is that node's, not counted again."""
    count = 0
    for node in instances(lambda name: base_type(name) in (REFERENCE_SOP, LINKED_FILE_NODE), kinds=("sop",)):
        if base_type(_type_name(node)) == REFERENCE_SOP:
            count += not inside_blender_file(node)
            continue
        mode = _guarded(node.parm, "mode")
        value = _guarded(mode.evalAsString, default="") if mode is not None else ""
        count += str(value).lower() == "link"
    return count


def apply_rewrites(rewrites, label):
    """Set ``[(node path, parm name, text)]`` in one undo group. A locked (linked) parameter
    is unlocked for the write and locked again; links stay. Returns failures as
    ``[(node path, parm name, error text)]``."""
    hou = _hou()
    failed = []
    if not rewrites:
        return failed
    with hou.undos.group(label):
        for node_path, parm_name, text in rewrites:
            try:
                node = hou.node(node_path)
                parm = node.parm(parm_name) if node is not None else None
                if parm is None:
                    raise LookupError("parameter not found")
                locked = bool(parm.isLocked())
                if locked:
                    parm.lock(False)
                try:
                    parm.set(text)
                finally:
                    if locked:
                        parm.lock(True)
                _guarded(_after_rewrite, node, parm_name)  # the write itself succeeded
            except Exception as error:
                failed.append((node_path, parm_name, str(error)))
    return failed


def _after_rewrite(node, parm_name):
    """A script write runs no parameter callback: a Blender File node's new ``file`` is pushed to
    its inner reference node here (what the callback does when the artist edits it)."""
    if parm_name != "file" or base_type(_type_name(node)) != LINKED_FILE_NODE:
        return
    from ..blender import blend_file_node

    blend_file_node.push_reference(node)


def show_report(title, text, details="", error=False):
    """A message box with details in a UI session; printed otherwise."""
    hou = _hou()
    try:
        if hou.isUIAvailable():
            severity = hou.severityType.Warning if error else hou.severityType.Message
            hou.ui.displayMessage(text, severity=severity, title=title, details=details or None)
            return
    except Exception:
        pass
    print(f"Cycles: {title}: {text}" + (f"\n{details}" if details else ""))
