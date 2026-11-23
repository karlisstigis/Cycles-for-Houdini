# SPDX-License-Identifier: Apache-2.0
"""Freeze native object/category selections; no SOP cooking or numeric artist IDs."""

import re, posixpath
from collections import defaultdict
import hou


_MASK_FIELDS = ("cycles_illuminates", "cycles_shadow_objects", "shadowmask", "lightmask")
_EMITTER_FIELDS = (*_MASK_FIELDS[:3], "shadowcategories", "categories")
_GEOMETRY_FIELDS = (*_EMITTER_FIELDS, "lightmask", "lightcategories")
_NATIVE_VIEWPORT_FIELDS = frozenset(
    (
        "ogl_enablelight",
        "light_contribprimary",
        "light_type",
        "light_color",
        "areasize",
        "coneenable",
        "coneangle",
        "conedelta",
        "shadow_type",
    )
)


def _light_identity(light):
    return light.get("linking_id") or light.get("source_path") or light["key"]


def _literal_fields(node, fields):
    """Return authored strings only when evaluation cannot change their selectors."""
    values = []
    for field in fields:
        parm = node.parm(field) if node is not None else None
        if parm is not None:
            if parm.isOverrideTrackActive():
                return None
            text = parm.unexpandedString()
            if any(marker in text for marker in ("$", "`", "@")):
                return None
        else:
            text = None
        values.append(text)
    return tuple(values)


def geometry_selectors_are_literal(geometry):
    """Unsafe to retain linking when object selectors can change by evaluation."""
    paths = {request["object_path"] for request in geometry}
    try:
        return all(_literal_fields(hou.node(path), _GEOMETRY_FIELDS) is not None for path in paths)
    except hou.Error:
        return False


def light_selector_signature(lights, world):
    """The inputs to linking that can change on a lights-only capture."""
    result = []
    try:
        for light in lights:
            identity = _light_identity(light)
            fields = _literal_fields(hou.node(identity), _EMITTER_FIELDS)
            if fields is None:
                return None
            result.append((identity, light.get("cast_shadow", True), fields))
        world_path = world.get("world", {}).get("source_path", "")
        world_fields = _literal_fields(hou.node(world_path), ("categories",))
        if world_fields is not None:
            return tuple(result), world_path, world_fields
    except hou.Error:
        pass
    return None


def references_only_by_linking(dependent, source):
    """Recognize a literal mask edge only when the OBJ has no ambiguous authored dependency."""
    from .light_properties import is_cycles_light

    try:
        if dependent.type().category() != hou.objNodeTypeCategory():
            return False
        masks = _literal_fields(dependent, _MASK_FIELDS)
        if masks is None:
            return False
        scope = dependent.parent()
        for mask in masks:
            for term in re.split(r"[,\s]+", mask or ""):
                term = term.removeprefix("^")
                if term.startswith(("./", "../")):
                    term = posixpath.normpath(posixpath.join(dependent.path(), term))
                if term and scope.node(term) == source:
                    native_light = is_cycles_light(dependent) and dependent.matchesCurrentDefinition()
                    for parm in dependent.parms():
                        if parm.isOverrideTrackActive():
                            return False
                        if parm.keyframes():
                            # Factory viewport mirrors only read this light's own channels.
                            if parm.rawValue() == "lock(1)" or (
                                native_light
                                and parm.tuple().name() in _NATIVE_VIEWPORT_FIELDS
                                and parm.isAtDefault(compare_temporary_defaults=False, compare_expressions=True)
                            ):
                                continue
                            return False
                        if parm.name() in _MASK_FIELDS or parm.parmTemplate().type() != hou.parmTemplateType.String:
                            continue
                        text = parm.unexpandedString()
                        if "`" in text or "$" in text or source.name() in text:
                            return False
                    return True
    except hou.Error:
        pass
    return False


