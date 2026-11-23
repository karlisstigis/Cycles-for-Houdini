# SPDX-License-Identifier: Apache-2.0
"""Whole Color Ramp promotion using native ramps and channel references."""

import ast, json, posixpath, re
from .ramps import ColorRampControls, RampSourceError


def _copy_channel(destination, original, mapping):
    """Copy animation while rebasing literal channel/node references.

    Houdini copies expression text verbatim with setFromParm. Its relative
    references otherwise change meaning when a shader channel moves outward.
    """
    import hou

    def path(value):
        absolute = posixpath.normpath(value if value.startswith("/") else original.node().path() + "/" + value)
        target = mapping.get(absolute, absolute)
        if value.startswith("/") and absolute not in mapping:
            return value
        return posixpath.relpath(target, destination.node().path())

    destination.setFromParm(original)
    for key in destination.keyframes():
        try:
            expression = key.expression()
            language = key.expressionLanguage()
        except hou.OperationFailed:
            continue
        if language == hou.exprLanguage.Hscript:
            expression = re.sub(
                r"\b(ch(?:f|t|s|sraw|op)?|chramp)\s*\(\s*([\"\'])(.*?)\2",
                lambda m: m[1] + "(" + m[2] + path(m[3]) + m[2],
                expression,
            )
        elif language == hou.exprLanguage.Python:

            class References(ast.NodeTransformer):
                def visit_Call(self, call):
                    self.generic_visit(call)
                    if (
                        isinstance(call.func, ast.Attribute)
                        and isinstance(call.func.value, ast.Name)
                        and call.func.value.id == "hou"
                        and call.func.attr in ("parm", "parmTuple", "node")
                        and call.args
                        and isinstance(call.args[0], ast.Constant)
                        and isinstance(call.args[0].value, str)
                    ):
                        call.args[0] = ast.Constant(path(call.args[0].value))
                    return call

            expression = ast.unparse(References().visit(ast.parse(expression, mode="eval")))
        if expression != key.expression():
            key.setExpression(expression, language)
            destination.setKeyframe(key)


MENUS = ("_color_space", "_hue_interpolation", "_interpolation")


def _ramp_template(source, name, label):
    """A native ramp parameter template with the source ramp's defaults."""
    template = source.parmTemplate()
    template.setName(name)
    template.setLabel(label)
    return template


def _copy_ramp(destination, source, suffixes, mapping=None):
    """Copy a native ramp's stops with their animation, by stop identity."""
    import hou

    count = source.evalAsInt()
    destination.set(source.evalAsRamp())
    pairs = []
    for i in range(1, count + 1):
        for suffix in suffixes:
            target = destination.node().parmTuple(destination.name() + str(i) + suffix)
            original = source.node().parmTuple(source.name() + str(i) + suffix)
            if target is not None and original is not None:
                pairs.extend(zip(target, original))
    return pairs


def promote(builder, node, name, label):
    """Promote a whole Color Ramp: Color and Alpha ramps plus its global menus.

    Both are native Houdini ramps on the material, shown inline. The shader's
    ramps reference them by count, like a promoted Float Ramp.
    """
    import hou

    source = ColorRampControls(node)
    if source.legacy():
        raise ValueError("Convert the retired RGBA promotion first")
    # Copy from the canonical stops, which may already be promoted further in.
    color, alpha = source.storage()[0], source.alpha_storage()
    for parm in (color, alpha):
        if not 1 <= parm.evalAsInt() <= 32:
            raise RampSourceError(1, "Promote a ramp with 1..32 stops")
    names = [name, name + "_alpha", name + "_panel", *(name + suffix for suffix in MENUS)]
    names += [
        prefix + str(i) + suffix
        for prefix in (name, name + "_alpha")
        for i in range(1, 33)
        for suffix in ("pos", "c", "value", "interp", "cr", "cg", "cb")
    ]
    if any(builder.parm(n) is not None or builder.parmTuple(n) is not None for n in names):
        raise ValueError("Material ramp parameter already exists: " + name)
    old_group = builder.parmTemplateGroup()
    group = builder.parmTemplateGroup()
    menus = []
    for suffix in MENUS:
        template = node.parm("ramp" + suffix).parmTemplate()
        template.setName(name + suffix)
        for condition, text in template.conditionals().items():
            template.setConditional(
                condition,
                text.replace("ramp_color_space", name + "_color_space").replace(
                    "ramp_interpolation", name + "_interpolation"
                ),
            )
        menus.append(template)
    ramps = (_ramp_template(color, name, "Color"), _ramp_template(alpha, name + "_alpha", "Alpha"))
    panel = hou.FolderParmTemplate(
        name + "_panel", label or "Color Ramp", parm_templates=(*menus, *ramps), folder_type=hou.folderType.Collapsible
    )
    group.append(panel)
    with hou.undos.group("Promote Color Ramp"):
        try:
            builder.setParmTemplateGroup(group)
            pairs = [(builder.parm(name + suffix), node.parm("ramp" + suffix)) for suffix in MENUS]
            pairs += _copy_ramp(builder.parm(name), color, ("pos", "c", "interp"))
            pairs += _copy_ramp(builder.parm(name + "_alpha"), alpha, ("pos", "value", "interp"))
            mapping = {p.path(): d.path() for d, p in pairs}
            for destination, original in pairs:
                _copy_channel(destination, original, mapping)
            originals = [node.parm("ramp"), node.parm("alpha_ramp")] + [node.parm("ramp" + suffix) for suffix in MENUS]
            backup = [(p, tuple(p.keyframes()), p.evalAsInt() if i < 2 else p.eval()) for i, p in enumerate(originals)]
            try:
                for parm, target in ((originals[0], name), (originals[1], name + "_alpha")):
                    parm.setExpression(
                        "ch(" + json.dumps(parm.node().relativePathTo(builder) + "/" + target) + ")",
                        hou.exprLanguage.Hscript,
                    )
                for suffix in MENUS:
                    node.parm("ramp" + suffix).set(builder.parm(name + suffix), follow_parm_reference=False)
            except Exception:
                for i, (p, keys, value) in enumerate(backup):
                    p.deleteAllKeyframes()
                    if keys:
                        p.setKeyframes(keys)
                    elif i < 2:
                        p.setExpression(str(value), hou.exprLanguage.Hscript)
                    else:
                        p.set(value, follow_parm_reference=False)
                raise
        except Exception:
            builder.setParmTemplateGroup(old_group)
            raise
    return builder.parmTuple(name)


