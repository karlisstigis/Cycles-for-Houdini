# SPDX-License-Identifier: Apache-2.0
"""Import a Blender World asset: an Environment Light, or a Cycles World Material.

Rules: docs/dev/blend-import.md, "Worlds". Blender exports the world (blend_world_export, through
blend_worker.export_library_worlds) and classifies it:

* ``simple`` (an equirectangular HDRI, optionally tinted, rotated about Z by one Mapping
  node) becomes a native Environment Light in /obj named after the world, created like
  viewer.menu_actions.create_environment. ``env_map`` is the HDRI where the .blend finds
  it (textures in place; a packed HDRI is extracted to ``<blend>/tex``; blend-import.md:
  Textures in place), intensity is Background Strength (exposure 0), colour is the tint, and the Mapping Z rotation goes
  into the light's object transform (light_rotation_for_blender_mapping).
* anything else becomes a Cycles World Material in /mat through the material translator
  (blend_materials.import_world_material), or is refused with the exporter's reason.

Link: one shared node per (source, world), parameters locked except Enable, grey, with
the comment "Linked from <file>: <world>" (blend_library_materials.mark_linked, link
record ``kind: "world"``). Convert & Append: an editable node; a second append is
``<world>_1``. Cycles has one world, so the import also decides which environment is
active (environment_state, activation_plan; Replace / Keep existing; blend-import.md: Worlds).

prepare_world runs on a background thread (no HOM); build_world on the host thread.
"""

import json
import math
import os
import re
import tempfile
from pathlib import Path

from . import blend_assets_layout as layout
from .blend_materials import BlendMaterialError

ENVIRONMENT_TYPE = "envlight"
ROP_TYPE = "cycles_ipr"
MODES = ("link", "append")
CHOICES = ("replace", "keep")
LINK_KIND = "world"
WORLD_USER_DATA = "cycles_blender_world"  # {"source", "name", "route"} on every imported world
ROUTES = {"simple": "light", "material": "material"}
MISSING_TEXTURE = "material.missing_texture"
DEFAULT_COLORSPACE = "auto"


class BlendWorldError(BlendMaterialError):
    """A verdict about the world itself (not importable as it is); never a transport failure."""


# ---------------------------------------------------------------- orientation (pure)


def light_rotation_for_blender_mapping(z_radians):
    """Environment Light rotation (rx, ry, rz) in degrees for a Blender Mapping Z rotation.

    The light shows exactly what Blender shows when the world's geometry is converted with
    the project's axis conversion. Derivation (column vectors, R_y(a) = right-handed
    rotation by `a` about +Y, which is what Houdini's ``ry`` builds):

    1. Blender world, Z up. An Environment Texture looks its image up with the view
       direction d_b in Blender world coordinates. Cycles' equirectangular lookup puts +X
       at the image centre (u = 0.5), +Y at u = 0.25 and +Z at the top row.
    2. Mapping (type Point) with rotation (0, 0, z) transforms the coordinates before the
       lookup: c = R_z(z) d_b. The coordinates turn by +z, so the image appears turned
       the opposite way (by -z) on the sky.
    3. The converter brings Blender geometry into Houdini with blend_convert._CONVERT:
       d_h = C d_b, C = (x, y, z) -> (x, z, -y) (Blender Z up becomes Houdini Y up). An
       HDRI must turn the same way so that it lines up with that geometry: d_b = C^T d_h.
    4. scene/world.py samples a native Environment Light (rotation matrix R, columns are
       its local axes in world space) at c = P R^T d_h, with P = rows (-Z, -X, +Y): local
       -Z is the image centre, local -X is u = 0.25 and local +Y is up.
    5. Equal lookups for every direction: P R^T = R_z(z) C^T, so R = C R_z(-z) P.
       C P = R_y(-90 deg) and C R_z(a) C^T = R_y(a) (Blender's Z axis is Houdini's Y), so
       R = R_y(-z) R_y(-90 deg) = R_y(-90 deg - z).

    Result: rx = rz = 0, ry = -90 - degrees(z), wrapped to [-180, 180). At z = 0 local -Z
    points at Houdini +X, which is Blender +X, Blender's image centre.
    """
    degrees = -90.0 - math.degrees(float(z_radians))
    degrees = (degrees + 180.0) % 360.0 - 180.0
    return (0.0, 0.0 if abs(degrees) < 1e-9 else degrees, 0.0)


