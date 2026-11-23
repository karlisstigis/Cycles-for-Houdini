# SPDX-License-Identifier: Apache-2.0
"""Headless, by construction: EVERY parameter of a light node, for one light type.

Usage: hython houdini_ipr_light_parameters_smoke.py --device CPU --type <name> [--path renderview|panel]
types: point spot grid disk sphere distant sun environment portal texture shader (hlight::2.0)
       cl_point cl_spot cl_rect cl_square cl_disk cl_ellipse cl_sun cl_portal cl_texture cl_shader (cycles::light::1)

For each visible, enabled parameter of the light (folders, buttons, labels, ramps
skipped) the test sets a valid different value in a live session A and sends the
update the Render View would send (lights-only read if the light rule allows it,
else the complete read). A second session B on an identical ROP always takes the
complete read of the same scene. It requires (a) A's image equals B's image,
(b) the path is recorded (lights-only, or full read with its reason), (c) after a
lights-only update the checker (CYCLES_HOUDINI_NARROW_CHECK) finds no difference.
The value is then restored the same way. A new Houdini light parameter is covered
automatically. Scene built in memory, never saved. Prints one row per parameter.
"""

import json
import os
import sys
import tempfile
import time

os.environ["CYCLES_HOUDINI_NARROW_CHECK"] = "1"
import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene, registry
from cycles_houdini.core import device_selection
from cycles_houdini.scene import camera_properties, light_properties, object_properties

device = sys.argv[sys.argv.index("--device") + 1] if "--device" in sys.argv else "CPU"
kind = sys.argv[sys.argv.index("--type") + 1] if "--type" in sys.argv else "point"
entry_point = sys.argv[sys.argv.index("--path") + 1] if "--path" in sys.argv else "renderview"
variant = sys.argv[sys.argv.index("--variant") + 1] if "--variant" in sys.argv else "plain"  # plain|keyed|expression
blur = "--blur" in sys.argv
limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 100000
scratch = tempfile.mkdtemp(prefix="l2_light_parms_")


def texture(name, value):
    path = os.path.join(scratch, name).replace("\\", "/")
    spec = oiio.ImageSpec(16, 8, 3, oiio.FLOAT)
    output = oiio.ImageOutput.create(path)
    assert output.open(path, spec)
    pixels = np.broadcast_to(np.asarray(value, np.float32), (8, 16, 3)).copy()
    pixels[:, :8] *= 0.25
    assert output.write_image(pixels) and output.close()
    return path


TEXTURE_A = texture("light_a.exr", (0.9, 0.6, 0.3))
TEXTURE_B = texture("light_b.exr", (0.3, 0.6, 0.9))


def menu_pick(parm, *words):
    for item, label in zip(parm.parmTemplate().menuItems(), parm.parmTemplate().menuLabels()):
        if any(w in item.lower() or w in label.lower() for w in words):
            return item
    raise ValueError(words)


obj = hou.node("/obj")
cam = obj.createNode("cam", "parm_cam")
cam.setParms(dict(ty=2, tz=8, rx=-10, resx=48, resy=27))
camera_properties.install(cam)
for name, sop_type, y in (("parm_ground", "grid", 0), ("parm_ball", "sphere", 1)):
    geo = obj.createNode("geo", name)
    object_properties.install(geo)
    sop = geo.createNode(sop_type)
    if sop_type == "sphere":
        sop.parm("type").set(menu_pick(sop.parm("type"), "polygon"))
    sop.setRenderFlag(True)
    geo.parm("ty").set(y)
_first = ch.create_material(name="parm_material_a", shader="emission")
_first.node("surface_shader").parmTuple("color").set((0.9, 0.5, 0.2))
_other = ch.create_material(name="parm_material_b", shader="emission")
_other.node("surface_shader").parmTuple("color").set((0.2, 0.5, 0.9))
FIRST_MATERIAL, OTHER_MATERIAL = _first.path(), _other.path()
if kind.startswith("cl_"):
    # The Cycles Light asset (cycles::light::1), as in the owner's scenes.
    light = obj.createNode("cycles::light::1", "parm_light")
    base = kind[3:]
    light.parm("cycles_light_type").set(dict(point="point", spot="spot", sun="sun").get(base, "grid"))
    if base in ("rect", "square", "disk", "ellipse"):
        light.parm("cycles_light_area_shape").set(
            menu_pick(light.parm("cycles_light_area_shape"), {"rect": "rect"}.get(base, base))
        )
    light.setParms(dict(tx=-1.5, ty=4, tz=3, rx=-50))
    if base == "portal":
        (light.parm("cycles_light_is_portal") or light.parm("light_is_portal")).set(1)
    if base == "texture":
        (light.parm("cycles_light_texture") or light.parm("light_texture")).set(TEXTURE_A)
    if base == "shader":
        (light.parm("cycles_light_material") or light.parm("light_material")).set(FIRST_MATERIAL)
    fill = obj.createNode("hlight::2.0", "parm_fill")
    light_properties.install(fill)
    fill.parm("light_type").set(menu_pick(fill.parm("light_type"), "point"))
    fill.setParms(dict(tx=2, ty=3, tz=4, light_intensity=4.0))
