# SPDX-License-Identifier: Apache-2.0
"""Native stylesheet selection and cheap source observation; no geometry reads."""

import json
from ..rop.rop_controls import value as rop_value


def object_binding(obj, now, stylesheets=()):
    import posixpath

    parm = obj.parm("shop_materialpath")
    path = parm.evalAtTime(now) if parm else ""
    if path.startswith("op:"):
        path = path[3:]
    if path and not path.startswith("/"):
        path = posixpath.normpath(posixpath.join(obj.path(), path))
    override = obj.parm("material_override")
    literal = override.evalAtTime(now) if override else ""
    stylesheet, errors, resources = object_styles(obj, now, stylesheets)
    return dict(
        object_material_path=path,
        object_material_overrides=[literal] if literal not in ("", "{}") else [],
        stylesheet=stylesheet,
        stylesheet_errors=errors,
        stylesheet_resources=resources,
    )


def object_binding_samples(obj, rop, now, times, nominal):
    """Host-only parameter reads; retain bounded owned states, never SOP geometry."""
    keys = (
        "object_material_path",
        "object_material_overrides",
        "stylesheet",
        "stylesheet_errors",
        "stylesheet_resources",
    )
    base = {key: nominal[key] for key in keys}
    samples = []
    size = 0
    observed = set(base["stylesheet_resources"])
    for time in times:
        value = base if time == now else object_binding(obj, time, selected(rop, time))
        observed.update(value["stylesheet_resources"])
        size += len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        if size > 8 * 1024 * 1024:
            nominal["stylesheet_resources"] = sorted(observed)
            nominal["stylesheet_errors"].append(
                "Object material shutter samples exceed 8 MiB; retaining requested-frame bindings and independent geometry motion"
            )
            return []
        samples.append(dict(time=time, **value))
    if all({key: sample[key] for key in keys} == base for sample in samples):
        return []
    # Watch off-frame imports too, including sources absent at the requested
    # frame. A version stamp invalidates retained deformation on their repair.
    from .. import _native

    resources = sorted(set(base["stylesheet_resources"]).union(*(sample["stylesheet_resources"] for sample in samples)))
    nominal["stylesheet_resources"] = resources
    nominal["object_material_revision"] = (
        json.dumps(_native.stylesheet_source_signature(resources)) if resources else ""
    )
    return samples


def selected(rop, now):
    import hou

    pattern = rop_value(rop, "apply_stylesheets", now)
    # Native manager order is authoritative; its last selected sheet wins.
    return tuple(hou.styles.styles(pattern)) if pattern.strip() else ()


def menu(kwargs):
    import hou

    return [value for name in hou.styles.styles() for value in (name, name)]


def signature(rop, now, resources=()):
    from .. import _native

    names = selected(rop, now)
    sources = tuple(sorted(set(resources) | {"style:" + name for name in names}))
    return names, sources, tuple(_native.stylesheet_source_signature(sources)) if sources else ()


def object_styles(obj, now, stylesheets=()):
    from .. import _native

    def value(name):
        parm = obj.parm(name)
        return parm.evalAtTime(now) if parm else ""

    own = value("shop_materialstylesheet")
    alias = value("material_stylesheet")
    errors = []
    if "\0" in own:
        errors.append("shop_materialstylesheet contains NULs; omitting the object stylesheet")
        own = ""
    if "\0" in alias:
        errors.append("material_stylesheet contains NULs; omitting the alias stylesheet")
        alias = ""
    if not stylesheets and own in ("", "{}") and alias in ("", "{}"):
        return "", errors, []
    layers = [json.dumps({"importFiles": [name]}) for name in reversed(stylesheets)]
    if alias not in ("", "{}"):
        layers.append(alias)
    # The native object subject implements path/name, categories, bundles and
    # self targeting. Supplying the owned source explicitly also respects the
    # requested frame instead of implicitly evaluating it at the UI frame.
    result = _native.object_stylesheet(obj.path(), layers, own, value("categories"), now)
    return result["json"], errors + result["errors"], result["resources"]
