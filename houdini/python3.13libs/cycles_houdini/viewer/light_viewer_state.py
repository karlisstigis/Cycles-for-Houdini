# SPDX-License-Identifier: Apache-2.0
"""Native Houdini handles for the authored Cycles light parameters.

The pose and shape handles use Houdini's installed native types. This module
translates their values to Cycles units.
No native operator definition or implementation is copied into the asset.
"""

import math

import hou

from ..scene.light_properties import area_size


STATE_NAME = "cycles_light_handles"
TRANSFORM = "Cycles Object Transform"
SPOT = "Spot Light"
RECTANGLE = "Area Size"
ELLIPSE = "Area Diameter"
RADIUS = "Emitter Radius"
SHAPES = (RECTANGLE, ELLIPSE, RADIUS)
TRANSLATE = ("tx", "ty", "tz")
ROTATE = ("rx", "ry", "rz")
UNIT_AXES = tuple(hou.Vector3(axis) for axis in ((1, 0, 0), (0, 1, 0), (0, 0, 1)))


def _shape_frame(node):
    # Native OBJ lights remove inherited scale from their world basis. The
    # resize handles still get the pre-transform and parent context. Convert
    # the light's native pose into that context and compensate each resize
    # axis so a dimension in the handle stays a scene-unit dimension.
    context = node.preTransform() * node.parentAndSubnetTransform()
    try:
        local = node.worldTransform() * context.inverted()
        rotation = local.extractRotates()
    except hou.OperationFailed:
        return None
    linear = hou.Matrix3(hou.hmath.buildRotate(rotation) * context)
    axis_lengths = tuple((axis * linear).length() for axis in UNIT_AXES)
    if min(axis_lengths) <= 1e-9:
        return None
    return local.extractTranslates(), rotation, axis_lengths


def _inner_angle(outer, blend):
    cosine = math.cos(math.radians(outer) * 0.5)
    return math.degrees(2 * math.acos(max(-1, min(1, cosine + (1 - cosine) * blend))))


def build_guide(sop):
    """Draw the native light's SOP glyph, including its Cycles emitter outline."""
    node = sop.parent()
    geometry = sop.geometry()

    def curve(positions, closed=False):
        polygon = geometry.createPolygon(is_closed=closed)
        for position in positions:
            point = geometry.createPoint()
            point.setPosition(position)
            polygon.addVertex(point)

    def ellipse(rx, ry, depth=0, axes=(0, 1, 2)):
        positions = []
        for i in range(48):
            angle = math.tau * i / 48
            position = [0, 0, 0]
            position[axes[0]] = rx * math.cos(angle)
            position[axes[1]] = ry * math.sin(angle)
            position[axes[2]] = depth
            positions.append(position)
        curve(positions, closed=True)

    def direction(length, sign):
        tip = (0, 0, sign * length)
        base = sign * length * 0.8
        offset = length * 0.09
        curve([(0, 0, 0), tip])
        curve([(-offset, 0, base), tip, (offset, 0, base)])
        curve([(0, -offset, base), tip, (0, offset, base)])

    kind = node.evalParm("cycles_light_type")
    if kind == "grid":
        width, height = area_size(node)
        x, y = width * 0.5, height * 0.5
        if node.evalParm("cycles_light_area_shape") in ("disk", "ellipse"):
            ellipse(x, y)
        else:
            curve([(-x, -y, 0), (x, -y, 0), (x, y, 0), (-x, y, 0)], closed=True)
        length = max(0.25, min(4, max(width, height) * 0.65))
        single_sided = node.evalParm("singlesided")
        reverse = node.evalParm("reverse")
        if not single_sided or not reverse:
            direction(length, -1)
        if not single_sided or reverse:
            direction(length, 1)
    elif kind == "spot":
        outer = node.evalParm("cycles_light_spot_angle")
        inner = _inner_angle(outer, node.evalParm("cycles_light_spot_smooth"))
        distance = 2
        for angle in sorted({inner, outer}):
            half = math.radians(angle) * 0.5
            radius = distance * math.sin(half)
            depth = -distance * math.cos(half)
            ellipse(radius, radius, depth)
            for x, y in ((radius, 0), (0, radius), (-radius, 0), (0, -radius)):
                curve([(0, 0, 0), (x, y, depth)])
        direction(distance, -1)
    elif kind == "sun":
        ellipse(0.18, 0.18)
        direction(1, -1)
    else:
        for axes in ((0, 1, 2), (1, 2, 0), (2, 0, 1)):
            ellipse(0.16, 0.16, axes=axes)