elif kind == "environment":
    light = obj.createNode("envlight", "parm_light")
    light_properties.install(light)
    light.parm("env_map").set(TEXTURE_A)
else:
    light = obj.createNode("hlight::2.0", "parm_light")
    light_properties.install(light)
    token = dict(
        point="point",
        spot="point",
        grid="grid",
        disk="disk",
        sphere="sphere",
        distant="distant",
        sun="sun",
        portal="grid",
        texture="grid",
        shader="grid",
    )[kind]
    light.parm("light_type").set(menu_pick(light.parm("light_type"), token))
    light.setParms(dict(tx=-1.5, ty=4, tz=3, rx=-50, light_intensity=10.0))
    if kind == "spot":
        light.parm("coneenable").set(1)
    if kind == "portal":
        (light.parm("cycles_light_is_portal") or light.parm("light_is_portal")).set(1)
    if kind == "texture":
        (light.parm("cycles_light_texture") or light.parm("light_texture")).set(TEXTURE_A)
    if kind == "shader":
        (light.parm("cycles_light_material") or light.parm("light_material")).set(FIRST_MATERIAL)
    # A second light keeps the scene lit when this one is switched off.
    fill = obj.createNode("hlight::2.0", "parm_fill")
    light_properties.install(fill)
    fill.parm("light_type").set(menu_pick(fill.parm("light_type"), "point"))
    fill.setParms(dict(tx=2, ty=3, tz=4, light_intensity=4.0))
rops = []
for name in ("parm_rop_a", "parm_rop_b"):
    rop = hou.node("/out").createNode("cycles_ipr", name)
    rop.setParms(
        dict(
            camera=cam.path(),
            resx=48,
            resy=27,
            ipr_samples=1,
            ipr_adaptive_sampling=0,
            ipr_use_denoise=0,
            allowmotionblur=1 if blur else 0,
        )
    )
    device_selection.set_devices(rop, [device])
    rops.append(rop)
rop_a, rop_b = rops


