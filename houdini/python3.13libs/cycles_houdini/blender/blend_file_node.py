# SPDX-License-Identifier: Apache-2.0
"""Python behind the Cycles Blender File SOP and the Cycles Blender Hierarchy object.

Contract (parameter tokens, inner nodes, user data, the rules of the two assets):
docs/dev/blend-import.md, "Asset nodes".

Two layers:

- Pure functions (no ``hou``): ``derived_paths``, ``status``, ``options_fingerprint``,
  ``visible_parms`` and the hide-when table ``HIDE_WHEN`` that the asset generator writes,
  selection text, target resolution and the source signature. Unit-tested directly.
- The ``hou`` layer: parameter callbacks, label texts, the inner switch expressions, the
  Update / Update All entry points, Open in Blender and ``hide_native_reference``. The
  assets' PythonModule sections only forward to these functions.

Cooking never starts Blender: the inner switches read the node's parameters and user data
only. Status reads one file signature when the parameter pane draws the Status label (a
backtick expression in the label) and when the network editor's Cycles menu opens.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

FILE_TYPE = "cycles::blender_file"
HIERARCHY_TYPE = "cycles::blender_hierarchy"
NATIVE_REFERENCE_TYPE = "cycles::blender_reference"
MODULE = "cycles_houdini.blender.blend_file_node"

# Inner nodes of the Blender File asset (scripts/create-blender-assets.py builds them).
REFERENCE_NODE = "reference"  # cycles::blender_reference, an Editable Node (blend-import.md: Asset nodes)
FILE_NODE = "converted_file"  # File SOP, an Editable Node: Update writes its path
STASH_NODE = "stash"  # Stash SOP; its `stash` parm is linked to DATA_PARM (blend-import.md: Asset nodes)
SOURCE_SWITCH = "source"  # 0 reference, 1 file, 2 stash
UNPACK_NODE = "unpack"
LOAD_SWITCH = "load_as_switch"  # 0 packed, 1 unpacked
EDITABLE_NODES = (REFERENCE_NODE, FILE_NODE)
DATA_PARM = "geometry_data"  # the only hidden parameter (blend-import.md: Asset nodes)

# Node user data (blend-import.md: Asset nodes): the state of the last Update, nothing else of the import.
USER_DATA = "cycles_blender_update"
OBJECT_USER_DATA = "cycles_blender_object"  # objects inside a hierarchy: their Blender object
COLLECTION_USER_DATA = "cycles_blender_collection"  # the collection null inside a hierarchy
SOURCE_USER_DATA = "cycles_blender_source"  # File / Stash SOP the converter created
MATERIAL_USER_DATA = "cycles_blender_material"  # Material SOP the converter created

UP_TO_DATE = "Up to date"
SOURCE_CHANGED = "Source changed"
SOURCE_MISSING = "Source missing"
NOT_CONVERTED = "Not converted. Press Update."
OPTIONS_CHANGED = "Options changed. Press Update."
STATUSES = (UP_TO_DATE, SOURCE_CHANGED, SOURCE_MISSING, NOT_CONVERTED, OPTIONS_CHANGED)

# Subdivision warning (blend-import.md: Asset nodes): Cycles subdivides per object, so a subdivided part
# must be its object's only geometry and the object's Cycles subdivision must be the part's. Shown after the Status text and in the
# Update report; checked when the label draws or Update runs, never in a cook.
SUBDIVISION_FIX = "Give it its own object, or turn on Apply Subdivision and press Update."
SUBDIVISION_MERGED = (
    "Warning: this subdivided part is merged with other geometry in {object}, and Cycles subdivides "
    "the whole object. " + SUBDIVISION_FIX
)
SUBDIVISION_MISMATCH = (
    "Warning: {object} does not subdivide this part as Blender does (Cycles tab, Subdivision). " + SUBDIVISION_FIX
)
SUBDIVISION_PARMS = (
    ("type", "cycles_subdivision_type"),
    ("render_level", "cycles_subdivision_render_level"),
)

MODES = ("link", "converted")
LOAD_AS = ("packed", "unpacked")
FRAMES_MODES = ("single", "range")
KINDS = ("object", "collection")
MATERIAL_MODES = ("link", "append", "none")
# Part menu tokens; "" (no menu) when the asset has one part. Suffixes as blend_convert._part_suffixes.
PART_ITEMS = (
    ("base", "Main"),
    ("subdiv", "Subdivided"),
    ("subdiv_2", "Subdivided 2"),
    ("subdiv_3", "Subdivided 3"),
    ("subdiv_4", "Subdivided 4"),
)

# Parameters of the Blender File node, in interface order.
FILE_PARMS = (
    "file",
    "kind",
    "target",
    "mode",
    "status",
    "frame",
    "viewport_shape",
    "viewport_wireframe",
    "shape_frame",
    "max_hull_vertices",
    "refresh",
    "load_as",
    "unpack_iterations",
    "stash",
    "frames_mode",
    "frame_range",
    "apply_subdivision",
    "nested_instances",
    "include_disabled",
    "part",
    "update",
    "open_blender",
    "files",
    DATA_PARM,
)
LINK_PARMS = ("frame", "viewport_shape", "viewport_wireframe", "shape_frame", "max_hull_vertices", "refresh")
# Parameters of the Blender Hierarchy object (its standard object parameters come after them).
HIERARCHY_PARMS = (
    "file",
    "selection",
    "edit_selection",
    "frames_mode",
    "frame_range",
    "stash",
    "apply_subdivision",
    "nested_instances",
    "include_disabled",
    "status",
    "update",
    "open_blender",
)
# Conversion options: a change sets "Options changed. Press Update." (blend-import.md: Asset nodes).
OPTION_KEYS = (
    "kind",
    "target",
    "selection",
    "stash",
    "frames",
    "apply_subdivision",
    "nested_instances",
    "include_disabled",
)

# Hide-when conditions written into the assets (Houdini syntax: inside one {} all hold, any
# {} group hides). tests/pytest_cases/test_blend_file_node.py checks them against
# visible_parms for every state the parameters can express. "Source missing" cannot be
# expressed with parameter values: the conditions use an empty Blender File instead, and
# Update / Open in Blender report a missing file when pressed.
HIDE_WHEN = {
    "frame": "{ mode != link }",
    "viewport_shape": "{ mode != link }",
    "viewport_wireframe": "{ mode != link }",
    "shape_frame": "{ mode != link }",
    "max_hull_vertices": "{ mode != link }",
    "refresh": "{ mode != link }",
    "include_disabled": "{ kind != collection }",
    "load_as": "{ mode != converted }",
    "unpack_iterations": "{ mode != converted } { load_as != unpacked }",
    "stash": "{ mode != converted }",
    "frames_mode": "{ mode != converted } { stash == 1 }",
    "frame_range": "{ mode != converted } { stash == 1 } { frames_mode != range }",
    "apply_subdivision": "{ mode != converted }",
    "nested_instances": "{ mode != converted }",
    "part": '{ mode != converted } { part == "" }',
    "update": '{ mode != converted } { file == "" }',
    "open_blender": '{ mode != converted } { file == "" }',
    "files": "{ mode != converted } { stash == 1 }",
}
HIERARCHY_HIDE_WHEN = {
    "frames_mode": "{ stash == 1 }",
    "frame_range": "{ stash == 1 } { frames_mode != range }",
    "update": '{ file == "" }',
    "open_blender": '{ file == "" }',
}


class NodeError(ValueError):
    """A Blender File / Hierarchy node cannot be read or updated (message for the artist)."""


# ---------------------------------------------------------------- pure: parts, frames, options


def part_suffix(token):
    """Part menu token -> file name suffix ("" / "base" -> "", "subdiv" -> "_subdiv", ...)."""
    token = (token or "").strip()
    return "" if token in ("", "base") else "_" + token


def part_token(suffix, count):
    """File name suffix -> part menu token; "" when the asset has a single part."""
    if count <= 1:
        return ""
    return "base" if not suffix else suffix.lstrip("_")


def frames_value(frames_mode, frame_range, stash=False):
    """None (the .blend's scene frame) or (start, end, step) from the Frames parameters."""
    if stash or frames_mode != "range":
        return None
    start, end, step = (int(v) for v in frame_range)
    return (start, end, step)


def options_fingerprint(parms):
    """Short digest of the conversion options in ``parms`` (keys of OPTION_KEYS; others ignored).

    The source path is not part of it: re-pointing a moved .blend is not an option change;
    the file signature decides whether the source changed.
    """
    values = {}
    for key in OPTION_KEYS:
        value = parms.get(key)
        if key == "frames":
            value = list(value) if value else None
        elif key in ("stash", "apply_subdivision", "nested_instances", "include_disabled"):
            value = bool(value)
        values[key] = value
    text = json.dumps(values, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def status(mode, has_converted, stored_signature, current_signature, source_exists, options_dirty):
    """One of STATUSES. Precedence: missing source, not converted, source changed, options changed.

    Link mode reads the .blend live, so it is up to date unless the file is missing.
    "Source changed" wins over "Options changed" so that Update All Blender Files, which
    updates only "Source changed" nodes, still finds a changed source.
    """
    if not source_exists:
        return SOURCE_MISSING
    if mode == "link":
        return UP_TO_DATE
    if not has_converted:
        return NOT_CONVERTED
    if stored_signature is not None and current_signature is not None and stored_signature != current_signature:
        return SOURCE_CHANGED
    if options_dirty:
        return OPTIONS_CHANGED
    return UP_TO_DATE


def visible_parms(state):
    """Parameter tokens of the Blender File node that do something in ``state``.

    state keys: mode, load_as, stash, frames_mode, kind, parts (count), source_missing.
    """
    mode = state.get("mode", "link")
    visible = {"file", "kind", "target", "mode", "status"}
    if state.get("kind") == "collection":
        visible.add("include_disabled")
    if mode == "link":
        visible.update(LINK_PARMS)
        return visible
    visible.update(("load_as", "stash", "apply_subdivision", "nested_instances"))
    if state.get("load_as") == "unpacked":
        visible.add("unpack_iterations")
    if not state.get("stash"):
        visible.update(("frames_mode", "files"))
        if state.get("frames_mode") == "range":
            visible.add("frame_range")
    if (state.get("parts") or 1) > 1:
        visible.add("part")
    if not state.get("source_missing"):
        visible.update(("update", "open_blender"))
    return visible


def hierarchy_visible_parms(state):
    """Parameter tokens of the Blender Hierarchy object that do something in ``state``."""
    visible = {
        "file",
        "selection",
        "edit_selection",
        "stash",
        "apply_subdivision",
        "nested_instances",
        "include_disabled",
        "status",
    }
    if not state.get("stash"):
        visible.add("frames_mode")
        if state.get("frames_mode") == "range":
            visible.add("frame_range")
    if not state.get("source_missing"):
        visible.update(("update", "open_blender"))
    return visible


def _condition_groups(text):
    groups = []
    for chunk in text.split("}"):
        chunk = chunk.strip().lstrip("{").strip()
        if not chunk:
            continue
        words = chunk.split()
        terms = []
        for index in range(0, len(words), 3):
            name, operator, value = words[index : index + 3]
            terms.append((name, operator, value.strip('"')))
        groups.append(terms)
    return groups


def condition_holds(text, values):
    """Evaluate a hide-when condition of HIDE_WHEN against {token: value} (tests, generator check)."""

    def same(name, value):
        current = values.get(name, "")
        if isinstance(current, bool):
            current = int(current)
        return str(current) == value

    for terms in _condition_groups(text):
        if all(same(name, value) == (operator == "==") for name, operator, value in terms):
            return True
    return False


def subdivision_matches(setting, values):
    """True when an object's Cycles subdivision parameters (``values``: parm name -> value) subdivide
    a part with the converter's ``setting`` (blend_convert.subdivision_settings; {} when unknown)."""
    override = values.get("cycles_subdivision_override")
    if not override or str(override) in ("0", "off", "False"):
        return False
    for key, name in SUBDIVISION_PARMS:
        if key in (setting or {}) and values.get(name) is not None and str(values[name]) != str(setting[key]):
            return False
    return True


def upstream_of(start, target):
    """(reached, merged): whether ``target`` feeds ``start`` through node inputs, and whether a node
    on the way (a Merge, a Boolean, ...) has more than one connected input, joining other geometry."""
    seen, pending, reached, merged = set(), [start], False, False
    while pending:
        current = pending.pop()
        if current is None:
            continue
        try:
            key = current.path()
        except Exception:
            key = id(current)
        if key in seen:
            continue
        seen.add(key)
        if current is target or (hasattr(target, "path") and key == target.path()):
            reached = True
            continue
        try:
            inputs = [node for node in current.inputs() if node is not None]
        except Exception:
            inputs = []
        if len(inputs) > 1:
            merged = True
        pending.extend(inputs)
    return reached, reached and merged


# ---------------------------------------------------------------- pure: selection and targets


def _quote(name):
    if not name or any(c.isspace() for c in name) or any(c in name for c in "\"'\\:"):
        return json.dumps(name, ensure_ascii=False)
    return name


def format_selection(selection):
    """[(kind, name)] -> "collection:Props object:\"Big Tree\"" (the Selection parameter)."""
    return " ".join(f"{kind}:{_quote(name)}" for kind, name in selection)


def parse_selection(text):
    """Inverse of format_selection. Raises NodeError for text it cannot read."""
    result, position, text = [], 0, (text or "").strip()
    decoder = json.JSONDecoder()
    while position < len(text):
        if text[position].isspace():
            position += 1
            continue
        colon = text.find(":", position)
        if colon < 0:
            raise NodeError("Selection items are written kind:name, e.g. collection:Props")
        kind = text[position:colon].strip().lower()
        if kind not in KINDS:
            raise NodeError(f"Unknown selection kind {kind!r}: use collection: or object:")
        position = colon + 1
        if position < len(text) and text[position] == '"':
            try:
                name, position = decoder.raw_decode(text, position)
            except ValueError:
                raise NodeError("Unterminated quoted name in Selection") from None
        else:
            end = position
            while end < len(text) and not text[end].isspace():
                end += 1
            name, position = text[position:end], end
        if not isinstance(name, str) or not name:
            raise NodeError("Missing name after " + kind + ":")
        if (kind, name) not in result:
            result.append((kind, name))
    return result


def target_token(name):
    """The `target` parameter text for one Blender name: the name itself when the reference
    grammar reads it unchanged, else the quoted exact name (works before any catalogue read)."""
    from .blend_targets import menu_entries

    token = menu_entries([name])[0]
    return name if token == name else json.dumps(name, ensure_ascii=False)


def resolve_target(text, names=(), bindings=None):
    """The one Blender name the `target` parameter selects. Raises NodeError otherwise."""
    from .blend_targets import _tokens, resolve_targets

    try:
        tokens = list(_tokens(text or ""))
    except ValueError as error:
        raise NodeError(f"Name: {error}") from None
    if len(tokens) != 1 or tokens[0][2]:
        raise NodeError("Name must select exactly one Blender object or collection")
    token, exact, _ = tokens[0]
    if exact:
        return token
    selected, warnings = resolve_targets(text, list(names), bindings=bindings or {})
    if len(selected) != 1:
        reason = "; ".join(warnings) or "Press Refresh to read the names in the .blend"
        raise NodeError(f"Name {text!r} does not select one Blender name: {reason}")
    return selected[0]


# ---------------------------------------------------------------- pure: files


def source_signature(path):
    """Signature of the .blend for Status: file_signature.native_signature on Windows; elsewhere
    (or when it is unavailable) modification time and size. None for a missing file."""
    if not path:
        return None
    try:
        from .file_signature import native_signature

        signature = native_signature(str(path))
    except OSError:
        signature = None
    if signature:
        return signature
    try:
        info = os.stat(path)
    except OSError:
        return None
    return f"{info.st_mtime_ns}:{info.st_size}"


def derived_paths(asset_root, source, kind, target, part="", frames=None, *, root_text=None, names=None):
    """Where the converted files of one Blender File node live (blend-import.md: Folder layout, Asset nodes). No HOM.

    ``asset_root``: the absolute assets_blender folder; ``target``: the Blender name;
    ``frames``: None or (start, end, step); ``names``: an ImportStore (its recorded names win)
    or None (the plain naming rule). ``root_text`` ("$HIP/assets_blender"): ``text`` uses it
    when the files are under that root. Returns dict(blend_folder, asset_folder, files
    [absolute], pattern (relative file name, ``$F4`` for a range), text (File SOP path)).
    """
    from . import blend_assets_layout as layout
    from .blend_convert import _variable_path, asset_original, frame_list

    blend_dir = layout.blend_folder(asset_root, source)
    original = asset_original(kind, target)
    if names is not None:
        asset = names.asset_name(original, target)
    else:
        asset = layout.safe_name(target, "asset")
    stem = asset + part_suffix(part)
    values = frame_list(frames)
    if len(values) > 1:
        files = [layout.geometry_file(stem, frame) for frame in values]
        pattern = layout.geometry_pattern(stem, True)
    else:
        files = [layout.geometry_file(stem)]
        pattern = files[0]
    folder = layout.asset_folder(blend_dir, asset)
    absolute = folder / pattern
    text = _variable_path(root_text, asset_root, absolute) if root_text else absolute.as_posix()
    return dict(
        blend_folder=Path(blend_dir),
        asset_folder=folder,
        files=[folder / name for name in files],
        pattern=pattern,
        text=text,
    )


def part_path(text, old_part, new_part):
    """File SOP path text of another part of the same asset ("" when it cannot be derived)."""
    old, new = part_suffix(old_part), part_suffix(new_part)
    folder, _, name = (text or "").rpartition("/")
    for ending in (".$F4.bgeo.sc", ".bgeo.sc"):
        if name.endswith(ending):
            stem = name[: -len(ending)]
            if old and not stem.endswith(old):
                return ""
            stem = stem[: len(stem) - len(old)] if old else stem
            return (folder + "/" if folder else "") + stem + new + ending
    return ""


# ---------------------------------------------------------------- hou helpers


def _hou():
    import hou

    return hou


def _node(node):
    if isinstance(node, str):
        found = _hou().node(node)
        if found is None:
            raise NodeError("No node " + node)
        return found
    return node


def _type_name(node):
    try:
        return node.type().name()
    except Exception:
        return ""


def is_file_node(node):
    return _type_name(node).split("::")[:2] == ["cycles", "blender_file"]


def is_hierarchy(node):
    return _type_name(node).split("::")[:2] == ["cycles", "blender_hierarchy"]


def node_types(hou=None):
    """(Blender File SOP type, Blender Hierarchy object type), each None when not installed."""
    hou = hou or _hou()
    lookup = getattr(hou, "nodeType", None)
    if not callable(lookup):
        return None, None
    try:
        file_type = lookup(hou.sopNodeTypeCategory(), FILE_TYPE)
    except Exception:
        file_type = None
    try:
        hierarchy_type = lookup(hou.objNodeTypeCategory(), HIERARCHY_TYPE)
    except Exception:
        hierarchy_type = None
    return file_type, hierarchy_type


def _eval(node, name, default=None):
    try:
        parm = node.parm(name)
        if parm is None:
            return default
        value = parm.eval()
        return default if value is None else value
    except Exception:
        return default


def _eval_tuple(node, name, default):
    try:
        tuple_parm = node.parmTuple(name)
        if tuple_parm is not None:
            return tuple(tuple_parm.eval())
    except Exception:
        pass
    values = [_eval(node, f"{name}{index}") for index in (1, 2, 3)]
    return default if any(value is None for value in values) else tuple(values)


def _set(node, name, value):
    parm = node.parm(name)
    if parm is None:
        raise NodeError(f"{node.path()} has no parameter {name}")
    parm.set(value)


def _set_tuple(node, name, values):
    try:
        tuple_parm = node.parmTuple(name)
    except Exception:
        tuple_parm = None
    if tuple_parm is not None:
        tuple_parm.set(tuple(values))
        return
    for index, value in enumerate(values, start=1):
        _set(node, f"{name}{index}", value)


def record(node):
    """The node's last-Update record (USER_DATA), {} when it never was updated."""
    try:
        text = node.userData(USER_DATA)
        value = json.loads(text) if text else {}
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def write_record(node, **values):
    data = record(node)
    data.update(values)
    try:
        node.setUserData(USER_DATA, json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        pass
    return data


def inner(node, name):
    try:
        return node.node(name)
    except Exception:
        return None


# ---------------------------------------------------------------- reading a node's parameters


def _source_text(node):
    try:
        return str(node.parm("file").eval() or "").replace("\\", "/")
    except Exception:
        return ""


def target_name(node):
    """The Blender name of a Blender File node's `target` (bindings and catalogue of the inner node)."""
    from . import blend_catalogue

    text = _eval(node, "target", "") or ""
    reference = inner(node, REFERENCE_NODE)
    names, bindings = [], {}
    if reference is not None:
        try:
            names = blend_catalogue.cached_menu(reference, "ref_target1")
            key = blend_catalogue._binding_key(_source_text(node), _eval(node, "kind", "object"))
            bindings = blend_catalogue._bindings(reference).get(key, {})
        except Exception:
            names, bindings = [], {}
    return resolve_target(text, names, bindings)


def node_options(node):
    """The import options a Blender File node or Blender Hierarchy object records in its parameters.

    Returns dict(node_kind, source, selection, kind, target, mode, stash, frames, include_disabled,
    apply_subdivision, nested_instances, part, materials).
    """
    node = _node(node)
    stash = bool(_eval(node, "stash", 0))
    frames = frames_value(_eval(node, "frames_mode", "single"), _eval_tuple(node, "frame_range", (1, 1, 1)), stash)
    values = dict(
        source=_source_text(node),
        stash=stash,
        frames=frames,
        include_disabled=bool(_eval(node, "include_disabled", 0)),
        apply_subdivision=bool(_eval(node, "apply_subdivision", 0)),
        nested_instances=bool(_eval(node, "nested_instances", 1)),
        materials=record(node).get("materials") or "link",
    )
    if is_hierarchy(node):
        selection = parse_selection(_eval(node, "selection", ""))
        values.update(node_kind="hierarchy", selection=selection, mode="converted", kind=None, target=None, part="")
    else:
        kind = _eval(node, "kind", "object")
        values.update(
            node_kind="file",
            kind=kind,
            target=_eval(node, "target", "") or "",
            mode=_eval(node, "mode", "link"),
            part=_eval(node, "part", "") or "",
            selection=None,
        )
        if kind != "collection":
            values["include_disabled"] = False
    if values["materials"] not in MATERIAL_MODES:
        values["materials"] = "link"
    return values


def fingerprint_of(node):
    """options_fingerprint of the node's current parameters."""
    options = node_options(node)
    parms = dict(options)
    if options["node_kind"] == "hierarchy":
        parms["selection"] = format_selection(options["selection"])
    return options_fingerprint(parms)


def _converted_present(node, data):
    storage = data.get("storage")
    if storage == "stash":
        return True
    if storage != "files":
        return False
    if is_hierarchy(node):
        return True
    reader = inner(node, FILE_NODE)
    if reader is None:
        return False
    try:
        text = reader.parm("file").unexpandedString()
    except Exception:
        return False
    if not text:
        return False
    try:
        expanded = _hou().text.expandString(text.replace("$F4", "0001")) if "$" in text else text
    except Exception:
        expanded = text
    path = Path(expanded)
    return path.parent.is_dir() if "$F" in text else path.is_file()


def node_status(node):
    """Status text of a Blender File node or Blender Hierarchy object (no Blender, one stat)."""
    try:
        node = _node(node)
        data = record(node)
        source = _source_text(node)
        exists = bool(source) and os.path.isfile(source)
        mode = "converted" if is_hierarchy(node) else _eval(node, "mode", "link")
        if mode == "converted" and exists:
            current = source_signature(source)
            try:
                dirty = bool(data.get("options")) and data.get("options") != fingerprint_of(node)
            except NodeError:
                dirty = True
        else:
            current, dirty = None, False
        return status(mode, _converted_present(node, data), data.get("signature"), current, exists, dirty)
    except Exception as error:  # a label must never raise
        return "Status unavailable: " + str(error)


def _pwd(node):
    if node is not None:
        return _node(node)
    return _hou().pwd()


def _eval_text(node, name):
    try:
        parm = node.parm(name)
        if parm is None:
            return None
        try:
            return parm.evalAsString()
        except Exception:
            return parm.eval()
    except Exception:
        return None


def subdivision_problem(node):
    """The subdivision warning for a Blender File node that outputs a subdivided part, or "".

    Merged: the part reaches its object's render node through a node with several inputs.
    Mismatch: the object's Cycles subdivision is off or differs from the part's (the setting
    recorded at the last Update; only "on" is checked for nodes updated by earlier builds). Not
    detected: geometry joined in a way that leaves no multi-input node (e.g. an Object Merge SOP
    inside the network of the node's input chain is still found; a second displayed object is not
    the same object and is fine).
    """
    try:
        node = _node(node)
        if not is_file_node(node) or _eval(node, "mode", "link") != "converted":
            return ""
        data = record(node)
        setting = data.get("subdivision")
        if setting is None:
            if not str(data.get("part") or _eval(node, "part", "") or "").startswith("subdiv"):
                return ""
            setting = {}
        container = node.parent()
        render = None
        for getter in ("renderNode", "displayNode"):
            try:
                render = getattr(container, getter)()
            except Exception:
                render = None
            if render is not None:
                break
        if render is None:
            return ""
        reached, merged = upstream_of(render, node)
        if not reached:
            return ""
        if merged:
            return SUBDIVISION_MERGED.format(object=container.path())
        values = {"cycles_subdivision_override": _eval(container, "cycles_subdivision_override")}
        for _, name in SUBDIVISION_PARMS:
            values[name] = _eval_text(container, name)
        if not subdivision_matches(setting, values):
            return SUBDIVISION_MISMATCH.format(object=container.path())
        return ""
    except Exception:
        return ""


def status_text(node=None):
    """The Status label's backtick expression (evaluated when the parameter pane draws): the
    status, then the subdivision warning when there is one."""
    try:
        node = _pwd(node)
        text = node_status(node)
        warning = subdivision_problem(node) if is_file_node(node) else ""
        return text.rstrip(".") + ". " + warning if warning else text
    except Exception as error:
        return "Status unavailable: " + str(error)


def files_text(node=None):
    """The Files label: the folder of the converted files (as written in the File SOP)."""
    try:
        reader = inner(_pwd(node), FILE_NODE)
        text = reader.parm("file").unexpandedString() if reader is not None else ""
    except Exception:
        text = ""
    return text.rpartition("/")[0] if text else "Not converted"


# ---------------------------------------------------------------- inner switch expressions (cook time)


def source_index(node=None):
    """Inner source switch: 0 the Link result, 1 the converted file, 2 the stash.

    Converted mode with no converted data keeps showing the Link result (blend-import.md: Asset nodes). Reads
    parameters and user data only: never Blender, never a file.
    """
    try:
        node = _pwd(node)
        if _eval(node, "mode", "link") != "converted":
            return 0
        storage = record(node).get("storage")
        return {"files": 1, "stash": 2}.get(storage, 0)
    except Exception:
        return 0


def unpack_index(node=None):
    """Inner Load As switch: 1 (through the Unpack SOP) for converted data loaded Unpacked."""
    try:
        node = _pwd(node)
        if source_index(node) == 0:
            return 0
        return 1 if _eval(node, "load_as", "packed") == "unpacked" else 0
    except Exception:
        return 0


# ---------------------------------------------------------------- the inner reference node (Link)


def push_reference(node):
    """Write file, kind and target into the inner reference node's first row (editable; blend-import.md: Asset nodes).

    Frame, viewport and Include Disabled reach it by channel reference (generator)."""
    reference = inner(node, REFERENCE_NODE)
    if reference is None:
        return None
    if not reference.evalParm("multiple_references"):
        reference.parm("multiple_references").set(1)
        reference.parm("references").set(1)
        reference.parm("references_initialized").set(1)
    marker = reference.parm("ref_frame_initialized1")
    if marker is not None:
        marker.set(1)  # the frame is driven by the outer node, never reset by initialize_rows
    reference.parm("file").set(_source_text(node))
    reference.parm("ref_enabled1").set(1)
    reference.parm("ref_target_kind1").set(_eval(node, "kind", "object"))
    reference.parm("ref_target1").set(_eval(node, "target", "") or "")
    return reference


def target_menu(kwargs):
    """Menu script of `target`: the inner reference node's cached names (no Blender, no I/O)."""
    from . import blend_catalogue

    reference = inner(kwargs["node"], REFERENCE_NODE)
    if reference is None:
        return []
    try:
        return blend_catalogue.cached_menu_entries(reference, "ref_target1")
    except Exception:
        return []


def part_menu(kwargs=None):
    return [value for item in PART_ITEMS for value in item]


# ---------------------------------------------------------------- creation and Update writes


def configure_file_node(
    node,
    *,
    source,
    kind,
    name,
    mode="link",
    stash=False,
    frames=None,
    include_disabled=False,
    apply_subdivision=False,
    nested_instances=True,
    part="",
    viewport_shape=None,
):
    """Set a new Blender File node's parameters (creation API, used by blend_convert.build_import)."""
    _set(node, "file", str(source).replace("\\", "/"))
    _set(node, "kind", kind)
    _set(node, "target", target_token(name))
    _set(node, "mode", mode)
    if viewport_shape in ("bounds", "hull"):
        _set(node, "viewport_shape", viewport_shape)
    _set(node, "stash", int(bool(stash)))
    if frames:
        _set(node, "frames_mode", "range")
        _set_tuple(node, "frame_range", [int(v) for v in frames])
    else:
        _set(node, "frames_mode", "single")
    _set(node, "include_disabled", int(bool(include_disabled and kind == "collection")))
    _set(node, "apply_subdivision", int(bool(apply_subdivision)))
    _set(node, "nested_instances", int(bool(nested_instances)))
    _set(node, "part", part or "")
    reference = push_reference(node)
    if reference is not None:
        from . import blend_catalogue

        try:
            blend_catalogue.initialize_rows(reference)
            blend_catalogue.bind_targets(reference)
        except Exception:
            pass
    return node


def configure_hierarchy(
    node,
    *,
    source,
    selection,
    stash=False,
    frames=None,
    include_disabled=False,
    apply_subdivision=False,
    nested_instances=True,
):
    _set(node, "file", str(source).replace("\\", "/"))
    _set(node, "selection", format_selection(selection))
    _set(node, "stash", int(bool(stash)))
    if frames:
        _set(node, "frames_mode", "range")
        _set_tuple(node, "frame_range", [int(v) for v in frames])
    else:
        _set(node, "frames_mode", "single")
    _set(node, "include_disabled", int(bool(include_disabled)))
    _set(node, "apply_subdivision", int(bool(apply_subdivision)))
    _set(node, "nested_instances", int(bool(nested_instances)))
    return node


def set_converted(node, *, path_text=None, geometry=None, part=None):
    """Point a Blender File node at its converted data: a file (path text) or stashed geometry.

    Files: the inner File SOP's path is written (an Editable Node) and reloaded; any stash is
    cleared (blend-import.md: Asset nodes). Stash: the geometry data parameter is set; files stay on disk.
    """
    hou = _hou()
    if part is not None:
        _set(node, "part", part)
    if geometry is not None:
        _set(node, DATA_PARM, geometry)
        return "stash"
    reader = inner(node, FILE_NODE)
    if reader is None:
        raise NodeError(node.path() + " has no inner File SOP " + FILE_NODE)
    reader.parm("file").set(path_text)
    try:
        reader.parm("reload").pressButton()
    except Exception:
        pass
    empty = getattr(hou, "Geometry", None)
    parm = node.parm(DATA_PARM)
    if parm is not None and callable(empty):
        try:
            parm.set(empty())
        except Exception:
            pass
    return "files"


def record_update(node, *, signature, storage, materials, options=None, transforms=None, part=None, subdivision=False):
    """Store the state of this Update in the node's user data (blend-import.md: Asset nodes).

    Keys: signature (of the .blend when it was read), time, storage ("files" | "stash" | None
    for Link), materials (the mode new materials are imported in), options (fingerprint),
    part (Blender File node), subdivision (Blender File node: the part's render-time
    subdivision setting, None for none; left as it was when not given), transforms (Blender
    Hierarchy object: local transform written per Blender object).
    """
    values = dict(
        signature=signature,
        time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        storage=storage,
        materials=materials,
        options=options if options is not None else fingerprint_of(node),
    )
    if transforms is not None:
        values["transforms"] = transforms
    if part is not None:
        values["part"] = part
    if subdivision is not False:
        values["subdivision"] = subdivision
    data = write_record(node, **values)
    if is_file_node(node):
        refresh_output(node)
    return data


def refresh_output(node):
    """Make a Blender File node show the data its record names, without a parameter change.

    The inner source and Load As switches pick their input with Python expressions that read
    the record (user data). Measured in Houdini 22.0.466 (tests/houdini_blend_file_node_smoke.py):
    while the source switch shows the Link input, writing user data, setting or reloading the
    inner File SOP, or setting the same empty geometry data leaves the switch and the node
    clean, so after Link -> Converted an Update wrote the files and the record and the node
    kept showing the Link result until one of its parameters changed. Setting Mode away and
    back is that change (Parm.set runs no callback; both switch expressions read Mode). The
    inner File SOP is reloaded as well, after the session committed the files (set_converted
    pressed Reload before that). Only this node's file is reloaded; Houdini's
    geometry cache of other files is kept. Returns the reloaded File SOP or None.
    """
    node = _node(node)
    parm = node.parm("mode")
    if parm is not None and _eval(node, "mode", "link") == "converted":
        try:
            parm.set("link")
            parm.set("converted")
        except Exception:
            pass
    if record(node).get("storage") != "files":
        return None
    reader = inner(node, FILE_NODE)
    if reader is None:
        return None
    try:
        reader.parm("reload").pressButton()
    except Exception:
        return None
    return reader


# ---------------------------------------------------------------- callbacks


def _callback_parm(kwargs):
    name = kwargs.get("parm_name")
    if not name and kwargs.get("parm") is not None:
        name = kwargs["parm"].name()
    return name or ""


def on_parm(kwargs):
    """Callback of every Blender File / Blender Hierarchy parameter (the asset's PythonModule)."""
    node, name = kwargs["node"], _callback_parm(kwargs)
    try:
        if is_hierarchy(node):
            return _on_hierarchy_parm(node, name)
        return _on_file_parm(node, name, kwargs)
    except Exception as error:  # a callback reports instead of raising into the parameter pane
        _tell(str(error), error=True)
    return None


def _on_file_parm(node, name, kwargs):
    from . import blend_catalogue

    reference = inner(node, REFERENCE_NODE)
    if name in ("file", "kind", "target"):
        reference = push_reference(node)
        if reference is not None:
            (blend_catalogue.parameters_changed if name == "file" else blend_catalogue.selection_changed)(reference)
    elif name in ("frame", "include_disabled") and reference is not None:
        blend_catalogue.selection_changed(reference)
    elif name in ("viewport_shape", "shape_frame", "max_hull_vertices") and reference is not None:
        blend_catalogue.viewport_changed(reference)
    elif name == "viewport_wireframe" and reference is not None:
        reference.cook(force=True)
    elif name == "refresh" and reference is not None:
        push_reference(node)
        blend_catalogue.refresh_node(reference)
    elif name == "mode" and _eval(node, "mode") == "link":
        push_reference(node)  # Converted -> Link never deletes anything (blend-import.md: Asset nodes)
    elif name == "part":
        _retarget_part(node, kwargs)
    elif name == "update":
        start_update([node])
    elif name == "open_blender":
        open_blender(node)
    return None


def _on_hierarchy_parm(node, name):
    if name == "update":
        start_update([node])
    elif name == "open_blender":
        open_blender(node)
    elif name == "edit_selection":
        edit_selection(node)
    return None


def _retarget_part(node, kwargs):
    """Part changed: load that part's file of the same asset (files storage)."""
    reader = inner(node, FILE_NODE)
    if reader is None:
        return
    previous = record(node).get("part", "")
    current = _eval(node, "part", "") or ""
    text = part_path(reader.parm("file").unexpandedString(), previous, current)
    if text:
        reader.parm("file").set(text)
        write_record(node, part=current)


def on_created(kwargs):
    node = kwargs["node"]
    if is_file_node(node):
        try:
            push_reference(node)
        except Exception:
            pass


def on_loaded(kwargs):
    """Nothing is read or started at load: Status is computed when the parameter pane shows it."""
    return None


def _tell(text, error=False):
    try:
        hou = _hou()
        if hou.isUIAvailable():
            severity = hou.severityType.Error if error else hou.severityType.Message
            hou.ui.displayMessage(text, severity=severity, title="Cycles Blender File")
            return
    except Exception:
        pass
    print(("Error: " if error else "") + text, file=sys.stderr if error else sys.stdout)


# ---------------------------------------------------------------- Update


def _report_lines(lines):
    errors = [text for text, error in lines if error]
    notes = [text for text, error in lines if not error]
    return "\n".join(errors + notes)


def _ask_shared(nodes):
    """Replace / Cancel when an Update would change files another scene uses (blend-import.md: Import record)."""
    from . import blend_convert

    shared = []
    for node in nodes:
        try:
            options = node_options(node)
            if options["stash"]:
                continue
            if options["node_kind"] == "hierarchy":
                selection = options["selection"]
            else:
                selection = [(options["kind"], target_name(node))]
            for entry in blend_convert.shared_scenes(options["source"], selection):
                shared.append(
                    f'"{entry["asset"]}" is also used by: ' + ", ".join(Path(s).name for s in entry["scenes"])
                )
        except Exception:
            continue
    if not shared:
        return "replace"
    hou = _hou()
    if not hou.isUIAvailable():
        return "replace"
    text = "\n".join(dict.fromkeys(shared)) + "\nReplacing it changes that scene too."
    choice = hou.ui.displayMessage(
        text, buttons=("Replace", "Cancel"), default_choice=0, close_choice=1, title="Update Blender File"
    )
    return "replace" if choice == 0 else "cancel"


def start_update(nodes, *, report=None):
    """Update button / menu: export in the background (UI) or synchronously (hython).

    Returns the update result in hython; None in the UI (the result is reported when done).
    """
    from . import blend_convert

    hou = _hou()
    nodes = [_node(node) for node in nodes]
    for node in nodes:
        source = _source_text(node)
        if not source or not os.path.isfile(source):
            raise NodeError(f"{node.path()}: {SOURCE_MISSING}: {source or '(no Blender File)'}")
    on_shared = _ask_shared(nodes)
    if on_shared == "cancel":
        return None
    lines = []

    def say(text, error=False):
        lines.append((text, error))
        if report is not None:
            report(text, error)

    snapshot = blend_convert.update_snapshot(nodes, on_shared=on_shared, report=say)
    if not hou.isUIAvailable():
        payload = blend_convert.prepare_update(snapshot)
        return blend_convert.sync_import(payload, None, report=say)
    import threading

    result = {}

    def work():
        try:
            result["payload"] = blend_convert.prepare_update(snapshot)
        except BaseException as error:  # reported on the main thread
            result["error"] = error

    def finish():
        try:
            if "error" in result:
                raise result["error"]
            blend_convert.sync_import(result["payload"], None, report=say)
        except Exception as error:
            lines.append((f"Update failed: {error}", True))
        try:
            hou.ui.setStatusMessage("Blender update finished", severity=hou.severityType.Message)
        except Exception:
            pass
        text = _report_lines(lines)
        if text:
            _tell(text, error=any(error for _, error in lines))

    def poll():
        if thread.is_alive():
            return
        hou.ui.removeEventLoopCallback(poll)
        finish()

    try:
        hou.ui.setStatusMessage("Updating from Blender...", severity=hou.severityType.Message)
    except Exception:
        pass
    thread = threading.Thread(target=work, name="cycles-blender-update", daemon=True)
    thread.start()
    hou.ui.addEventLoopCallback(poll)
    return None


def all_nodes(hou=None):
    """Every Blender File node and Blender Hierarchy object in the scene."""
    hou = hou or _hou()
    found = []
    for node_type in node_types(hou):
        if node_type is None:
            continue
        try:
            found.extend(node_type.instances())
        except Exception:
            continue
    return found


def changed_nodes(hou=None):
    return [node for node in all_nodes(hou) if node_status(node) == SOURCE_CHANGED]


def can_update_all(kwargs=None):
    """Network-menu context of Update All Blender Files: the bridge is on and a source changed."""
    try:
        from ..core import blender_bridge

        if not blender_bridge.enabled():
            return False
        return bool(changed_nodes())
    except Exception:
        return False


def update_all(kwargs=None, *, report=None):
    """Update every Blender File node and Blender Hierarchy object whose status is "Source changed"."""
    nodes = changed_nodes()
    if not nodes:
        _tell("No Blender File node has a changed source.")
        return None
    try:
        return start_update(nodes, report=report)
    except NodeError as error:
        _tell(str(error), error=True)
        return None


# ---------------------------------------------------------------- Open in Blender, selection, Tab menu


def blender_executable():
    from ..core import blender_bridge

    executable = blender_bridge._discover(
        blender_bridge._configured_executable(),
        os.environ.get("PATH", ""),
        os.environ.get("ProgramFiles", "C:/Program Files"),
    )
    if not executable or not os.path.isfile(executable):
        raise NodeError("Blender not found. Install Blender or set CYCLES_HOUDINI_BLENDER to its executable.")
    return executable


def _blender_ocio():
    """CYCLES_HOUDINI_BLENDER_OCIO (Houdini's variable when set there), or None."""
    try:
        value = _hou().getenv("CYCLES_HOUDINI_BLENDER_OCIO")
    except Exception:
        value = None
    return value or os.environ.get("CYCLES_HOUDINI_BLENDER_OCIO") or None


def open_blender(node, *, popen=None):
    """Start Blender on the node's .blend, detached; never waits for it.

    Blender gets its own environment (blend_worker.blender_environment): without Houdini's
    OCIO and Python variables, so a file saved there keeps Blender's colour space names."""
    from .blend_worker import blender_environment

    node = _node(node)
    source = _source_text(node)
    if not source or not os.path.isfile(source):
        raise NodeError(f"{SOURCE_MISSING}: {source or '(no Blender File)'}")
    command = [blender_executable(), source]
    options = dict(
        env=blender_environment(_blender_ocio()),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0x8) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200
        )
    else:
        options["start_new_session"] = True
    return (popen or subprocess.Popen)(command, **options)


def edit_selection(node, *, editor=None):
    """Edit... next to Selection: the importer's selection tree for this .blend
    (blend_import_dialog.edit_selection(source, selection, include_disabled=) -> [(kind, name)]
    or None for Cancel). A text prompt only when that dialog cannot be loaded (no Qt).
    Returns the new selection, or None when nothing changed."""
    hou = _hou()
    node = _node(node)
    current = parse_selection(_eval(node, "selection", ""))
    if editor is None:
        try:
            from . import blend_import_dialog

            editor = blend_import_dialog.edit_selection
        except Exception:
            editor = None
    if editor is not None:
        chosen = editor(_source_text(node), current, include_disabled=bool(_eval(node, "include_disabled", 0)))
    else:
        if not hou.isUIAvailable():
            return None
        button, text = hou.ui.readInput(
            'Collections and objects, e.g. collection:Props object:"Big Tree"',
            buttons=("OK", "Cancel"),
            initial_contents=format_selection(current),
            title="Blender Hierarchy Selection",
        )
        if button != 0:
            return None
        chosen = parse_selection(text)
    if not chosen or list(chosen) == current:
        return None
    _set(node, "selection", format_selection(chosen))
    return list(chosen)


def hide_native_reference():
    """Hide cycles::blender_reference from the Tab menu when the Blender File asset is installed (blend-import.md:
    Asset nodes).

    The native node stays installed for old scenes and for use inside the asset. Returns True
    when it was hidden. Call once at UI startup (cycles_houdini_startup.py).
    """
    try:
        hou = _hou()
        file_type, _ = node_types(hou)
        native = hou.sopNodeTypeCategory().nodeType(NATIVE_REFERENCE_TYPE)
        if file_type is None or native is None:
            return False
        native.setHidden(True)
        return True
    except Exception:
        return False