class LightState:
    def __init__(self, state_name, scene_viewer):
        self.state_name = state_name
        self.scene_viewer = scene_viewer
        self.node = None
        self.handles = {}
        self.active = False
        self.undo_open = False
        self.refresh_pending = False
        self.signature = None
        self.guide = hou.GeometryDrawable(
            scene_viewer,
            hou.drawableGeometryType.Line,
            "Cycles light direction",
            params={"color1": (1, 0.85, 0.25, 1), "line_width": 2},
        )

    def _key(self):
        node = self.node
        return (
            tuple(node.worldTransform().asTuple()),
            tuple(node.parmTransform().asTuple()),
            node.evalParm("cycles_light_type"),
            node.evalParm("cycles_light_area_shape"),
            (node.evalParm("areasizex"), node.evalParm("areasizey")),
            node.evalParm("cycles_light_radius"),
            node.evalParm("cycles_light_spot_angle"),
            node.evalParm("cycles_light_spot_smooth"),
            node.evalParm("singlesided"),
            node.evalParm("reverse"),
        )

    def _refresh(self, force=False):
        self.refresh_pending = False
        if not self.active:
            return
        try:
            signature = self._key()
            if signature == self.signature and not force:
                return
            self.signature = signature
            kind = self.node.evalParm("cycles_light_type")
            valid = _shape_frame(self.node) is not None
            # Create the native light handle only for spots. Its hidden pose
            # controls otherwise remain pickable (or draw disabled overlays).
            if kind == "spot" and SPOT not in self.handles:
                self.scene_viewer.bindViewerHandle(
                    "light",
                    SPOT,
                    settings="parm_transform(1)",
                    cache_previous_parms=True,
                    handle_parms=[*TRANSLATE, *ROTATE, "coneangle", "conedelta"],
                )
                self.handles[SPOT] = hou.Handle(self.scene_viewer, SPOT)
            elif kind != "spot" and SPOT in self.handles:
                self.scene_viewer.unbindViewerHandle(SPOT)
                del self.handles[SPOT]
            area = kind == "grid"
            rounded = self.node.evalParm("cycles_light_area_shape") in ("disk", "ellipse")
            shown = {
                RECTANGLE: area and not rounded and valid,
                ELLIPSE: area and rounded and valid,
                RADIUS: kind in ("point", "spot") and valid,
                TRANSFORM: kind != "spot",
                SPOT: kind == "spot",
            }
            for name, handle in self.handles.items():
                # Inactive shapes need neither interaction nor updates. Keep
                # their input state consistent with their visibility.
                if shown[name]:
                    if not handle.isEnabled():
                        handle.enable()
                    handle.show(True)
                    handle.update()
                else:
                    if handle.isEnabled():
                        handle.disable()
                    handle.show(False)
            self._direction_geometry(kind)
            if not valid:
                prompt = (
                    "Shape handles need an invertible parent transform. Use the light parameters to edit the shape."
                )
            elif kind == "spot":
                prompt = "Drag the emitter to resize it. Use the native light menu (Z: cone, O: orientation) to edit and aim the spotlight."
            else:
                prompt = "Drag the light shape to resize it; use the native light handle to position and aim it."
            self.scene_viewer.setPromptMessage(prompt)
        except hou.ObjectWasDeleted:
            self.active = False
            self.guide.show(False)
            self._end_undo()

    def _direction_geometry(self, kind):
        if kind not in ("grid", "spot", "sun"):
            self.guide.show(False)
            return
        matrix = self.node.worldTransform()
        center = matrix.extractTranslates()
        linear = hou.Matrix3(matrix)
        u, v, w = (axis * linear for axis in UNIT_AXES)
        area = kind == "grid"
        extent = max(area_size(self.node)) if area else 1
        distance = max(0.25, min(4, extent * 0.65))
        direction = w * (1 if area and self.node.evalParm("singlesided") and self.node.evalParm("reverse") else -1)
        directions = (direction, -direction) if area and not self.node.evalParm("singlesided") else (direction,)
        geometry = hou.Geometry()
        for direction in directions:
            end = center + direction * distance
            base = end - direction * distance * 0.2
            for positions in (
                (center, end),
                (base + u * distance * 0.1, end, base - u * distance * 0.1),
                (base + v * distance * 0.1, end, base - v * distance * 0.1),
            ):
                poly = geometry.createPolygon(is_closed=False)
                for position in positions:
                    point = geometry.createPoint()
                    point.setPosition(position)
                    poly.addVertex(point)
        self.guide.setGeometry(geometry)
        self.guide.show(True)

    def onEnter(self, kwargs):
        self.node = kwargs["node"]
        self.active = True
        self.handles = {name: hou.Handle(self.scene_viewer, name) for name in (*SHAPES, TRANSFORM)}
        self._refresh(force=True)

    def onStateToHandle(self, kwargs):
        node = kwargs.get("node") or self.node
        if node is None:
            return
        name, parms = kwargs["handle"], kwargs["parms"]
        if name in SHAPES:
            frame = _shape_frame(node)
            if frame is None:
                parms["onoff"] = 0
                return
            position, rotation, axis_lengths = frame
            parms.update(zip(("centerx", "centery", "centerz"), position))
            parms.update(zip(ROTATE, rotation))
            # These are scale factors, not switches. Zero collapses all native
            # resize controls. Keep the installed handle defaults at one.
            parms.update(constant_scale=1, uniform_scale=1, orient=0, onoff=1)
            if name == RADIUS:
                diameter = node.evalParm("cycles_light_radius") * 2
                parms.update(zip(("sizex", "sizey", "sizez"), (diameter / length for length in axis_lengths)))
            else:
                width, height = area_size(node)
                parms.update(sizex=width / axis_lengths[0], sizey=height / axis_lengths[1], sizez=0)
        elif name == SPOT:
            # The installed light handle interprets pose in OBJ parameter
            # space when parm_transform(1) is set.
            matrix = node.parmTransform()
            parms.update(zip(TRANSLATE, matrix.extractTranslates()))
            parms.update(zip(ROTATE, matrix.extractRotates()))
            outer = node.evalParm("cycles_light_spot_angle")
            inner = _inner_angle(outer, node.evalParm("cycles_light_spot_smooth"))
            parms.update(
                sx=1,
                sy=1,
                sz=1,
                coneangle=inner,
                conedelta=max(0, (outer - inner) * 0.5),
                coneonoff="on",
                coneanglefactor=1,
                spotlightdist=3,
                focus=3,
                light_type="point",
                light_conefov=0,
            )

    def onHandleToState(self, kwargs):
        if not self.active:
            return
        node = kwargs.get("node") or self.node
        name, parms = kwargs["handle"], kwargs["parms"]
        modified = set(kwargs["mod_parms"])
        if name in SHAPES:
            frame = _shape_frame(node)
            if frame is None:
                return
            axis_lengths = frame[2]
        if name == SPOT:
            # One native light handle owns both pose and cone. A second,
            # cone-only light handle still exposes native pose controls and can
            # steal drags without changing any of its bound parameters.
            previous = kwargs["prev_parms"]
            if modified.intersection((*TRANSLATE, *ROTATE)):
                parameter_matrix = node.parmTransform()
                position = parameter_matrix.extractTranslates()
                if modified.intersection(ROTATE):
                    old_rotation = hou.hmath.buildRotate(tuple(previous[name] for name in ROTATE))
                    new_rotation = hou.hmath.buildRotate(tuple(parms[name] for name in ROTATE))
                    parameter_matrix *= old_rotation.inverted() * new_rotation
                for index, channel in enumerate(TRANSLATE):
                    parameter_matrix.setAt(3, index, position[index] + float(parms[channel]) - float(previous[channel]))
                node.setParmTransform(parameter_matrix)
        changes = {}
        if name in (RECTANGLE, ELLIPSE):
            axes = ("sizex", "sizey")
            uniform = node.evalParm("cycles_light_area_shape") in ("square", "disk")
            candidates = [
                (index, max(0.0001, abs(float(parms[axis])) * axis_lengths[index]))
                for index, axis in enumerate(axes)
                if axis in modified
            ]
            if uniform and candidates:
                old = node.evalParm("areasizex")
                changes["areasizex"] = max(candidates, key=lambda value: abs(value[1] - old))[1]
            elif not uniform:
                changes.update(("areasize" + "xy"[index], value) for index, value in candidates)
        elif name == RADIUS:
            candidates = [
                abs(float(parms[axis])) * 0.5 * axis_lengths[index]
                for index, axis in enumerate(("sizex", "sizey", "sizez"))
                if axis in modified
            ]
            if candidates:
                old = node.evalParm("cycles_light_radius")
                changes["cycles_light_radius"] = max(candidates, key=lambda value: abs(value - old))
        elif name == SPOT and modified & {"coneangle", "conedelta"}:
            inner = max(0, min(179.999, float(parms["coneangle"])))
            outer = max(0.001, min(179.999, inner + 2 * max(0, float(parms["conedelta"]))))
            inner = min(inner, outer)
            cosine = math.cos(math.radians(outer) * 0.5)
            blend = (math.cos(math.radians(inner) * 0.5) - cosine) / max(1e-12, 1 - cosine)
            changes.update(cycles_light_spot_angle=outer, cycles_light_spot_smooth=max(0, min(1, blend)))
        changes = {
            name: value
            for name, value in changes.items()
            if math.isfinite(value) and not math.isclose(node.evalParm(name), value, rel_tol=1e-7, abs_tol=1e-8)
        }
        if changes:
            node.setParms(changes)

    def onBeginHandleToState(self, kwargs):
        if not self.undo_open:
            self.scene_viewer.beginStateUndo("Edit Cycles light")
            self.undo_open = True

    def _end_undo(self):
        if self.undo_open:
            self.scene_viewer.endStateUndo()
            self.undo_open = False

    def onEndHandleToState(self, kwargs):
        self._end_undo()
        self._refresh(force=True)

    def onNodeChangeEvent(self, kwargs):
        if kwargs["event_type"] == hou.nodeEventType.BeingDeleted:
            self.onExit(kwargs)
        else:
            self._refresh()

    def onPlaybackChangeEvent(self, kwargs):
        self._refresh()

    def onDraw(self, kwargs):
        if not self.active:
            return
        try:
            # Parent/pre-transform changes need not change the light's channels.
            # Schedule handle synchronization outside the viewport draw callback.
            if not self.refresh_pending and self._key() != self.signature:
                self.refresh_pending = True
                hou.ui.postEventCallback(self._refresh)
            self.guide.draw(kwargs["draw_handle"])
        except hou.ObjectWasDeleted:
            self.onExit(kwargs)

    def onDrawInterrupt(self, kwargs):
        self.onDraw(kwargs)

    def onInterrupt(self, kwargs):
        self._end_undo()

    def onResume(self, kwargs):
        self._refresh(force=True)

    def onExit(self, kwargs):
        self.active = False
        self._end_undo()
        self.guide.show(False)
        self.node = None


