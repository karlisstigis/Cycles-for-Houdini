# SPDX-License-Identifier: Apache-2.0
"""Houdini point instancing (Instance OBJ, s@instance) for the native scene capture.

Python resolves ordinary prototype objects and immutable external mesh resources.
The HDK adapter uses Houdini packed geometry for OBJ prototypes and places external
MeshData directly. External frame selection reads only the instance/index values;
point transforms and shading fields are captured natively.
"""

import posixpath
import json
import math
import os
import hou

MODES = {"off": "off", "on": "full", "fast": "fast"}
MOTION = {"off": None, "deform": "deformation", "velocity": "velocity"}
_GENERATED_MARKER = "cycles_external_blend"

# Latest prototype dependencies per ROP session id: (objects, missing absolute paths). The
# scene observer subscribes to them without reading geometry in Houdini callbacks.
_dependencies = {}
_external_dependencies = {}
_prepared_keys = {}
_preparing_external = 0


def is_blend_reference(value):
    path, separator, _ = value.partition("#")
    return bool(separator) and path.lower().endswith(".blend")


def is_generated(node):
    """Owned import prototypes do not participate in ordinary scene membership."""
    while node is not None:
        if node.userData(_GENERATED_MARKER):
            return True
        node = node.parent()
    return False


def generated_nodes(nodes):
    """Find generated nodes in one current scene walk, sharing ancestry queries."""
    generated = set()
    known = {}
    for source in nodes:
        ancestors = []
        node = source
        while node is not None and node not in known:
            if node.userData(_GENERATED_MARKER):
                known[node] = True
                break
            ancestors.append(node)
            node = node.parent()
        result = known.get(node, False)
        for ancestor in ancestors:
            known[ancestor] = result
        if result:
            generated.add(source)
    return generated


def external_frames(sop, now, values):
    """Unique external reference/frame pairs; ordinary OBJ paths never read index."""
    references = {value for value in values if is_blend_reference(value)}
    if not references:
        return {}, 0
    geometry = sop.geometryAtFrame(hou.timeToFrame(now)) if hasattr(sop, "geometryAtFrame") else sop.geometry()
    attribute = geometry.findPointAttrib("index")
    if attribute is not None and (attribute.dataType() != hou.attribData.Float or attribute.size() != 1):
        raise ValueError("Blender instance index must be a scalar float point attribute: " + sop.path())
    # HOM returns doubles here (unlike the binary-string API's Float32 default),
    # preserving Float64 index values for the native double-key lookup.
    frames = geometry.pointFloatAttribValues("index") if attribute is not None else None
    result, invalid = {}, 0
    for i, reference in enumerate(geometry.pointStringAttribValues("instance")):
        if reference not in references:
            continue
        frame = float(frames[i]) if frames is not None else float(hou.timeToFrame(now))
        if not math.isfinite(frame):
            invalid += 1
            continue
        result.setdefault(reference, set()).add(frame)
    return result, invalid


def is_instancer(obj):
    return (
        obj is not None
        and obj.type().category() == hou.objNodeTypeCategory()
        and obj.type().name().split("::")[0].rsplit("/", 1)[-1] == "instance"
        and obj.parm("ptinstance") is not None
    )


def mode(obj, now):
    token = obj.parm("ptinstance").evalAtTime(now)
    token = obj.parm("ptinstance").parmTemplate().menuItems()[token] if isinstance(token, int) else token
    if token not in MODES:
        raise ValueError("Unsupported Point Instancing mode on " + obj.path() + ": " + str(token))
    return MODES[token]


def motion(obj, now):
    parm = obj.parm("ptmotionblur")
    token = parm.evalAtTime(now) if parm else "deform"
    return token if token in MOTION else "deform"


def motion_settings(obj, now, inherited, *, point_instancer=False):
    """One effective geometry-motion policy for discovery and native requests."""
    from . import object_properties

    # The object's own choice (not From ROP): missing v is then reported natively.
    explicit = object_properties._value(obj, "geometry_motion", now, "inherit") != "inherit"
    result = dict(
        motion_blur=object_properties.motion_enabled(obj, now),
        motion=object_properties.motion_mode(obj, now, inherited),
        motion_explicit=explicit,
    )
    if point_instancer and not explicit:
        native_motion = MOTION[motion(obj, now)]
        if native_motion is None:
            result["motion_blur"] = False
        else:
            result["motion"] = native_motion
    return result