# ---------------------------------------------------------------- light parameters (pure)


def light_colorspace(image, config, find_space):
    """(cycles_environment_colorspace, warning or None) for an exported image record.

    Data images bypass conversion; other images name the Houdini OCIO space with the same
    interop identity (find_space = blend_materials._find_space). Unresolvable: Auto, warned.
    """
    if image.get("is_data"):
        return "data", None
    try:
        return find_space(config, image), None
    except BlendMaterialError as error:
        return DEFAULT_COLORSPACE, f"{error}; the Environment Light uses Auto"


def light_parameters(simple, *, env_map, colorspace, to_working):
    """Environment Light parameters for a simple world record (``record["simple"]``).

    to_working: 3x3 matrix from Blender's working space to Houdini's (the tint is a
    Blender scene-linear colour; light_color is Houdini scene-linear).
    """
    tint = [float(v) for v in simple["tint"][:3]]
    color = tuple(sum(row[c] * tint[c] for c in range(3)) for row in to_working)
    return {
        "env_map": env_map,
        "light_intensity": float(simple["strength"]),
        "light_exposure": 0.0,
        "light_color": color,
        "cycles_environment_colorspace": colorspace,
        "light_contribprimary": 1,
        "t": (0.0, 0.0, 0.0),
        "r": light_rotation_for_blender_mapping(simple["rotation_z"]),
    }


# ---------------------------------------------------------------- existing environment (pure)


def environment_state(scene_facts):
    """Which environment lighting is active now (blend-import.md: Worlds).

    scene_facts: {"rops": [{"path", "world_source", "world_material", "lights": [paths of
    the enabled Environment Lights the ROP selects]}], "lights": [paths of every enabled
    Environment Light]}. A ROP has environment lighting with world_source "scene" and a
    selected light, or "material" with a World Material. Without any Cycles ROP, every
    enabled Environment Light counts (a new ROP selects all lights by default).

    Returns {"active": [paths], "kind": "none"|"light"|"material"|"mixed", "lights": [...],
    "rops": [...]}.
    """
    rops = list(scene_facts.get("rops") or ())
    lights = list(dict.fromkeys(scene_facts.get("lights") or ()))
    active, kinds = [], set()
    if rops:
        for rop in rops:
            if rop.get("world_source") == "scene":
                found, kind = list(rop.get("lights") or ()), "light"
            elif rop.get("world_source") == "material" and rop.get("world_material"):
                found, kind = [rop["world_material"]], "material"
            else:
                found, kind = [], None
            if found:
                kinds.add(kind)
                active.extend(path for path in found if path not in active)
    elif lights:
        active, kinds = list(lights), {"light"}
    kind = "none" if not kinds else kinds.pop() if len(kinds) == 1 else "mixed"
    return {"active": active, "kind": kind, "lights": lights, "rops": rops}