def create_template():
    template = hou.ViewerStateTemplate(STATE_NAME, "Cycles Light", hou.objNodeTypeCategory())
    template.bindFactory(LightState)
    template.bindIcon("OBJ_light")
    # The general OBJ transform handle does not depend on light-specific pose
    # modes saved in a HIP file. Let Houdini own parent/pivot/channel semantics.
    bindings = [(prefix + axis, prefix + axis) for prefix in ("t", "r", "p") for axis in "xyz"]
    bindings += [("xOrd", "trs_order"), ("rOrd", "xyz_order")]
    template.bindHandleStatic("xform", TRANSFORM, bindings, settings="hidewhen('{ cycles_light_type == spot }')")
    # Bind only dimensions at creation so the native handles start in their
    # resize mode. Pose values are still supplied by onStateToHandle, while the
    # installed light transform handle owns movement and aiming.
    template.bindHandle("boundingrect", RECTANGLE, cache_previous_parms=True, handle_parms=["sizex", "sizey"])
    template.bindHandle("boundingrect", ELLIPSE, cache_previous_parms=True, handle_parms=["sizex", "sizey"])
    template.bindHandle("boundingbox", RADIUS, cache_previous_parms=True, handle_parms=["sizex", "sizey", "sizez"])
    template.bindNodeChangeEvent(
        [hou.nodeEventType.ParmTupleChanged, hou.nodeEventType.InputRewired, hou.nodeEventType.BeingDeleted]
    )
    template.bindPlaybackChangeEvent()
    return template