def absolute(obj, value):
    """Soho's IFDmisc.absoluteObjectPath: relative to the instancer object; op: accepted."""
    path = value[3:] if value.startswith("op:") else value
    return posixpath.normpath(path if path.startswith("/") else posixpath.join(obj.path(), path))


def resolve(obj, value):
    """(object or None, reason)."""
    node = obj.node(value[3:] if value.startswith("op:") else value)
    if node is None:
        return None, "does not exist"
    if node.type().category() != hou.objNodeTypeCategory():
        return None, "is not an object (Houdini instances objects, not SOPs)"
    from .light_properties import supported as is_light

    if is_light(node) or node.type().name().split("::")[0] in ("hlight", "envlight", "ambient", "indirectlight"):
        return None, "is a light; instancing lights is not supported"
    if is_subnet(node):
        return node, ""
    if node.renderNode() is None or node.renderNode().type().category() != hou.sopNodeTypeCategory():
        return None, "has no renderable SOP geometry"
    return node, ""


def is_subnet(node):
    sop = node.renderNode() if node.type().category() == hou.objNodeTypeCategory() else None
    return (
        node.isNetwork()
        and node.childTypeCategory() == hou.objNodeTypeCategory()
        and (sop is None or sop.type().category() != hou.sopNodeTypeCategory())
    )


def subnet_members(instancer, subnet, now):
    """Member objects of a subnet prototype, filtered by the instancer's Subnetwork Filtering."""
    # Instance OBJs retain their native controls. Ordinary Geometry OBJs with
    # s@instance use the Cycles tab, without requiring Instance OBJ properties.
    prefix = "" if is_instancer(instancer) else "cycles_"

    def value(name, default):
        parm = instancer.parm(prefix + name)
        return parm.evalAtTime(now) if parm else default

    choice = value("subnet_filter", "display" if is_instancer(instancer) else "all")
    if choice not in ("all", "display", "pattern"):
        raise ValueError("Invalid Subnetwork Filtering on " + instancer.path() + ": " + str(choice))
    pattern = value("subnet_pattern", "*") if choice == "pattern" else "*"
    from .light_properties import supported as is_light

    result = []
    for node in subnet.recursiveGlob(pattern or "*", filter=hou.nodeTypeFilter.ObjGeometry):
        if is_generated(node):
            continue
        sop = node.renderNode()
        if sop is None or sop.type().category() != hou.sopNodeTypeCategory() or is_light(node):
            continue
        if node.type().name().split("::")[0] == "null":
            continue
        if choice == "display" and not node.isObjectDisplayedAtFrame(hou.timeToFrame(now)):
            continue
        result.append(node)
    return sorted(result, key=lambda node: node.path())


def referenced_values(obj, sop, now):
    """Unique per-point instance strings (string table only) and whether the attribute exists."""
    geometry = sop.geometryAtFrame(hou.timeToFrame(now)) if hasattr(sop, "geometryAtFrame") else sop.geometry()
    if geometry is None:
        return (), False
    attribute = geometry.findPointAttrib("instance")
    if attribute is None:
        return (), False
    if attribute.dataType() != hou.attribData.String:
        raise ValueError("The instance point attribute must be a string: " + sop.path())
    return tuple(attribute.strings()), True


def has_instance_attribute(sop, now):
    """Any object whose points carry s@instance instances them (like Redshift and Octane)."""
    geometry = sop.geometryAtFrame(hou.timeToFrame(now)) if hasattr(sop, "geometryAtFrame") else sop.geometry()
    attribute = geometry.findPointAttrib("instance") if geometry is not None else None
    return attribute is not None and attribute.dataType() == hou.attribData.String


def packed_references(sop, now):
    """Read serialized primitive payloads, including references in native packs.

    This native discovery never resolves a Blender reference or reads its proxy
    box. It works equally after a file SOP, copy, merge, or scene reopen.
    """
    from .. import load

    return tuple(load().packed_blender_references(sop.path(), float(now)))