def configuration(scene, world, now, diagnostics):
    from .object_scene import value

    roots = {request["object_path"]: hou.node(request["object_path"]) for request in scene["geometry"]}
    roots = {path: node for path, node in roots.items() if node is not None}
    emitters = dict(roots)
    shadow_enabled = {}
    for light in scene["lights"]:
        path = _light_identity(light)
        emitters[path] = hou.node(path)
        shadow_enabled[path] = light.get("cast_shadow", True)
    world_node = hou.node(world.get("world", {}).get("source_path", ""))
    emitters["world"] = world_node
    shadow_enabled["world"] = world.get("world", {}).get("cast_shadow", True)
    universe = frozenset(emitters)
    objects = frozenset(roots)
    resolved = {}
    categories = {}

    def issue(node, field, error):
        diagnostics.append(
            dict(
                code="linking.source_unavailable",
                source=node.path(),
                resource="linking:" + field,
                message=field + ": " + str(error),
                action="Ignoring this selector; retaining other supported linking controls",
            )
        )

    def mask(node, field, targets, is_emitter=False):
        text = value(node, field, now, "*") if node else "*"
        if not isinstance(text, str):
            issue(node, field, "Expected an object mask string")
            return targets
        if text == "*":
            return targets
        if not text.strip():
            return frozenset()
        scope = node.parent() if node else hou.node("/obj")
        terms = []
        for term in re.split(r"[,\s]+", text.strip()):
            prefix = "^" if term.startswith("^") else ""
            term = term[1:] if prefix else term
            if node and term.startswith(("./", "../")):
                term = posixpath.normpath(posixpath.join(node.path(), term))
            terms.append(prefix + term)
        text = " ".join(terms)
        key = (scope.path(), text, is_emitter)
        if key not in resolved:
            try:
                matches = {item.path() for item in scope.recursiveGlob(text, filter=hou.nodeTypeFilter.Obj)}
                if is_emitter:
                    # __world is the stable artist alias even for a constant or
                    # material world with no native environment OBJ.
                    selected = False
                    for term in terms:
                        excluded = term.startswith("^")
                        term = term[1:] if excluded else term
                        if not term:
                            continue
                        world_match = hou.text.patternMatch(term, "__world")
                        if world_node:
                            world_match |= world_node in scope.recursiveGlob(term, filter=hou.nodeTypeFilter.Obj)
                            if term.startswith("/"):
                                world_match |= hou.text.patternMatch(term, world_node.path())
                        if world_match:
                            selected = not excluded
                    if selected:
                        matches.add("world")
                resolved[key] = frozenset(matches)
            except hou.Error as error:
                issue(node, field, error)
                resolved[key] = None
        return targets if resolved[key] is None else targets & resolved[key]

    def category(node, field, candidates):
        text = value(node, field, now, "") if node else ""
        if not isinstance(text, str):
            issue(node, field, "Expected a category selector string")
            return candidates
        if not text.strip():
            return candidates
        try:
            if "(" in text or ")" in text:
                raise ValueError("Category grouping is unsupported by native category syntax")
            expression = text.replace(",", "|")
            if "&" not in expression and "|" not in expression:
                expression = "|".join(expression.split())
            clauses = [[term.strip() for term in clause.split("&")] for clause in expression.split("|")]
            if any(not term or any(c.isspace() for c in term) for clause in clauses for term in clause):
                raise ValueError("Invalid category expression")

            def tagged(identity):
                if identity not in categories:
                    source = emitters.get(identity) or roots.get(identity)
                    tags = value(source, "categories", now, "") if source else ""
                    if not isinstance(tags, str):
                        issue(source, "categories", "Expected category tags as a string")
                        categories[identity] = None
                    else:
                        categories[identity] = frozenset(re.split(r"[,\s]+", tags.strip())) - {""}
                return categories[identity]

            def term_matches(term, identity):
                if term == "*":
                    return True
                if term == "-*":
                    return False
                tags = tagged(identity)
                # Malformed source metadata omits only this candidate's category
                # filtering. Object masks and the other candidates remain valid.
                if tags is None:
                    return True
                if term == "+":
                    return bool(tags)
                if term == "-":
                    return not tags
                return term[1:] not in tags if term.startswith("-") else term in tags

            return frozenset(
                identity
                for identity in candidates
                if any(all(term_matches(term, identity) for term in clause) for clause in clauses)
            )
        except ValueError as error:
            issue(node, field, error)
            return candidates

    receiver_excluded = defaultdict(set)
    blocker_excluded = defaultdict(set)
    for identity, node in emitters.items():
        if node is None or identity == "world":
            continue
        illuminated = mask(node, "cycles_illuminates", objects)
        shadowed = (
            category(node, "shadowcategories", mask(node, "cycles_shadow_objects", mask(node, "shadowmask", objects)))
            if shadow_enabled.get(identity, True)
            else objects
        )
        for path in objects - illuminated:
            receiver_excluded[path].add(identity)
        for path in objects - shadowed:
            blocker_excluded[path].add(identity)
    receivers = {}
    blockers = {}
    for path, node in roots.items():
        allowed = category(node, "lightcategories", mask(node, "lightmask", universe, True))
        denied = receiver_excluded.get(path, frozenset())
        if allowed != universe or denied:
            receivers[path] = allowed - denied
        if path in blocker_excluded:
            blockers[path] = universe - blocker_excluded[path]

    def rules(bindings):
        grouped = defaultdict(list)
        for path, allowed in bindings.items():
            excluded = universe - allowed
            invert = len(excluded) < len(allowed)
            grouped[(invert, tuple(sorted(excluded if invert else allowed)))].append(path)
        return [
            dict(objects=sorted(paths), emitters=list(emitters), exclude=invert)
            for (invert, emitters), paths in sorted(grouped.items())
        ]

    return dict(receivers=rules(receivers), blockers=rules(blockers))