def activation_plan(new_kind, state, choice="replace", *, new_path=None):
    """What to change so the imported world is (or is not) the active one (blend-import.md: Worlds).

    new_kind: "light" (Environment Light) or "material" (World Material). choice:
    "replace": the new one becomes active. A light is enabled, every other enabled
    Environment Light is disabled (not deleted) and each ROP not in Scene mode is set to
    Scene. A World Material is set as world_material with world_source "material" on
    every Cycles ROP; lights are left alone (a ROP in Material mode ignores them).
    "keep": a new light is disabled and nothing else changes; a World Material is created
    and no ROP changes. When nothing else is active, "keep" acts as "replace".

    Returns {"enable": [paths], "disable": [paths], "rops": [{"path", "set": {parm: value}}],
    "report": [lines]}.
    """
    if new_kind not in ("light", "material"):
        raise ValueError("Unknown world kind: " + repr(new_kind))
    if choice not in CHOICES:
        raise ValueError("existing_environment must be 'replace' or 'keep', not " + repr(choice))
    label = new_path or "the imported world"
    others = [path for path in state.get("active", ()) if path != new_path]
    plan = {"enable": [], "disable": [], "rops": [], "report": []}
    if choice == "keep" and others:
        if new_kind == "light":
            plan["disable"].append(new_path)
            plan["report"].append(f"Kept the existing environment ({', '.join(others)}); {label} is disabled.")
        else:
            plan["report"].append(
                f"Kept the existing environment ({', '.join(others)}); {label} is not set on any Cycles ROP."
            )
        return plan
    rops = state.get("rops") or ()
    if new_kind == "light":
        plan["enable"].append(new_path)
        plan["disable"] = [path for path in state.get("lights", ()) if path != new_path]
        if plan["disable"]:
            plan["report"].append("Disabled environment lights (not deleted): " + ", ".join(plan["disable"]) + ".")
        for rop in rops:
            if rop.get("world_source") != "scene":
                plan["rops"].append({"path": rop["path"], "set": {"world_source": "scene"}})
                plan["report"].append(f"{rop['path']}: World Source set to Scene Environment Light.")
    else:
        for rop in rops:
            changes = {}
            if rop.get("world_source") != "material":
                changes["world_source"] = "material"
            if rop.get("world_material") != new_path:
                changes["world_material"] = new_path
            if changes:
                plan["rops"].append({"path": rop["path"], "set": changes})
                plan["report"].append(f"{rop['path']}: World Material set to {label}.")
        if not rops:
            plan["report"].append(
                f"No Cycles ROP in the scene: choose {label} under ROP > Scene > Environment > World Material."
            )
    return plan


# ---------------------------------------------------------------- names


def node_name(world_name):
    """Sanitised Houdini node name for a Blender world name."""
    text = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_]", "_", str(world_name))).strip("_")
    if not text:
        return "blender_world"
    return "w_" + text if text[0].isdigit() else text


def _texture_path(prepared, image):
    """env_map for the image: in place (absolute), or the extracted packed file in tex/."""
    if image.get("packed") is False:
        return str(image.get("filename") or "").replace("\\", "/")
    path = (Path(prepared["root"]) / str(image["filename"])).as_posix()
    text, asset_root = prepared.get("asset_root_text"), prepared.get("asset_root")
    if text and asset_root:
        try:
            relative = Path(path).relative_to(Path(asset_root))
        except ValueError:
            return path
        return text.rstrip("/\\").replace("\\", "/") + "/" + relative.as_posix()
    return path


# ---------------------------------------------------------------- prepare (background thread, no HOM)


def _not_simple(record):
    return "; ".join(reason["message"] for reason in record.get("reasons", ()))


