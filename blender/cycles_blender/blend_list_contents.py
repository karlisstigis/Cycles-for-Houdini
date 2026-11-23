# SPDX-License-Identifier: GPL-3.0-or-later
# Blender component of Cycles for Houdini (see blender/README.md). This file is one of the scripts
# that run inside Blender and use its Python API, so it is published under the GNU General Public
# License, version 3 or later (blender/LICENSE). The Houdini integration (Apache-2.0) never imports
# it; the two sides exchange only files, command-line arguments and pipe messages.
"""Blender-side scene listing for File > Import > Blender (.blend).

Runs inside a disposable Blender worker. The main file is opened without UI or
scripts and only read; nothing is saved. The result is plain JSON data (see
docs/dev/blend-import.md, "Worker listing"): collections with their tree and
view-layer exclusion, objects, scenes, frame range and frame rate.
"""

from pathlib import Path

MAX_COLLECTIONS = 20000
MAX_OBJECTS = 200000
MAX_SLOTS = 256


def _library(ident):
    library = getattr(ident, "library", None)
    return str(library.filepath) if library is not None else None


def _layer_states(view_layer):
    """{collection pointer: excluded} for every collection under view_layer.

    Blender propagates exclusion to all children of an excluded LayerCollection,
    so a collection counts as excluded when none of its occurrences in the
    layer tree is reached through included layers only.
    """
    states = {}
    if view_layer is None:
        return states
    pending = [(child, False) for child in view_layer.layer_collection.children]
    while pending:
        layer, parent_excluded = pending.pop()
        excluded = parent_excluded or bool(layer.exclude)
        key = layer.collection.as_pointer()
        states[key] = states.get(key, True) and excluded
        pending.extend((child, excluded) for child in layer.children)
    return states


def _has_geometry_nodes(obj):
    for modifier in getattr(obj, "modifiers", ()):
        if modifier.type == "NODES":
            return True
    return False


def _material_slots(obj):
    names = []
    for slot in list(getattr(obj, "material_slots", ()))[:MAX_SLOTS]:
        material = slot.material
        names.append(str(material.name) if material is not None else "")
    return names


def list_contents(bpy, scene=None):
    """Describe the currently open main file (pure read of bpy.data)."""
    context_scene = getattr(bpy.context, "scene", None)
    scene = scene or context_scene or (bpy.data.scenes[0] if len(bpy.data.scenes) else None)
    view_layer = None
    if scene is not None:
        context_layer = getattr(bpy.context, "view_layer", None)
        if context_layer is not None and any(
            layer.as_pointer() == context_layer.as_pointer() for layer in scene.view_layers
        ):
            view_layer = context_layer
        elif len(scene.view_layers):
            view_layer = scene.view_layers[0]
    states = _layer_states(view_layer)
    collections = list(bpy.data.collections)
    objects = list(bpy.data.objects)
    if len(collections) > MAX_COLLECTIONS or len(objects) > MAX_OBJECTS:
        raise ValueError("Blender file has too many collections or objects to list")

    parents = {collection.as_pointer(): [] for collection in collections}
    for collection in collections:
        for child in collection.children:
            parents.setdefault(child.as_pointer(), []).append(str(collection.name))
    scene_pointer = scene.collection.as_pointer() if scene is not None else None
    collection_rows = []
    for collection in collections:
        key = collection.as_pointer()
        owners = parents.get(key, [])
        collection_rows.append(
            {
                "name": str(collection.name),
                "parent": owners[0] if owners else None,
                "parents": list(owners),
                "children": [str(child.name) for child in collection.children],
                "objects": [str(obj.name) for obj in collection.objects],
                "excluded": bool(states.get(key, False)),
                "in_scene": key in states,
                "hide_render": bool(getattr(collection, "hide_render", False)),
                "library": _library(collection),
            }
        )

    object_rows = []
    for obj in objects:
        users = [
            str(collection.name)
            for collection in obj.users_collection
            if collection.as_pointer() != scene_pointer and not getattr(collection, "is_embedded_data", False)
        ]
        instance = (
            getattr(obj, "instance_collection", None) if getattr(obj, "instance_type", "") == "COLLECTION" else None
        )
        object_rows.append(
            {
                "name": str(obj.name),
                "type": str(obj.type),
                "parent": str(obj.parent.name) if obj.parent is not None else None,
                "collections": users,
                "has_geometry_nodes": _has_geometry_nodes(obj),
                "material_slots": _material_slots(obj),
                "hide_render": bool(getattr(obj, "hide_render", False)),
                "instance_collection": str(instance.name) if instance is not None else None,
                "library": _library(obj),
            }
        )

    scene_rows = []
    for candidate in bpy.data.scenes:
        layer_names = [str(layer.name) for layer in candidate.view_layers]
        if candidate == scene and view_layer is not None:
            active = str(view_layer.name)
        else:
            active = layer_names[0] if layer_names else ""
        scene_rows.append(
            {
                "name": str(candidate.name),
                "view_layers": layer_names,
                "active_view_layer": active,
                "collections": [str(child.name) for child in candidate.collection.children],
                "objects": [str(obj.name) for obj in candidate.collection.objects],
            }
        )

    if scene is not None:
        render = scene.render
        fps = float(render.fps) / float(render.fps_base or 1.0)
        frame_range = [int(scene.frame_start), int(scene.frame_end)]
    else:
        fps, frame_range = 24.0, [1, 1]
    return {
        "active_scene": str(scene.name) if scene is not None else None,
        "scenes": scene_rows,
        "collections": collection_rows,
        "objects": object_rows,
        "frame_range": frame_range,
        "fps": fps,
    }


def list_file(path):
    """Open path read-only (no UI, no scripts) and describe it. Never saves."""
    import bpy

    source = str(Path(path).resolve())
    if not Path(source).is_file():
        raise FileNotFoundError("Blender file not found: " + source)
    bpy.ops.wm.open_mainfile(filepath=source, load_ui=False, use_scripts=False)
    result = list_contents(bpy)
    result["source"] = source
    return result