def bundle_references(nodes, now):
    from .object_scene import value

    patterns = set()
    for node in nodes:
        if node.type().category() != hou.objNodeTypeCategory():
            continue
        for field in ("lightmask", "shadowmask", "cycles_illuminates", "cycles_shadow_objects"):
            text = value(node, field, now, "")
            if isinstance(text, str):
                patterns.update(re.findall(r"@([^\s,^]+)", text))
    return tuple(sorted(patterns))


def bundle_signature(patterns):
    """Metadata only; callers throttle this independently of viewport navigation."""
    if not patterns:
        return ()
    return tuple(
        (bundle.name(), tuple(bundle.nodes()))
        for bundle in hou.nodeBundles()
        if any(hou.text.patternMatch(pattern, bundle.name()) for pattern in patterns)
    )


def parameter_template(emission=False):
    from .object_properties import _section

    def selector(name, label, help):
        return hou.StringParmTemplate(
            "cycles_" + name,
            label,
            1,
            default_value=("*",),
            string_type=hou.stringParmType.NodeReferenceList,
            tags={"opfilter": "!!OBJ/GEOMETRY!!", "oprelative": "."},
            help=help,
        )

    parameters = []
    if emission:
        lightmask = selector(
            "unused",
            "Light Mask",
            "Object light mask, combined with emitter selections. __world keeps only the world and filters other emitters. The pinned engine cannot exclude world illumination: unsupported world exclusions report a warning and retain it. Does not affect geometry or camera visibility.",
        )
        lightmask.setName("lightmask")
        lightmask.setTags({"opfilter": "!!OBJ!!", "oprelative": "."})
        parameters.append(lightmask)
        parameters.append(
            hou.StringParmTemplate(
                "lightcategories",
                "Light Selection",
                1,
                default_value=("",),
                help="Optional native category expression. Intersects Light Mask. Supports &, |, -tag, *, -*, + and -. Empty applies no category filter.",
            )
        )
    parameters += [
        selector(
            "illuminates",
            "Illuminates",
            "Object paths, wildcards, exclusions and bundles. Only these OBJ roots receive this emitter; their native Light Mask and Light Selection also apply. Packed children inherit the root selection. Does not hide the emitter from the camera.",
        ),
        selector(
            "shadow_objects",
            "Shadow Casters",
            "Only these OBJ roots block this emitter. Uses Cycles shadow linking and keeps their camera visibility. Native Shadow Mask and Shadow Selection also apply when present.",
        ),
        hou.StringParmTemplate(
            "shadowcategories",
            "Shadow Selection",
            1,
            default_value=("",),
            help="Optional native category filter for this emitter's blockers, intersecting Shadow Casters and the native Shadow Mask.",
        ),
        hou.StringParmTemplate(
            "categories",
            "Categories",
            1,
            default_value=("",),
            help="Native object category tags, separated by spaces or commas. Selections on other objects/lights can refer to these tags.",
        ),
    ]
    if not emission:
        for parm in parameters:
            if parm.name() in ("cycles_shadow_objects", "shadowcategories"):
                parm.setConditional(hou.parmCondType.DisableWhen, "{ cycles_light_cast_shadow == 0 }")
    return _section("emission_linking" if emission else "light_linking", "Linking", parameters)