def prepare_world(
    source, name, *, root, executable, timeout=300, cancelled=None, source_config=None, library_root=None
):
    """No HOM; safe on a background thread. Export world `name` of `source` (textures in place).

    `root` is the expanded asset root (blend_library_materials.resolve_asset_root); packed
    images go to ``<root>/<blend>/tex``. Returns {"record", "route" ("light" | "material"),
    "root" (the tex folder), "source", "name", "asset_root", "blend_folder"} for build_world.
    Raises BlendWorldError for a verdict about the world (missing, not importable) and
    blend_library_materials.LibraryMaterialUnavailable for everything else.
    """
    from . import blend_worker
    from .blend_library_materials import LibraryMaterialUnavailable, place_packed_textures

    source, root = Path(source).resolve(), Path(root)
    if cancelled is not None and cancelled.is_set():
        raise LibraryMaterialUnavailable("World import cancelled")
    if not executable:
        raise LibraryMaterialUnavailable("Blender not found; install Blender or set CYCLES_HOUDINI_BLENDER")
    with tempfile.TemporaryDirectory(prefix="cycles_blend_world_", ignore_cleanup_errors=True) as staging:
        try:
            exported = blend_worker.export_library_worlds(
                source,
                [name],
                staging,
                executable,
                timeout=timeout,
                cancelled=cancelled,
                textures="in_place",
                source_config=source_config,
            )
        except Exception as error:
            if cancelled is not None and cancelled.is_set():
                raise LibraryMaterialUnavailable("World import cancelled") from error
            raise LibraryMaterialUnavailable(
                f"Blender could not export world '{name}' from {source}: {error}"
            ) from error
        if cancelled is not None and cancelled.is_set():
            raise LibraryMaterialUnavailable("World import cancelled")
        if name in exported["errors"]:
            raise BlendWorldError(exported["errors"][name])
        records = [record for record in exported["records"] if record.get("name") == name]
        if len(records) != 1:
            raise BlendWorldError("Blender returned no world record for " + name)
        record = records[0]
        if record["kind"] == "unsupported":
            raise BlendWorldError(
                f"World '{name}' cannot be imported: {record['unsupported']}"
                + (f" (not a simple HDRI world: {_not_simple(record)})" if record.get("reasons") else "")
            )
        carrier = (
            {"nodes": [{"image": record["simple"]["image"]}]} if record["kind"] == "simple" else record["material"]
        )
        blend_dir = layout.blend_folder(root, source)
        try:
            tex = place_packed_textures(staging, carrier, blend_dir, source=source, library_root=library_root)
        except (OSError, ValueError, RuntimeError) as error:
            raise LibraryMaterialUnavailable(
                f"Could not place the packed textures of world {name} in {blend_dir}: {error}"
            ) from error
    return {
        "record": record,
        "route": ROUTES[record["kind"]],
        "root": str(tex),
        "source": str(source),
        "name": name,
        "asset_root": str(root),
        "blend_folder": str(blend_dir),
    }


# ---------------------------------------------------------------- Houdini layer


_TUPLES = ("light_color", "t", "r")


def _set(node, name, value):
    """Set a parameter (a tuple component by component), also on a linked (locked) node;
    every component keeps its lock state."""
    if name in _TUPLES:
        parms = node.parmTuple(name)
        pairs = list(zip(parms, value, strict=True)) if parms is not None else None
    else:
        parm = node.parm(name)
        pairs = [(parm, value)] if parm is not None else None
    if pairs is None:
        raise BlendWorldError(f"{node.path()} has no parameter {name}")
    for parm, component in pairs:
        locked = bool(parm.isLocked())
        if locked:
            parm.lock(False)
        try:
            parm.set(component)
        finally:
            if locked:
                parm.lock(True)


def _install(node):
    from ..scene import light_properties

    light_properties.install(node)


def _is_environment(node):
    return node.type().name().split("::")[0] == ENVIRONMENT_TYPE


def _environment_lights():
    import hou

    root = hou.node("/obj")
    return [node for node in (root.allSubChildren() if root is not None else ()) if _is_environment(node)]


def _enabled(node):
    parm = node.parm("light_enable")
    return bool(parm.eval()) if parm is not None else True


def _cycles_rops():
    import hou

    node_type = hou.nodeType(hou.ropNodeTypeCategory(), ROP_TYPE)
    return sorted(node_type.instances(), key=lambda node: node.path()) if node_type else []


def _selected_lights(rop):
    """Enabled Environment Lights the ROP's light patterns select (as scene/world.py reads them)."""
    import hou

    from ..scene import object_scene

    return [node for node in object_scene.selected(rop, hou.time(), lights=True) if _is_environment(node)]


def _material_path(rop):
    text = rop.parm("world_material").evalAsString()
    if not text:
        return ""
    node = rop.node(text[3:] if text.startswith("op:") else text)
    return node.path() if node is not None else text


def scene_facts():
    """HOM: the facts environment_state reads, from the scene's Cycles ROPs and lights."""
    rops = []
    for rop in _cycles_rops():
        try:
            lights = [node.path() for node in _selected_lights(rop)]
        except Exception:  # noqa: BLE001 - an invalid pattern selects nothing; the render reports it
            lights = []
        rops.append(
            {
                "path": rop.path(),
                "world_source": rop.parm("world_source").evalAsString(),
                "world_material": _material_path(rop),
                "lights": lights,
            }
        )
    return {"rops": rops, "lights": [node.path() for node in _environment_lights() if _enabled(node)]}