class ExternalPlan:
    """Main-thread discovery with background process startup, owned by a capture.

    Discovery only reads the attributes already needed by point instancing.
    No HOM objects or callbacks are passed to Blender's startup thread.
    """

    def __init__(self, sops, now, times=(), *, rop=None):
        self.sops, self.now = sops, now
        self.rop = rop
        self.times = tuple(dict.fromkeys((now, *times)))
        self.specs = {}
        self.observations = {}
        self.packed_observations = {}
        self.packed_specs = set()
        self.point_frames = {}
        self.results = None
        self.warmups = []
        self.warm_sources = set()

    def _discover(self):
        from ..blender import blender_assets

        now = self.now
        for sop in self.sops:
            if sop is None:
                continue
            obj = sop.parent()
            # A SOP may live inside subnet/HDA networks; find its OBJ owner.
            while obj is not None and obj.type().category() != hou.objNodeTypeCategory():
                obj = obj.parent()
            if obj is None:
                continue
            try:
                if is_instancer(obj) and mode(obj, now) == "off":
                    continue
                times = self.times
                if self.rop is not None:
                    settings = motion_settings(
                        obj, now, self.rop.parm("geometry_motion").evalAtTime(now), point_instancer=is_instancer(obj)
                    )
                    # Velocity if Available is decided natively from the frame's v.
                    if not settings["motion_blur"] or settings["motion"] not in (
                        "deformation",
                        "velocity_if_available",
                    ):
                        times = (now,)
                frames, packed, point_frames = {}, set(), {}
                for sample in times:
                    # Shutter samples only look for Blender references. Reading
                    # geometry at another time cooks the SOP there, and the
                    # native capture then recooks the frame with new data ids,
                    # which defeats geometry reuse. Without a Blender reference
                    # at the frame, no other time is read.
                    if sample != now and not frames:
                        break
                    values, has_attribute = referenced_values(obj, sop, sample)
                    try:
                        sampled_frames, invalid = external_frames(sop, sample, values)
                    except ValueError:
                        # A bad point index must not suppress independent
                        # packed discovery at this or another shutter sample.
                        sampled_frames = {}
                    else:
                        if sample == now:
                            self.observations[sop.sessionId()] = (values, has_attribute, sampled_frames, invalid)
                    if is_instancer(obj):
                        fallback = obj.parm("instancepath").evalAtTime(sample)
                        if is_blend_reference(fallback):
                            sampled_frames.setdefault(fallback, set()).add(float(hou.timeToFrame(sample)))
                    for reference, selected in sampled_frames.items():
                        point_frames.setdefault(reference, set()).update(selected)
                        frames.setdefault(reference, set()).update(selected)
                    for reference, frame in packed_references(sop, sample):
                        packed.add((reference, frame))
                        frames.setdefault(reference, set()).add(frame)
                self.point_frames[sop.sessionId()] = point_frames
                setting = obj.parm("cycles_blender_subdivision")
                native = bool(setting.evalAtTime(now)) if setting is not None else True
                self.packed_observations[sop.sessionId()] = tuple(sorted(packed))
                for reference, frame in packed:
                    frames.setdefault(reference, set()).add(frame)
                    self.packed_specs.add((reference, frame, native))
                for value, samples in frames.items():
                    specs = [(value, frame, native) for frame in sorted(samples)]
                    for spec in specs:
                        self.specs[spec] = None
                    source = value.partition("#")[0]
                    if specs and source not in self.warm_sources:
                        try:
                            warmup = blender_assets.prewarm_prototypes(specs)
                            if warmup is not None:
                                self.warmups.append(warmup)
                                if warmup.scheduled:
                                    self.warm_sources.add(source)
                        except (ValueError, RuntimeError, OSError, hou.Error):
                            pass  # Authoritative preparation reports failures.
            except (ValueError, RuntimeError, hou.Error):
                # The authoritative request emits the existing source diagnostic.
                continue

    def __enter__(self):
        try:
            self._discover()
        except BaseException:
            self.__exit__()
            raise
        return self

    def get(self, reference, frame, native):
        key = (reference, frame, native)
        if key not in self.specs:
            return None
        if self.results is None:
            from ..blender import blender_assets

            options = dict(publish_bounds=True, bounds_specs=self.packed_specs) if self.packed_specs else {}
            self.results = dict(zip(self.specs, blender_assets.prepare_prototypes(tuple(self.specs), **options)))
        result = self.results[key]
        if isinstance(result, Exception):
            raise result
        return result

    def observation(self, sop):
        return self.observations.get(sop.sessionId())

    def sampled_point_frames(self, sop):
        return self.point_frames.get(sop.sessionId(), {})

    def frame_samples(self):
        return [dict(time=float(time), frame=float(hou.timeToFrame(time))) for time in self.times]

    def packed_observation(self, sop):
        return self.packed_observations.get(sop.sessionId())

    def __exit__(self, *unused):
        for warmup in self.warmups:
            warmup.close()
        self.warmups.clear()


