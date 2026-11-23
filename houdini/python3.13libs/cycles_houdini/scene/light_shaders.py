# SPDX-License-Identifier: Apache-2.0
"""Owned analytic-emitter graphs; no geometry cooking or renderer calls."""

from functools import lru_cache
import os
import hou


def _node(identity, kind, **parameters):
    return dict(id=identity, definition="cycles/" + kind + "/1", parameters=parameters)


def _link(source, socket, target, input):
    return {"from": dict(node=source, socket=socket), "to": dict(node=target, socket=input)}


@lru_cache(maxsize=1)
def _allowed():
    from .. import capabilities

    return frozenset(capabilities()["light_graph_definitions"])


def material_path(node, now):
    parm = node.parm("cycles_light_material")
    path = parm.evalAtTime(now) if parm else ""
    if not path:
        return ""
    import posixpath

    return path if path.startswith("/") else posixpath.normpath(posixpath.join(node.path(), path))


def graph(node, now, kind, size=None, diagnostics=None):
    from .object_scene import value

    selected = material_path(node, now)
    if selected:
        from ..materials import material_graph, MaterialSourceError

        try:
            builder = hou.node(selected)
            if builder is None:
                raise MaterialSourceError("Missing Emission Material: " + selected)
            result = material_graph(builder, now)
            if not result.get("surface"):
                raise MaterialSourceError("Connect an Emission or color output for this analytic light")
            omitted = bool(result.get("volume") or result.get("displacement"))
            for key in ("volume", "displacement", "displacement_method", "displacement_time_dependent"):
                result.pop(key, None)
            incoming = {}
            for link in result["links"]:
                incoming.setdefault(link["to"]["node"], []).append(link["from"]["node"])
            used = set()
            pending = [result["surface"]["node"]]
            while pending:
                current = pending.pop()
                if current in used:
                    continue
                used.add(current)
                pending.extend(incoming.get(current, ()))
            result["nodes"] = [item for item in result["nodes"] if item["id"] in used]
            result["links"] = [item for item in result["links"] if item["to"]["node"] in used]
            if omitted and diagnostics is not None:
                diagnostics.append(
                    dict(
                        code="light.unsupported_setting",
                        source=node.path(),
                        resource=f"light:{node.sessionId()}",
                        message=selected + ": analytic lights do not evaluate volume or displacement outputs",
                        action="Retained the independent emission output",
                    )
                )
            unsupported = [item["definition"] for item in result["nodes"] if item["definition"] not in _allowed()]
            if unsupported:
                raise MaterialSourceError("Analytic emission needs Emission/color/utility nodes, not " + unsupported[0])
            return result
        except MaterialSourceError as error:
            if diagnostics is not None:
                diagnostics.append(
                    dict(
                        code="light.shader_unavailable",
                        source=node.path(),
                        resource=f"light:{node.sessionId()}",
                        message=selected + ": " + str(error),
                        action="Rendering the light with its color and power; omitted the assigned emission material",
                    )
                )
            return None
    texture = value(node, "cycles_light_texture", now, "")
    if not texture and kind in ("grid", "disk"):
        texture = value(node, "areamap", now, "")
    profile = value(node, "cycles_light_ies", now, "")
    if not texture and not profile:
        return None

    def path(source):
        if source.startswith("op:"):
            return source
        return os.path.normpath(source if os.path.isabs(source) else os.path.join(hou.expandString("$HIP"), source))

    nodes = [_node("emission", "emission", color=(1.0, 1.0, 1.0), strength=1.0)]
    links = []
    if profile or (texture and kind not in ("grid", "disk")):
        nodes += [
            _node("direction", "geometry"),
            _node(
                "local_direction",
                "vector_transform",
                transform_type="vector",
                convert_from="world",
                convert_to="object",
            ),
        ]
        links += [_link("direction", "incoming", "local_direction", "vector")]
    if texture:
        color = value(node, "cycles_light_texture_colorspace", now, "auto")
        if kind in ("grid", "disk"):
            nodes += [
                _node("position", "geometry"),
                _node(
                    "local_position",
                    "vector_transform",
                    transform_type="point",
                    convert_from="world",
                    convert_to="object",
                ),
                _node(
                    "normalized_position",
                    "vector_math",
                    math_type="divide",
                    vector2=(float(size[0]), float(size[1]), 1.0),
                ),
                _node("texture_coordinates", "vector_math", math_type="add", vector2=(0.5, 0.5, 0.0)),
                _node("color_texture", "image_texture", filename=path(texture), colorspace=color, extension="clamp"),
            ]
            links += [
                _link("position", "position", "local_position", "vector"),
                _link("local_position", "vector", "normalized_position", "vector1"),
                _link("normalized_position", "vector", "texture_coordinates", "vector1"),
                _link("texture_coordinates", "vector", "color_texture", "vector"),
            ]
        else:
            nodes += [_node("color_texture", "environment_texture", filename=path(texture), colorspace=color)]
            links += [_link("local_direction", "vector", "color_texture", "vector")]
        links += [_link("color_texture", "color", "emission", "color")]
    if profile:
        nodes += [_node("ies_profile", "ies_light", filename=path(profile))]
        links += [
            _link("local_direction", "vector", "ies_profile", "vector"),
            _link("ies_profile", "fac", "emission", "strength"),
        ]
    result = dict(schema_version=1, nodes=nodes, links=links, surface=dict(node="emission", socket="emission"))
    if texture.startswith("op:"):
        result["image_time"] = float(now)
    return result
