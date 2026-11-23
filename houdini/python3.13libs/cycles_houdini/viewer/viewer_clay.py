# SPDX-License-Identifier: Apache-2.0
"""Clay: a viewer state that renders every surface with one neutral grey material.

Applied to the viewer's own scene capture only, after it is read from Houdini and
before it reaches the renderer. Scene material assignments and ROP parameters are
never written. Each material keeps its volume and displacement outputs; only its
surface closure is replaced. Lights and the world are not materials and stay as
they are.
"""

from functools import wraps

# Linear albedo of the clay surface: mid grey, matte.
COLOR = (0.5, 0.5, 0.5)
NODE = "__cycles_viewer_clay"
# ROP fallback shaders that describe a surface; volume kinds stay unchanged.
SURFACE_KINDS = ("diffuse", "principled", "emission")


def surface_node():
    return dict(id=NODE, definition="cycles/diffuse_bsdf/1", parameters={"color": COLOR, "roughness": 0.0})


def graph(source):
    """A material graph whose surface is clay; volume and displacement are retained."""
    if not isinstance(source, dict) or "surface" not in source:
        return source  # Volume and displacement-only graphs have no surface to replace.
    result = dict(source)
    terminals = [source[name]["node"] for name in ("volume", "displacement") if name in source]
    upstream = {}
    for link in source.get("links", ()):
        upstream.setdefault(link["to"]["node"], set()).add(link["from"]["node"])
    keep, pending = set(), list(terminals)
    while pending:
        key = pending.pop()
        if key not in keep:
            keep.add(key)
            pending.extend(upstream.get(key, ()))
    # Drop the replaced surface network: its textures and attributes are no longer read.
    result["nodes"] = sorted(
        [node for node in source.get("nodes", ()) if node["id"] in keep] + [surface_node()], key=lambda n: n["id"]
    )
    result["links"] = [link for link in source.get("links", ()) if link["to"]["node"] in keep]
    result["surface"] = dict(node=NODE, socket="BSDF")
    return result


def material(definition):
    """One resolved material definition with a clay surface."""
    if not isinstance(definition, dict):
        return definition
    kind = definition.get("kind")
    if kind == "graph":
        return dict(definition, graph=graph(definition.get("graph")))
    if kind in SURFACE_KINDS:
        return dict(
            key=definition["key"],
            kind="diffuse",
            color=COLOR,
            roughness=0.0,
            cryptomatte_name=definition.get("cryptomatte_name", ""),
            pass_index=definition.get("pass_index", 0),
        )
    return definition  # Volume fallback shaders and diagnostic materials.


def resolver(source):
    @wraps(source)
    def resolve(references, at_time, *args, **kwargs):
        return [material(item) for item in source(references, at_time, *args, **kwargs)]

    resolve.cycles_clay = True
    return resolve


def apply(update):
    """Return the capture with clay materials (a shallow copy; the input is not changed)."""
    if "materials" not in update and "material_resolver" not in update:
        return update
    result = dict(update)
    if "materials" in update:
        result["materials"] = [material(item) for item in update["materials"]]
    source = update.get("material_resolver")
    if source is not None and not getattr(source, "cycles_clay", False):
        result["material_resolver"] = resolver(source)
    return result