def packed_request(rop, obj, sop, now, base, diagnostics, external_plan=None):
    """Attach evaluated members to the ordinary request's native packed visitor."""
    if not base.get("render_types", {}).get("packed", True):
        return base
    references = external_plan.packed_observation(sop) if external_plan is not None else None
    if references is None:
        references = packed_references(sop, now)
    if not references:
        return base
    from ..blender import blender_assets

    setting = obj.parm("cycles_blender_subdivision")
    native = bool(setting.evalAtTime(now)) if setting is not None else True
    specs = [(reference, frame, native) for reference, frame in references]
    results = None
    prototypes, paths, keys = [], set(), set()
    global _preparing_external
    _preparing_external += 1
    try:
        for reference, frame, _ in specs:
            path = hou.expandString(reference.partition("#")[0])
            paths.add(os.path.abspath(path if os.path.isabs(path) else os.path.join(hou.expandString("$HIP"), path)))
            try:
                result = external_plan.get(reference, frame, native) if external_plan is not None else None
                if result is None:
                    if results is None:
                        results = dict(zip(specs, blender_assets.prepare_prototypes(specs, publish_bounds=True)))
                    result = results[(reference, frame, native)]
                if isinstance(result, Exception):
                    raise result
                paths.update(result.get("dependencies", ()))
                if result.get("cache_key") is not None:
                    keys.add(tuple(result["cache_key"]))
                for issue in result.get("diagnostics", ()):
                    diagnostics.append(dict(issue, source=obj.path(), resource=base["object_key"]))
                members = result["instances"]
            except (ValueError, RuntimeError, OSError, hou.Error) as error:
                diagnostics.append(
                    dict(
                        code="geometry.unavailable_blender_reference",
                        source=obj.path(),
                        resource=base["object_key"],
                        message=reference + " at frame " + repr(frame) + ": " + str(error),
                        action="Omitting this Blender reference; rendering valid packed and ordinary geometry",
                    )
                )
                members = []
            prototypes.append(
                dict(
                    path=reference, node_id=0, source_reference=reference, source_frame=frame, direct_instances=members
                )
            )
    finally:
        _preparing_external -= 1
    session, owner = rop.sessionId(), obj.sessionId()
    _external_dependencies.setdefault(session, {}).setdefault(owner, ())
    _external_dependencies[session][owner] = tuple(sorted(set(_external_dependencies[session][owner]) | paths))
    _prepared_keys.setdefault(session, {}).setdefault(owner, set()).update(keys)
    return dict(base, packed_blender=dict(mode="fast", instancer_path=obj.path(), fallback=-1, prototypes=prototypes))