def apply_plan(plan):
    """HOM: apply an activation_plan (inside the caller's undo group); return its report lines."""
    import hou

    for path, value in [(p, 0) for p in plan["disable"]] + [(p, 1) for p in plan["enable"]]:
        node = hou.node(path) if path else None
        if node is not None:
            _set(node, "light_enable", value)
    for change in plan["rops"]:
        rop = hou.node(change["path"])
        for name, value in change["set"].items():
            _set(rop, name, value)
    return list(plan["report"])


def all_imported_worlds():
    """Environment Lights in /obj and World Materials in /mat imported from Blender worlds."""
    import hou

    found = []
    for parent in (hou.node("/obj"), hou.node("/mat")):
        for node in parent.children() if parent is not None else ():
            if world_info(node) is not None:
                found.append(node)
    return found


def world_info(node):
    """{"source", "name", "route"} of an imported world node, or None."""
    try:
        raw = node.userData(WORLD_USER_DATA)
        info = json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001 - foreign or damaged user data
        return None
    return info if isinstance(info, dict) and {"source", "name", "route"} <= set(info) else None


def _same_file(a, b):
    return bool(a and b) and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def find_linked_world(source, name):
    """The one shared linked node of (source .blend, world name), or None."""
    from .blend_library_materials import link_info

    for node in all_imported_worlds():
        info = link_info(node)
        if info and info.get("kind") == LINK_KIND and info["name"] == name and _same_file(info["source"], source):
            return node
    return None


def existing_environment(source=None, name=None, *, mode="link"):
    """HOM: paths of the active environment lighting to name in the Replace / Keep / Cancel
    question; empty when there is nothing to ask. A linked world that would be reused
    (mode "link", same source and name) does not count."""
    state = environment_state(scene_facts())
    reused = find_linked_world(source, name) if mode == "link" and source and name else None
    return [path for path in state["active"] if reused is None or path != reused.path()]


def _image_report(prepared, image, path, diagnostics):
    lines = []
    if image.get("missing"):
        warning = dict(
            code=MISSING_TEXTURE,
            severity="warning",
            source=prepared["name"],
            node="Environment Texture",
            path=path,
            message=f"{prepared['name']} / Environment Texture: texture not found: {path}",
        )
        if diagnostics is not None:
            diagnostics.append(warning)
        lines.append(warning["message"])
    elif image.get("packed") is False:
        lines.append(f"The HDRI stays at {path}; Collect Textures copies it into the project.")
    return lines


def _build_light(prepared, diagnostics):
    import hou

    from . import blend_materials
    from .blend_library_materials import free_child_name

    record, simple = prepared["record"], prepared["record"]["simple"]
    image = simple["image"]
    config, to_working, _ = blend_materials._color_matrices(record["color_management"])
    space, warning = light_colorspace(image, config, blend_materials._find_space)
    path = _texture_path(prepared, image)
    parent = hou.node("/obj")
    node = parent.createNode(ENVIRONMENT_TYPE, free_child_name(parent, node_name(prepared["name"])))
    try:
        _install(node)
        for name, value in light_parameters(simple, env_map=path, colorspace=space, to_working=to_working).items():
            _set(node, name, value)
        node.moveToGoodPosition()
    except Exception:
        node.destroy()
        raise
    lines = [f"Imported world {prepared['name']} as the Environment Light {node.path()}."]
    if warning:
        if diagnostics is not None:
            diagnostics.append(
                dict(code="world.colorspace", severity="warning", source=prepared["name"], message=warning)
            )
        lines.append(warning)
    return node, lines + _image_report(prepared, image, path, diagnostics)