def convert_legacy(builder, name):
    """Convert a retired hidden RGBA promotion to inline Color/Alpha ramps.

    The bundle's stops become a native RGB Color ramp and a float Alpha ramp
    at the same positions (Linear Alpha bases where HSV/HSL ignored them),
    with their animation. Shaders referencing the bundle then reference both
    ramps. Renders are unchanged.
    """
    import hou

    bundle = builder.parm(name)
    template = bundle.parmTemplate() if bundle is not None else None
    if template is None or template.tags().get("cycles_rgba_ramp") != "1":
        raise ValueError("No retired RGBA Color Ramp promotion named " + name)
    count = bundle.evalAsInt()
    if builder.parm(name + "_alpha") is not None:
        raise ValueError("Material ramp parameter already exists: " + name + "_alpha")
    space = builder.parm(name + "_color_space")
    hue = space is not None and space.evalAsString() in ("HSV", "HSL")
    consumers = [
        p
        for p in bundle.parmsReferencingThis()
        if p.name() == "ramp" and p.node().type().name() == "cycles::color_ramp::1"
    ]
    saved = {}
    for i in range(1, count + 1):
        for suffix in ("pos", "interp", "cr", "cg", "cb", "ca"):
            p = builder.parm(name + str(i) + suffix)
            if p is None:
                continue
            keys = tuple(p.keyframes())
            saved[str(i) + suffix] = (
                keys,
                None
                if keys
                else (
                    p.unexpandedString()
                    if suffix == "interp" and p.parmTemplate().type() == hou.parmTemplateType.String
                    else p.eval()
                ),
            )

    def rewrite(text):
        # Channel references between bundle stops follow their new names.
        return re.sub(r"\b" + re.escape(name) + r"(\d+)ca\b", lambda m: name + "_alpha" + m[1] + "value", text)

    old_group = builder.parmTemplateGroup()
    group = builder.parmTemplateGroup()
    color = hou.RampParmTemplate(
        name, "Color", hou.rampParmType.Color, default_value=count, default_basis=hou.rampBasis.Linear
    )
    alpha = hou.RampParmTemplate(
        name + "_alpha", "Alpha", hou.rampParmType.Float, default_value=count, default_basis=hou.rampBasis.Linear
    )
    group.replace(name, color)
    group.insertAfter(name, alpha)
    if group.find(name + "_edit") is not None:
        group.remove(name + "_edit")
    with hou.undos.group("Convert Color Ramp to inline ramps"):
        try:
            builder.setParmTemplateGroup(group)
            keys = tuple(i / max(count - 1, 1) for i in range(count))
            builder.parm(name).set(hou.Ramp((hou.rampBasis.Linear,) * count, keys, ((0.0, 0.0, 0.0),) * count))
            builder.parm(name + "_alpha").set(hou.Ramp((hou.rampBasis.Linear,) * count, keys, (1.0,) * count))

            def apply(parm, key):
                if parm is None or key not in saved:
                    return
                keys, value = saved[key]
                parm.deleteAllKeyframes()
                if keys:
                    for k in keys:
                        try:
                            if k.isExpressionSet():
                                k.setExpression(rewrite(k.expression()), k.expressionLanguage())
                        except hou.OperationFailed:
                            pass
                    parm.setKeyframes(keys)
                elif isinstance(value, str):
                    parm.set(value)
                else:
                    parm.set(value)

            for i in range(1, count + 1):
                apply(builder.parm(f"{name}{i}pos"), f"{i}pos")
                apply(builder.parm(f"{name}{i}interp"), f"{i}interp")
                for c in "rgb":
                    apply(builder.parm(f"{name}{i}c{c}"), f"{i}c{c}")
                apply(builder.parm(f"{name}_alpha{i}pos"), f"{i}pos")
                apply(builder.parm(f"{name}_alpha{i}value"), f"{i}ca")
                if hue:
                    builder.parm(f"{name}_alpha{i}interp").set("linear")
                else:
                    apply(builder.parm(f"{name}_alpha{i}interp"), f"{i}interp")
            for parm in consumers:
                shader = parm.node()
                shader.parm("alpha_ramp").setExpression(
                    "ch(" + json.dumps(shader.relativePathTo(builder) + "/" + name + "_alpha") + ")",
                    hou.exprLanguage.Hscript,
                )
        except Exception:
            builder.setParmTemplateGroup(old_group)
            raise
    return builder.parm(name), builder.parm(name + "_alpha")


def convert_all_legacy(root=None):
    """Convert every retired RGBA promotion below root (default: the scene)."""
    import hou

    converted = []
    for node in (root or hou.node("/")).allSubChildren(recurse_in_locked_nodes=False):
        for parm in node.spareParms() if hasattr(node, "spareParms") else ():
            template = parm.parmTemplate()
            if template.type() == hou.parmTemplateType.Folder and template.tags().get("cycles_rgba_ramp") == "1":
                convert_legacy(node, parm.name())
                converted.append(parm.path())
    return converted