def request(rop, obj, sop, now, base, build, diagnostics, stack=(), attribute_only=False, external_plan=None):
    """Turn the object's ordinary geometry request into an instancing request.

    build(rop, prototype_sop, now, stack) returns the request of a prototype object.
    attribute_only: a geometry object whose points carry s@instance (no Instance Object
    fallback, separate keys because its other geometry keeps the ordinary request)."""

    def issue(code, message, action):
        diagnostics.append(
            dict(code=code, source=obj.path(), resource=base["object_key"], message=message, action=action)
        )

    if obj.sessionId() in stack:
        raise ValueError("Cyclic point instancing through " + obj.path())
    if len(stack) >= 32:
        raise ValueError("Point instancing is nested more than 32 levels at " + obj.path())
    instancing_mode = "fast" if attribute_only else mode(obj, now)
    fallback_value = "" if attribute_only else obj.parm("instancepath").evalAtTime(now)
    observed = external_plan.observation(sop) if external_plan is not None and instancing_mode != "off" else None
    if observed is not None:
        values, has_attribute = observed[:2]
    elif instancing_mode != "off":
        values, has_attribute = referenced_values(obj, sop, now)
    else:
        values, has_attribute = (), False
    prototypes, index, missing, member_paths, external_paths = [], {}, set(), set(), set()
    prepared_keys = set()
    seen_fallbacks = set()
    setting = obj.parm("cycles_blender_subdivision")
    native_subdivision = bool(setting.evalAtTime(now)) if setting is not None else True
    try:
        frames, invalid_frames = observed[2:] if observed is not None else external_frames(sop, now, values)
    except ValueError as error:
        frames, invalid_frames = {}, 0
        issue(
            "instancing.invalid_blender_index",
            str(error),
            "Omitting external Blender instances; retaining ordinary OBJ instances",
        )
    if invalid_frames:
        issue(
            "instancing.invalid_blender_index",
            str(invalid_frames) + " Blender instances have a non-finite index",
            "Omitting those instances; retaining valid frames and ordinary OBJ instances",
        )
    if external_plan is not None:
        frames = external_plan.sampled_point_frames(sop)
        values = tuple(dict.fromkeys((*values, *frames)))

    # The standalone request path also sends one batch for all its missing
    # samples; scene captures supply a plan spanning all selected instancers.
    local_results = None

    def external_result(value, frame):
        nonlocal local_results
        from ..blender import blender_assets

        if external_plan is not None:
            result = external_plan.get(value, frame, native_subdivision)
            if result is not None:
                return result
        if local_results is None:
            samples = {
                (reference, sample, native_subdivision) for reference, samples in frames.items() for sample in samples
            }
            if is_blend_reference(fallback_value):
                samples.add((fallback_value, float(hou.timeToFrame(now)), native_subdivision))
            samples = sorted(samples)
            local_results = dict(zip(samples, blender_assets.prepare_prototypes(samples)))
        result = local_results.get((value, frame, native_subdivision))
        if result is None:
            return blender_assets.prepare_prototype(value, frame, native_subdivision=native_subdivision)
        if isinstance(result, Exception):
            raise result
        return result

    def add(value, frame=None):
        global _preparing_external
        external = is_blend_reference(value)
        if external:
            frame = float(hou.timeToFrame(now)) if frame is None else frame
            path = hou.expandString(value.split("#", 1)[0])
            external_paths.add(
                os.path.abspath(path if os.path.isabs(path) else os.path.join(hou.expandString("$HIP"), path))
            )
            try:
                from ..blender import blender_assets

                _preparing_external += 1
                try:
                    node = external_result(value, frame)
                finally:
                    _preparing_external -= 1
                external_paths.update(node.get("dependencies", ()))
                if node.get("cache_key") is not None:
                    prepared_keys.add(tuple(node["cache_key"]))
                for detail in node.get("diagnostics", ()):
                    key = (value, detail.get("source", ""), detail["message"])
                    if key not in seen_fallbacks:
                        seen_fallbacks.add(key)
                        diagnostics.append(dict(detail, source=obj.path(), resource=base["object_key"]))
            except (ValueError, RuntimeError, OSError, hou.Error) as error:
                issue(
                    "instancing.unavailable_blender_prototype",
                    value + " at frame " + repr(frame) + ": " + str(error),
                    "Omitting instances that reference it; rendering the others",
                )
                return -1
            identity = (value, frame)
            if identity in index:
                return index[identity]
            index[identity] = len(prototypes)
            proto_request = dict(base)
            proto_request.pop("point_instancing", None)
            prototypes.append(
                dict(
                    path=value,
                    node_id=0,
                    request=proto_request,
                    source_reference=value,
                    source_frame=frame,
                    direct_instances=node["instances"],
                )
            )
            return index[identity]
        else:
            node, reason = resolve(obj, value)
        if node is None:
            missing.add(absolute(obj, value))
            issue(
                "instancing.unavailable_prototype",
                "'" + value + "' " + reason,
                "Omitting instances that reference it; rendering the others",
            )
            return -1
        identity = (value, frame) if external else node.sessionId()
        if identity in index:
            return index[identity]
        if node.sessionId() in stack or node == obj:
            issue(
                "instancing.cycle",
                "Instancing " + node.path() + " from " + obj.path() + " would be cyclic",
                "Omitting these instances",
            )
            return -1
        try:
            if is_subnet(node):
                members = (
                    [
                        member
                        for member in node.recursiveGlob("*", filter=hou.nodeTypeFilter.ObjGeometry)
                        if member.renderNode() is not None
                        and member.renderNode().type().category() == hou.sopNodeTypeCategory()
                    ]
                    if external
                    else subnet_members(obj, node, now)
                )
                if not members:
                    raise ValueError("the subnet has no member objects selected by Subnetwork Filtering")
                entries = [
                    dict(
                        path=m.path(),
                        node_id=m.sessionId(),
                        request=build(rop, m.renderNode(), now, stack + (obj.sessionId(), node.sessionId())),
                    )
                    for m in members
                ]
                member_paths.update(m.path() for m in members)
                proto_request = dict(
                    entries[0]["request"],
                    point_instancing=dict(mode="members", instancer_path=node.path(), fallback=-1, prototypes=entries),
                    render_types=dict(mesh=False, points=False, curves=False, volume=False, packed=True),
                )
            else:
                proto_request = build(rop, node.renderNode(), now, stack + (obj.sessionId(),))
        except ValueError as error:
            issue(
                "instancing.unavailable_prototype",
                node.path() + ": " + str(error),
                "Omitting instances that reference it; rendering the others",
            )
            return -1
        index[identity] = len(prototypes)
        entry = dict(path=node.path(), node_id=node.sessionId(), request=proto_request)
        if external:
            entry.update(source_reference=value, source_frame=frame)
        prototypes.append(entry)
        return index[identity]

    fallback = add(fallback_value) if fallback_value else -1
    for value in values:
        if value:
            if is_blend_reference(value):
                for frame in sorted(frames.get(value, ())):
                    add(value, frame)
            else:
                add(value)
    if instancing_mode != "off" and not has_attribute and not fallback_value and not attribute_only:
        issue(
            "instancing.empty", "No Instance Object and no instance point attribute", "This instancer renders nothing"
        )
    _dependencies.setdefault(rop.sessionId(), {})[obj.sessionId()] = (
        tuple(p["path"] for p in prototypes if not p.get("source_reference")) + tuple(sorted(member_paths)),
        tuple(sorted(missing)),
    )
    prior = _external_dependencies.setdefault(rop.sessionId(), {}).get(obj.sessionId(), ())
    _external_dependencies[rop.sessionId()][obj.sessionId()] = tuple(sorted(set(prior) | external_paths))
    _prepared_keys.setdefault(rop.sessionId(), {}).setdefault(obj.sessionId(), set()).update(prepared_keys)
    base = dict(base)
    if attribute_only:
        base["object_key"] += "/point-instances"
        base["geometry_key"] += "/point-instances"
    base["point_instancing"] = dict(
        mode=instancing_mode,
        instancer_path=obj.path(),
        fallback=fallback,
        prototypes=prototypes,
        keep_attributes=keep_attributes(rop, obj, sop, now, [base] + [p["request"] for p in prototypes]),
    )
    if any(p.get("source_reference") for p in prototypes):
        base["point_instancing"]["frame"] = float(hou.timeToFrame(now))
        if external_plan is not None:
            base["point_instancing"]["frame_samples"] = external_plan.frame_samples()
    base["render_types"] = dict(mesh=False, points=False, curves=False, volume=False, packed=True)
    if not attribute_only:
        base.update(motion_settings(obj, now, base.get("motion", "transform"), point_instancer=True))
    return base