def wait_complete(entry, generation, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        status = entry.session.status()
        frame = entry.session.frame_info()
        if status["state"] == "failed":
            raise RuntimeError(status["error"])
        if status["state"] == "complete" and frame and frame["complete"] and frame["generation"] >= generation:
            if not status.get("capture_pending"):
                return np.frombuffer(entry.session.frame()["planes"]["combined"]["pixels"], np.float32).copy()
        time.sleep(0.005)
    raise TimeoutError("render did not complete")


def full(entry, rop):
    now = hou.time()
    return entry.apply(native_scene.capture(rop, now), now, reuse_if_unchanged=True)


def render_view_update(entry):
    """soho_adapter._update in update mode (camera from the ROP, as the panel path)."""
    now = hou.time()
    if entry.change_serial == entry.applied_serial:
        return "no change notification", entry.session.status()["generation"]
    refusal, entry.observer.light_refusal = entry.observer.light_refusal, None
    motion = native_scene.motion_configuration(rop_a, now)
    narrow = entry.narrow_update(now, motion, native_scene.capture(rop_a, now, camera_only=True))
    if narrow is not None:
        update = narrow
        path = entry.narrow_reason
    else:
        update = native_scene.capture(rop_a, now)
        path = (
            entry.narrow_reason
            if entry.light_values_pending
            else "full read: " + refusal["reason"]
            if refusal
            else "full read: other notifications in the same edit (not light-only)"
        )
    generation = entry.apply(
        update, now, reuse_if_unchanged=True, change_serial=entry.change_serial if "geometry" not in update else None
    )
    return path, generation


def panel_update(entry):
    """RenderPanel.render's capture branch (headless: the panel widget needs Houdini's UI)."""
    now = hou.time()
    if entry.change_serial == entry.applied_serial:
        return "no change notification", entry.session.status()["generation"]
    refusal, entry.observer.light_refusal = entry.observer.light_refusal, None
    motion = native_scene.motion_configuration(rop_a, now)
    narrow = entry.narrow_update(now, motion, native_scene.capture(rop_a, at_time=now, camera_only=True))
    if narrow is not None:
        update, path = narrow, entry.narrow_reason
    else:
        update = native_scene.capture(rop_a, at_time=now, deleted_sources=entry.observer.deleted_sources)
        update["passes"] = list(dict.fromkeys(update["passes"] + ["position", "depth"]))
        path = (
            entry.narrow_reason
            if entry.light_values_pending
            else "full read: " + refusal["reason"]
            if refusal
            else "full read: other notifications in the same edit (not light-only)"
        )
    generation = entry.apply(
        update, now, reuse_if_unchanged=True, change_serial=entry.change_serial if narrow is not None else None
    )
    return path, generation


def step(entry_a, entry_b):
    path, generation_a = (panel_update if entry_point == "panel" else render_view_update)(entry_a)
    generation_b = full(entry_b, rop_b)
    image_a, image_b = wait_complete(entry_a, generation_a), wait_complete(entry_b, generation_b)
    check = entry_a.run_narrow_check() if not path.startswith(("full read", "no change")) else None
    return path, np.array_equal(image_a, image_b), float(np.abs(image_a - image_b).max()), check


def new_value(parm_tuple):
    template = parm_tuple.parmTemplate()
    kind_ = template.type()
    menu = getattr(template, "menuItems", lambda: ())()
    current = parm_tuple.eval()
    if kind_ == hou.parmTemplateType.Toggle:
        return [0 if current[0] else 1]
    if menu and kind_ in (hou.parmTemplateType.Menu, hou.parmTemplateType.Int, hou.parmTemplateType.String):
        tokens = list(menu)
        now = parm_tuple[0].evalAsString()
        index = tokens.index(now) if now in tokens else -1
        return [tokens[(index + 1) % len(tokens)]]
    if kind_ == hou.parmTemplateType.Menu:
        return None
    if kind_ in (hou.parmTemplateType.Int, hou.parmTemplateType.Float):
        lo, hi = template.minValue(), template.maxValue()
        span = (hi - lo) or 1.0
        values = []
        for value in current:
            step_ = max(1, round(0.2 * span)) if kind_ == hou.parmTemplateType.Int else 0.17 * span
            changed = value + step_
            if (template.maxIsStrict() and changed > hi) or (changed > hi and value - step_ >= lo):
                changed = value - step_
            if template.minIsStrict() and changed < lo:
                changed = lo
            values.append(int(changed) if kind_ == hou.parmTemplateType.Int else changed)
        return values
    if kind_ == hou.parmTemplateType.String:
        if template.stringType() == hou.stringParmType.FileReference:
            return [TEXTURE_B if current[0] != TEXTURE_B else TEXTURE_A]
        if template.stringType() == hou.stringParmType.NodeReference:
            if parm_tuple.name() == "cycles_light_material":
                return [OTHER_MATERIAL if current[0] != OTHER_MATERIAL else FIRST_MATERIAL]
            return ["/obj/parm_ball" if current[0] != "/obj/parm_ball" else ""]
        return [current[0] + "_l2" if current[0] else "parm_ball"]
    return None


SKIP_TYPES = {
    hou.parmTemplateType.Folder: "folder",
    hou.parmTemplateType.FolderSet: "folder",
    hou.parmTemplateType.Button: "button",
    hou.parmTemplateType.Label: "label",
    hou.parmTemplateType.Separator: "separator",
    hou.parmTemplateType.Ramp: "ramp",
    hou.parmTemplateType.Data: "data",
}


def numeric(parm_tuple):
    template = parm_tuple.parmTemplate()
    return (
        template.type() in (hou.parmTemplateType.Float, hou.parmTemplateType.Int)
        and not getattr(template, "menuItems", lambda: ())()
    )


animated = []
if variant in ("keyed", "expression"):
    # Every numeric parameter animated before the sessions start: two keys, or a
    # time expression. Edits then happen at the current frame (5).
    hou.setFrame(5)
    for parm_tuple in light.parmTuples():
        if not numeric(parm_tuple) or SKIP_TYPES.get(parm_tuple.parmTemplate().type()):
            continue
        if parm_tuple[0].isHidden() or parm_tuple[0].isDisabled() or parm_tuple.parmTemplate().isHidden():
            continue
        try:
            for parm in parm_tuple:
                value = parm.eval()
                if parm.keyframes():
                    continue
                if variant == "keyed":
                    template = parm_tuple.parmTemplate()
                    delta = 1 if isinstance(value, int) else 0.05 * max(abs(value), 1.0)
                    other = value + delta
                    if other > template.maxValue() and (template.maxIsStrict() or value - delta >= template.minValue()):
                        other = value - delta
                    for frame, v in ((1, value), (10, other)):
                        key = hou.Keyframe()
                        key.setFrame(frame)
                        key.setValue(v)
                        parm.setKeyframe(key)
                else:
                    parm.setExpression(
                        "%r + 0 * $F" % float(value) if not isinstance(value, int) else "%d + 0 * $F" % value
                    )
            animated.append(parm_tuple.name())
        except hou.Error:
            continue

entry_a = registry.for_rop(rop_a, native_scene.configuration(rop_a))
entry_b = registry.for_rop(rop_b, native_scene.configuration(rop_b))
wait_complete(entry_a, full(entry_a, rop_a))
wait_complete(entry_b, full(entry_b, rop_b))
rows, skipped = [], {}
for parm_tuple in light.parmTuples():
    if len(rows) >= limit:
        break
    template = parm_tuple.parmTemplate()
    reason = SKIP_TYPES.get(template.type())
    if reason is None and (template.isHidden() or parm_tuple[0].isHidden()):
        reason = "hidden for this type"
    if reason is None and parm_tuple[0].isDisabled():
        reason = "disabled for this type"
    if reason is None and parm_tuple.isMultiParmInstance():
        reason = None
    if reason:
        skipped.setdefault(reason, []).append(parm_tuple.name())
        continue
    value = new_value(parm_tuple)
    if value is None:
        skipped.setdefault("no valid alternative value", []).append(parm_tuple.name())
        continue
    original = parm_tuple.eval()
    expressions = None
    if variant == "expression" and parm_tuple.name() in animated:
        expressions = [parm.expression() for parm in parm_tuple]
    try:
        values = value if len(value) == len(parm_tuple) else value * len(parm_tuple)
        if expressions is not None:
            for parm, v in zip(parm_tuple, values):
                parm.setExpression("%r + 0 * $F" % float(v) if not isinstance(v, int) else "%d + 0 * $F" % v)
        else:
            parm_tuple.set(values)
    except hou.Error as error:
        skipped.setdefault("cannot set", []).append(parm_tuple.name())
        continue
    try:
        path, equal, difference, check = step(entry_a, entry_b)
        if expressions is not None:
            for parm, expression in zip(parm_tuple, expressions):
                parm.setExpression(expression)
        else:
            parm_tuple.set(original)
        back_path, back_equal, back_difference, back_check = step(entry_a, entry_b)
    except Exception as error:
        path, equal, difference, check = "error: " + str(error)[:120], False, None, None
        back_path, back_equal, back_check = "", False, None
        try:
            parm_tuple.set(original)
            entry_a, entry_b = (
                registry.for_rop(rop_a, native_scene.configuration(rop_a)),
                registry.for_rop(rop_b, native_scene.configuration(rop_b)),
            )
            wait_complete(entry_a, full(entry_a, rop_a))
            wait_complete(entry_b, full(entry_b, rop_b))
        except Exception:
            pass
    row = dict(
        entry_point=entry_point,
        variant=variant,
        blur=blur,
        animated=parm_tuple.name() in animated,
        type=kind,
        parm=parm_tuple.name(),
        path=path,
        equal=equal,
        max_difference=difference,
        checker=None if check is None else check["differences"],
        restore_path=back_path,
        restore_equal=back_equal,
        restore_checker=None if back_check is None else back_check["differences"],
    )
    rows.append(row)
    print("LIGHT_PARM " + json.dumps(row), flush=True)
registry.close_all()
failures = [r for r in rows if not r["equal"] or not r["restore_equal"] or r["checker"] or r["restore_checker"]]
summary = dict(
    variant=variant,
    blur=blur,
    animated=len(animated),
    entry_point=entry_point,
    type=kind,
    parameters=len(rows),
    lights_only=sum(1 for r in rows if not r["path"].startswith(("full read", "no change", "error"))),
    full_read=sorted({r["path"] for r in rows if r["path"].startswith("full read")}),
    no_notification=[r["parm"] for r in rows if r["path"].startswith("no change")],
    skipped={k: len(v) for k, v in skipped.items()},
    failures=failures,
)
print("LIGHT_PARMS_RESULT " + json.dumps(summary), flush=True)
print("LIGHT_PARMS_SKIPPED " + json.dumps(skipped), flush=True)
print("PASS" if not failures else "FAIL", flush=True)
hou.exit(0 if not failures else 1, suppress_save_prompt=True)