def _build_material(prepared, diagnostics):
    import hou

    from . import blend_materials
    from .blend_library_materials import (
        _variable_texture_paths,
        forward_slash_texture_paths,
        free_child_name,
        release_prototype_marker,
    )

    record = prepared["record"]
    parent = hou.node("/mat")
    path = blend_materials.import_world_material(
        record["material"], prepared["root"], parent=parent, diagnostics=diagnostics
    )
    node = hou.node(path)
    try:
        node.setName(free_child_name(parent, node_name(prepared["name"]), node), unique_name=True)
        release_prototype_marker(node)
        forward_slash_texture_paths(node)
        text, asset_root = prepared.get("asset_root_text"), prepared.get("asset_root")
        if text and asset_root:
            _variable_texture_paths(node, asset_root, text.strip(), hou.text.expandString)
    except Exception:
        node.destroy()
        raise
    reasons = _not_simple(record)
    lines = [
        f"Imported world {prepared['name']} as the Cycles World Material {node.path()}"
        + (f" (not a simple HDRI world: {reasons})." if reasons else ".")
    ]
    hidden = [name for name, shown in (record.get("visibility") or {}).items() if not shown]
    if hidden:
        lines.append(
            f"World ray visibility ({', '.join(hidden)} off in Blender) is not imported: "
            "set it on the Cycles ROP under Scene > Environment."
        )
    return node, lines


def _build(prepared, diagnostics):
    builder = _build_light if prepared["route"] == "light" else _build_material
    node, lines = builder(prepared, diagnostics)
    lines += list(prepared["record"].get("warnings") or ())
    node.setUserData(
        WORLD_USER_DATA,
        json.dumps(
            {
                "source": str(prepared["source"]).replace("\\", "/"),
                "name": prepared["name"],
                "route": prepared["route"],
            },
            ensure_ascii=False,
        ),
    )
    return node, lines


def _source_signature(source):
    try:
        from ..assets.importers import source_signature

        return source_signature(source)
    except Exception:  # noqa: BLE001 - the stale check then reports nothing
        return None


def _mark_linked(node, prepared, link_info=None):
    from .blend_library_materials import mark_linked

    info = {
        "kind": LINK_KIND,
        "route": prepared["route"],
        "source": prepared["source"],
        "name": prepared["name"],
        "signature": _source_signature(prepared["source"]),
        "asset_root": str(prepared.get("asset_root") or "").replace("\\", "/"),
    }
    info.update(link_info or {})
    mark_linked(node, info)
    if prepared["route"] == "light":
        # Enable stays editable: Replace / Keep and the artist switch environments with it.
        parm = node.parm("light_enable")
        if parm is not None and parm.isLocked():
            parm.lock(False)
    return info


def build_world(prepared, *, mode="link", existing_environment="replace", diagnostics=None, link_info=None):
    """Host thread (HOM): build a prepared world and decide the active environment (blend-import.md: Worlds).

    mode "link": reuse the linked node of (source, world) or build and link it; "append":
    a new editable node (``<world>_1`` when the name is taken). existing_environment:
    "replace" or "keep" (activation_plan); the caller asks the question when
    existing_environment() is not empty. link_info: extra keys for the link record (the
    browser's library, library_root, key). Everything happens in one undo group (nested in
    the caller's when it has one). Returns {"node", "reused", "route", "report": [lines]}.
    """
    import hou

    if mode not in MODES:
        raise ValueError("World import mode must be 'link' or 'append', not " + repr(mode))
    if existing_environment not in CHOICES:
        raise ValueError("existing_environment must be 'replace' or 'keep', not " + repr(existing_environment))
    name, route = prepared["name"], prepared["route"]
    with hou.undos.group("Import Blender world " + name):
        state = environment_state(scene_facts())
        node = find_linked_world(prepared["source"], name) if mode == "link" else None
        reused = node is not None
        if reused:
            report = [f"Reused the linked world {node.path()}."]
        else:
            node, report = _build(prepared, diagnostics)
            if mode == "link":
                _mark_linked(node, prepared, link_info)
        kind = (world_info(node) or {}).get("route", route)
        plan = activation_plan(kind, state, existing_environment, new_path=node.path())
        report += apply_plan(plan)
    try:
        node.setCurrent(True, clear_all_selected=True)
    except Exception:  # noqa: BLE001 - selection is cosmetic
        pass
    return {"node": node, "reused": reused, "route": kind, "report": report}


# ---------------------------------------------------------------- reload a linked world