TRIMMED = ("orient", "N", "up", "pscale", "scale", "rot", "trans", "pivot", "transform", "instance")
# Shader nodes that read a named geometry attribute, and the parameter holding the name
# (mirrors shader_attribute_requests in src/core/shader.cpp).
_READERS = (
    ("cycles::attribute::1", "attribute"),
    ("cycles::uvmap::1", "attribute"),
    ("cycles::vertex_color::1", "layer_name"),
    ("cycles::tangent::1", "attribute"),
    ("cycles::normal_map::1", "attribute"),
    ("cycles::vector_displacement::1", "attribute"),
)


def keep_attributes(rop, obj, sop, now, requests=()):
    """Transform-named point attributes that a shader or style sheet in the scene may read.

    The instance transform consumes orient/N/up/pscale/scale/rot/trans/pivot/transform and the
    instance path; instances carry them only when something can read them. Conservative: any
    Cycles attribute-reading node anywhere in the scene, the ROP colour attribute, and any style
    sheet text (object parameter or geometry attribute) that mentions the name."""
    import re

    # Effective overrides may change a promoted attribute-name string. Imported/native styles
    # may also select by any transform attribute. Retain the small fixed set in those cases;
    # never guess from the filename or the material's un-overridden parameter values.
    for request in nested_requests(requests):
        if any(
            request.get(key)
            for key in (
                "object_material_overrides",
                "object_surface_overrides",
                "object_displacement_overrides",
                "object_material_samples",
                "stylesheet",
                "stylesheet_resources",
                "stylesheet_errors",
            )
        ):
            return sorted(TRIMMED)
    geometry = sop.geometryAtFrame(hou.timeToFrame(now)) if hasattr(sop, "geometryAtFrame") else sop.geometry()
    if geometry is not None:
        for owner in (geometry.globalAttribs(), geometry.primAttribs(), geometry.pointAttribs()):
            for attrib in owner:
                if attrib.name() in ("material_override", "material_stylesheet"):
                    return sorted(TRIMMED)
    names = set()
    for type_name, parm_name in _READERS:
        node_type = hou.nodeType(hou.vopNodeTypeCategory(), type_name)
        for node in node_type.instances() if node_type else ():
            parm = node.parm(parm_name)
            if parm is not None:
                try:
                    if parm.keyframes() or parm.getReferencedParm() != parm:
                        return sorted(TRIMMED)
                    names.add(parm.evalAtTime(now))
                except hou.Error:
                    # An unrelated broken reader must not fail capture or justify dropping data.
                    return sorted(TRIMMED)
    if rop.parm("color_attribute") is not None:
        names.add(rop.parm("color_attribute").evalAtTime(now))
    texts = []
    for node in (obj, rop):
        for parm in node.parms():
            if "stylesheet" in parm.name() and parm.parmTemplate().type() == hou.parmTemplateType.String:
                texts.append(parm.evalAsStringAtFrame(hou.timeToFrame(now)))
    geometry = sop.geometryAtFrame(hou.timeToFrame(now)) if hasattr(sop, "geometryAtFrame") else sop.geometry()
    if geometry is not None:
        for owner in (geometry.globalAttribs(), geometry.primAttribs(), geometry.pointAttribs()):
            for attrib in owner:
                if attrib.name() == "material_stylesheet" and attrib.dataType() == hou.attribData.String:
                    texts.extend(attrib.strings())
    text = "\n".join(t for t in texts if t)
    keep = {name for name in TRIMMED if name in names or (text and re.search(r"\b" + re.escape(name) + r"\b", text))}
    return sorted(keep)


def nested_requests(requests):
    """Every owned request; direct external members have no host request."""
    for item in requests:
        yield item
        for role in ("point_instancing", "packed_blender"):
            yield from nested_requests(
                prototype["request"]
                for prototype in item.get(role, {}).get("prototypes", ())
                if prototype.get("request") is not None
            )


def dependencies(rop):
    """Prototype objects and missing paths found by the latest capture of this ROP."""
    found = _dependencies.get(rop.sessionId(), {})
    objects, missing = set(), set()
    for paths, absent in found.values():
        objects.update(paths)
        missing.update(absent)
    return tuple(sorted(objects)), tuple(sorted(missing))


def reset(rop):
    _dependencies.pop(rop.sessionId(), None)
    _external_dependencies.pop(rop.sessionId(), None)
    _prepared_keys.pop(rop.sessionId(), None)


def reset_owner(rop, obj):
    """Replace one captured owner's dependencies; preserve untouched owners."""
    session, owner = rop.sessionId(), obj.sessionId()
    for records in (_dependencies, _external_dependencies, _prepared_keys):
        records.get(session, {}).pop(owner, None)


def external_signature(rop):
    """Cheap source-file observation; the loader validates its bundle on capture."""
    paths = {path for values in _external_dependencies.get(rop.sessionId(), {}).values() for path in values}
    result = []
    for path in sorted(paths):
        try:
            stat = os.stat(path)
            result.append((path, stat.st_size, stat.st_mtime_ns))
        except OSError:
            result.append((path, None, None))
    return tuple(result)


def prepared_keys(rop):
    """Native external resources referenced by this ROP's latest capture."""
    return {key for keys in _prepared_keys.get(rop.sessionId(), {}).values() for key in keys}