def reload_request(node):
    """HOM: what prepare_world needs to re-export a linked world:
    {"path", "source", "name", "root"} (root = the expanded asset root)."""
    from .blend_library_materials import asset_root_text, link_info, resolve_asset_root

    info = link_info(node)
    if info is None or info.get("kind") != LINK_KIND:
        raise BlendWorldError("Not a linked Blender world: " + node.path())
    root = info.get("asset_root") or str(resolve_asset_root(asset_root_text()[0], info["source"]))
    return {"path": node.path(), "source": info["source"], "name": info["name"], "root": root}


def rebuild_linked_world(node, prepared, diagnostics=None):
    """HOM: refresh a linked world from a new prepare_world result; one undo group.

    An Environment Light is updated in place (its name, Enable state and everything that
    refers to it are kept); a World Material is rebuilt beside the old node and swapped in
    under the same name, so ROP references stay valid. A world that changed route
    (simple <-> graph) is refused: import it again.
    """
    import hou

    from .blend_library_materials import link_info, mark_linked, unmark_linked

    info = link_info(node)
    if info is None or info.get("kind") != LINK_KIND:
        raise BlendWorldError("Not a linked Blender world: " + node.path())
    if prepared["route"] != info.get("route"):
        raise BlendWorldError(
            f"{node.path()}: world {info['name']} is now imported as a "
            f"{'Environment Light' if prepared['route'] == 'light' else 'World Material'}; import it again"
        )
    with hou.undos.group("Reload linked world " + node.name()):
        info = dict(info, signature=_source_signature(info["source"]))
        info.pop("locked_before", None)
        if prepared["route"] == "light":
            enabled = _enabled(node)
            unmark_linked(node)
            record = prepared["record"]
            from . import blend_materials

            config, to_working, _ = blend_materials._color_matrices(record["color_management"])
            image = record["simple"]["image"]
            space, _ = light_colorspace(image, config, blend_materials._find_space)
            path = _texture_path(prepared, image)
            for name, value in light_parameters(
                record["simple"], env_map=path, colorspace=space, to_working=to_working
            ).items():
                _set(node, name, value)
            _set(node, "light_enable", int(enabled))
            report = _image_report(prepared, image, path, diagnostics)
            mark_linked(node, info)
            parm = node.parm("light_enable")
            if parm is not None and parm.isLocked():
                parm.lock(False)
            return node, report
        name, position = node.name(), node.position()
        new, report = _build(prepared, diagnostics)
        node.destroy()
        new.setName(name)
        new.setPosition(position)
        mark_linked(new, info)
        return new, report


def reload_linked_world(node, *, prepared=None, executable=None, timeout=300, diagnostics=None):
    """Synchronous Reload from Library for a linked world (blocks while Blender exports
    unless `prepared` is given). Returns (node, report lines)."""
    if prepared is None:
        from ..core import blender_bridge

        import hou

        request = reload_request(node)
        getenv = getattr(hou, "getenv", None)
        prepared = prepare_world(
            request["source"],
            request["name"],
            root=request["root"],
            executable=str(blender_bridge.require_executable(executable)),
            timeout=timeout,
            source_config=getenv("CYCLES_HOUDINI_BLENDER_OCIO") if callable(getenv) else None,
        )
        # A packed HDRI keeps the $HIP form of its extracted file while the scene's asset
        # root still names the folder recorded at link time (as importers' reload does).
        text = reload_root_text(request["root"], hou.text.expandString)
        if text:
            prepared["asset_root_text"] = text
    return rebuild_linked_world(node, prepared, diagnostics)


def reload_root_text(root, expand):
    """The scene's unexpanded asset root text (``$HIP/assets_blender``) when it expands to
    ``root``, the asset root a linked world recorded; else None (absolute paths)."""
    from .blend_library_materials import asset_root_text

    text = asset_root_text()[0]
    try:
        same = os.path.normcase(os.path.abspath(expand(text))) == os.path.normcase(os.path.abspath(root))
    except Exception:  # noqa: BLE001 - an unexpandable root keeps absolute paths
        return None
    return text if same and text.strip() != expand(text) else None
