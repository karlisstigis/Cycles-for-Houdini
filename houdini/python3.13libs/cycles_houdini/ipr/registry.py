# SPDX-License-Identifier: Apache-2.0
"""Host-thread session ownership shared by native IPR and dedicated panels."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque
from copy import deepcopy
import json
import os
import time
import atexit

import hou
from .. import Session, _native, synchronize_color_configuration
from .control_bridge import ControlBridge
from ..scene import native_scene


def _mouse_button_down():
    from PySide6 import QtCore, QtWidgets

    return QtWidgets.QApplication.mouseButtons() != QtCore.Qt.NoButton


def _pose_channels_driven(node, names):
    for name in names:
        for parm in node.parmTuple(name) or ():
            keys = parm.keyframes()
            # The native Light and Camera classes author a constant lock(1)
            # channel on scale. It has no external transform dependency.
            if keys and (len(keys) != 1 or parm.rawValue() != "lock(1)"):
                return True
    return False


@dataclass
class Entry:
    key: str
    config: tuple
    session: Session
    channel: str = ""
    socket_port: str = ""
    display_retry_at: float = 0.0
    display_retry_failures: int = 0
    display_reopened_at: float = 0.0
    helper_pid: int | None = None
    source_objects: set = field(default_factory=set)
    source_geometry: set = field(default_factory=set)
    source_lights: set = field(default_factory=set)
    last_soho: dict = field(default_factory=dict)
    last_values: dict = field(default_factory=dict)
    previous_color_notice: bool = False
    error: str = ""
    paused: bool = False
    final_paused: bool = False
    stop_serial: int = 0
    final_update_pending: bool = False
    views: int = 0
    crop_override: tuple | None = None
    crop_reference: tuple | None = None
    rop: object = None
    observer: object = None
    change_serial: int = 0
    applied_serial: int = 0
    requested_serial: int = 0
    changed_at: float = 0.0
    observed_time: float = 0.0
    color_notified: int = 0
    last_camera: object = None
    last_motion: object = None
    pixel_subdivision: bool = False
    camera_refinement_pending: bool = False
    tessellation_revision: int = 0
    camera_changed_at: float = 0.0
    camera_changed_with_button_down: bool = False
    camera_only_pending: bool = False
    pose_serial: int = 0
    pose_preview_serial: int = 0
    transform_only_pending: bool = False
    light_pose_sources: dict = field(default_factory=dict)
    light_pose_dependencies: dict = field(default_factory=dict)
    expression_light_poses: set = field(default_factory=set)
    light_pose_overrides: dict = field(default_factory=dict)
    pose_source_ids: set = field(default_factory=set)
    pose_sources_unknown: bool = False
    light_pose_capture_required: bool = False
    environment_pose_source: object = None
    light_values_pending: bool = False
    linking_values_pending: set = field(default_factory=set)
    material_values_pending: bool = False
    material_surface_rewired: bool = False
    material_edit_paths: set = field(default_factory=set)
    narrow_check: object = None
    light_basis: object = None
    cook_baseline: set = field(default_factory=set)
    narrow_reason: str = ""

    def set_crop_override(self, crop, reference_size=None):
        self.crop_reference = (tuple(crop), tuple(reference_size or self.config[1:3])) if crop is not None else None
        self.crop_override = crop
        self.resize_crop(self.config[1:3])

    def resize_crop(self, size):
        if self.crop_override is None:
            return
        # Keep the original selection as the reference, rather than repeatedly
        # rounding the last resized crop (which would drift during zooming).
        if self.crop_reference is None:
            self.crop_reference = (self.crop_override, self.config[1:3])
        (x, y, w, h), (rw, rh) = self.crop_reference
        width, height = size
        left = max(0, min(width - 1, x * width // rw))
        bottom = max(0, min(height - 1, y * height // rh))
        right = max(left + 1, min(width, ((x + w) * width + rw - 1) // rw))
        top = max(bottom + 1, min(height, ((y + h) * height + rh - 1) // rh))
        self.crop_override = (left, bottom, right - left, top - bottom)

    def changed(
        self,
        camera_only=False,
        object_pose=False,
        source_node=None,
        light_value=False,
        material_value=None,
        material_surface_rewired=False,
        linking_receiver=None,
    ):
        clean = self.change_serial == self.applied_serial
        if clean or linking_receiver is None:
            self.linking_values_pending.clear()
        if linking_receiver is not None and (clean or self.linking_values_pending):
            self.linking_values_pending.add(linking_receiver)
        # Local material edits keep the materials-only update until another scene domain changes.
        if material_value is not None and (clean or not self.material_values_pending):
            self.material_edit_paths = set()
            self.material_surface_rewired = False
        self.material_values_pending = material_value is not None and (clean or self.material_values_pending)
        self.material_surface_rewired |= material_surface_rewired
        if material_value is not None:
            self.material_edit_paths.add(material_value)
        # Edits on light nodes (SceneObserver light_value_callback) keep the
        # lights-only read only while nothing else is pending; any other edit revokes it.
        self.light_values_pending = bool(light_value) and (clean or self.light_values_pending)
        if object_pose:
            if clean or self.pose_serial <= self.pose_preview_serial:
                self.pose_source_ids.clear()
                self.pose_sources_unknown = False
            if source_node is None:
                self.pose_sources_unknown = True
            else:
                try:
                    self.pose_source_ids.add(source_node.sessionId())
                except hou.ObjectWasDeleted:
                    self.pose_sources_unknown = True
        self.camera_only_pending = bool(camera_only) and (clean or self.camera_only_pending)
        self.transform_only_pending = bool(camera_only or object_pose) and (clean or self.transform_only_pending)
        self.change_serial += 1
        if object_pose:
            self.pose_serial = self.change_serial
        self.changed_at = time.monotonic()

    def defer_source_capture(self):
        # Pose previews own no SOP/shader work. Keep their authoritative source
        # check pending until the gesture settles, rather than repeatedly
        # scanning the whole scene during a drag. A real source edit immediately
        # revokes this deferral, including one arriving between pose events.
        if self.pose_serial > self.applied_serial and self._pose_gesture_held():
            # A mouse button is held and pose previews are flowing (a handle drag): never start the
            # complete read on the main thread mid-drag, even when some other notification arrived
            # with it. The release decides (narrow rules or the complete read).
            # CYCLES_HOUDINI_DRAG_DEFER=0 restores the old rule (only pure transform gestures defer).
            return True
        if not self.transform_only_pending or self.pose_serial <= self.applied_serial:
            return False
        if self.last_motion is None or self.error or hou.time() != self.observed_time:
            return False
        if self.light_pose_capture_required:
            return False
        if self.environment_pose_source is not None:
            identity, pose = self.environment_pose_source
            node = hou.nodeBySessionId(identity)
            try:
                # Environment orientation is encoded in the world shader,
                # rather than an analytic light pose. Keep its existing live
                # source route until it has an owned world-mapping preview.
                if node is None or tuple(node.worldTransformAtTime(hou.time()).asTuple()) != pose:
                    return False
            except hou.Error:
                return False
        if time.monotonic() - self.changed_at < 0.15:
            return True
        return hou.isUIAvailable() and _mouse_button_down()

    def _pose_gesture_held(self):
        if os.environ.get("CYCLES_HOUDINI_DRAG_DEFER") == "0" or not hou.isUIAvailable():
            return False
        if self.error or self.last_motion is None or time.monotonic() - self.changed_at > 1.0:
            return False
        return _mouse_button_down()

    def light_value_capture(self, now, motion):
        """Lights-only read after edits on light nodes, or None for the complete read.

        narrow_reason and the trace say which path was taken and why.
        """
        if not self.light_values_pending:
            return None
        reason = (
            "renderer error"
            if self.error
            else "no accepted read"
            if self.last_motion is None or not self.rop
            else "time changed"
            if self.observed_time != now
            else "motion settings changed"
            if self.last_motion != motion
            else ""
        )
        if not reason and native_scene.session_identity(
            native_scene.configuration(self.rop)
        ) != native_scene.session_identity(self.config):
            reason = "renderer configuration changed"
        result = None
        if not reason:
            result, path = native_scene.light_capture(self.rop, now, motion, self.light_basis)
            reason = path if result is not None else path
        self.narrow_reason = reason if result is not None else "full read: " + reason
        trace("narrow_light", path=self.narrow_reason)
        if result is not None:
            result["_narrow"] = "lights"
        return result

    def material_value_capture(self, now, motion):
        """Materials-only update after value edits in material networks, or None for the complete read.

        The edited materials are resolved alone (WorkerSession.prepare_material_edit); every other material
        and all geometry stay as accepted. The complete read stays when the edit changes what the geometry
        carries (attributes, pointiness, true displacement, volume use), the material also shades a light,
        or the material is not on rendered geometry. narrow_reason and the trace say which path and why.
        """
        if not self.material_values_pending:
            return None
        reason = (
            "materials path switched off (CYCLES_HOUDINI_MATERIAL_PATH=0)"
            if os.environ.get("CYCLES_HOUDINI_MATERIAL_PATH") == "0"
            else "renderer error"
            if self.error
            else "no accepted read"
            if self.last_motion is None or not self.rop
            else "time changed"
            if self.observed_time != now
            else "motion settings changed"
            if self.last_motion != motion
            else ""
        )
        method = getattr(self.session, "prepare_material_edit", None)
        if not reason and method is None:
            reason = "session has no materials-only update"
        if not reason and native_scene.session_identity(
            native_scene.configuration(self.rop)
        ) != native_scene.session_identity(self.config):
            reason = "renderer configuration changed"
        if not reason and self.light_basis is not None:
            shared = set(self.light_basis.get("light_materials") or ()) & self.material_edit_paths
            if shared:
                reason = "the material also shades a light: " + sorted(shared)[0]
        result = None
        if not reason:
            configuration = native_scene.material_configuration(self.rop, now, motion)
            try:
                ok, why, _keys = method(sorted(self.material_edit_paths), configuration["material_resolver"], now)
            except Exception as error:  # Resolution errors belong to the complete read's diagnostics.
                ok, why = False, "material read failed: " + str(error)[:120]
            if ok:
                result = dict(
                    configuration,
                    material_edit=True,
                    _narrow="materials",
                    _refresh_observer=self.material_surface_rewired,
                )
            else:
                reason = why
        self.narrow_reason = "materials" if result is not None else "full read: " + reason
        trace("narrow_material", path=self.narrow_reason)
        return result

    def narrow_update(self, now, motion, camera_update):
        """The narrow update for the pending edits, or None for the complete read.

        The single entry point for both live-render paths (soho_adapter._update for
        Houdini's Render View, RenderPanel.render for the Cycles Render panel):
        camera_update is the caller's camera part; the rules live here.
        """
        self.narrow_reason = ""
        if self.change_serial > self.applied_serial and self.linking_values_pending:
            serial = self.change_serial
            linking_update = None
            if (
                self.rop
                and not self.error
                and self.observed_time == now
                and self.last_motion == motion
                and native_scene.session_identity(native_scene.configuration(self.rop))
                == native_scene.session_identity(self.config)
            ):
                linking_update = native_scene.receiver_linking_capture(
                    self.rop, now, motion, self.light_basis, self.linking_values_pending
                )
            if self.change_serial != serial:
                linking_update = None  # Evaluating a selector may deliver a new source edit.
            self.narrow_reason = (
                "linking" if linking_update is not None else "full read: receiver linking inputs changed"
            )
            trace("narrow_linking", path=self.narrow_reason)
            return dict(camera_update, **linking_update, _narrow="linking") if linking_update is not None else None
        lights = self.light_value_capture(now, motion)
        if lights is not None:
            return dict(camera_update, **lights)
        materials = self.material_value_capture(now, motion)
        if materials is not None:
            return dict(camera_update, **materials)
        if self.material_values_pending:
            return None  # narrow_reason says why the complete read is needed.
        if self.pose_commit(now, motion):
            return dict(camera_update, _narrow="poses")
        if not self.narrow_reason:
            self.narrow_reason = "full read: the pending edits are not light-only or transform-only"
        return None

    def pose_commit(self, now, motion):
        """After a transform-only OBJ edit: accept the pose previews as the scene.

        Allowed when the moved OBJs' affected set is their own transforms and those
        of their OBJ descendants (and lights/cameras below them, which the preview
        already evaluates): no node outside a moved subtree references a moved OBJ
        or a descendant (object merges, optransform/origin expressions, constraints,
        instancers), none is an instancing prototype. Animation is allowed (time).
        Otherwise the complete read stays. The reason is traced (narrow_reason).
        """
        if not (self.transform_only_pending and self.pose_serial > self.applied_serial) or self.camera_only_pending:
            return False
        reason = ""
        if os.environ.get("CYCLES_HOUDINI_POSE_COMMIT") == "0":
            reason = "pose commit switched off (CYCLES_HOUDINI_POSE_COMMIT=0)"
        elif self.pose_sources_unknown or not self.pose_source_ids:
            reason = "moved objects unknown"
        elif self.error or self.observed_time != now or self.last_motion != motion or not self.rop:
            reason = "time, motion settings or renderer state changed"
        else:
            prototypes = getattr(self.observer, "instancing_prototypes", ()) if self.observer else ()
            from ..scene import light_properties, linking, object_scene

            try:
                rendered = set(object_scene.selected(self.rop, now))
            except (ValueError, hou.Error):
                rendered = None
                reason = "object selection unavailable"
            for identity in self.pose_source_ids if rendered is not None else ():
                node = hou.nodeBySessionId(identity)
                if node is None:
                    reason = "a moved object was deleted"
                    break
                subtree = [node]
                index = 0
                while index < len(subtree):
                    subtree.extend(
                        child
                        for child in subtree[index].outputs()
                        if child.type().category() == hou.objNodeTypeCategory() and child not in subtree
                    )
                    index += 1
                for member in subtree:
                    if member in prototypes:
                        reason = "an instancing prototype moved: " + member.path()
                        break
                    # Any reference, also from SOPs inside the moved objects (an Object
                    # Merge "into this object", origin(".") expressions): their geometry
                    # depends on the transform. Only OBJ descendants (parenting) follow.
                    # Nodes inside a non-renderable object, such as a Null's control
                    # glyph or a light's guide geometry, do not reach the render.
                    inside = member.path().rstrip("/") + "/"
                    direct = set(member.dependents(include_children=False))
                    dependents = (
                        member.dependents(include_children=True) if light_properties.supported(member) else direct
                    )
                    outside = [
                        dependent.path()
                        for dependent in dependents
                        if dependent not in subtree
                        and not (member not in rendered and dependent.path().startswith(inside))
                        and not (dependent in direct and linking.references_only_by_linking(dependent, member))
                    ]
                    if outside:
                        reason = "referenced by " + outside[0]
                        break
                if reason:
                    break
            # Guard independent of dependents(): a dependency Houdini does not report
            # (VEX optransform(), Python, indirect paths) still dirties the SOPs that
            # read the moved transform. Any rendered object whose render SOP now needs
            # to cook takes the complete read.
            for candidate in sorted(rendered or (), key=lambda node: node.path()) if not reason else ():
                try:
                    sop = candidate.renderNode()
                    if sop is not None and sop.sessionId() not in self.cook_baseline and sop.needsToCook():
                        reason = "geometry of " + candidate.path() + " needs to cook after the move"
                        break
                except hou.Error:
                    reason = "geometry state of " + candidate.path() + " unavailable"
                    break
        if not reason:
            if self.pose_serial > self.pose_preview_serial:
                self.preview_object_poses()
            if self.pose_serial > self.pose_preview_serial:
                reason = "pose preview not available"
            elif not self.session.commit_pose_previews():
                reason = "source work outstanding"
        self.narrow_reason = "poses" if not reason else "full read: " + reason
        trace("narrow_poses", path=self.narrow_reason)
        return not reason

    def pose_preview_host_safe(self):
        """True when a pose preview may run from a Qt timer instead of Houdini's idle callback.

        Houdini can withhold idle callbacks during a viewport drag. Pose
        acquisition evaluates OBJ transforms only (no SOP cooks); it is safe
        outside the idle callback when no pose source or OBJ ancestor carries
        animation, expressions or constraints (those may cook CHOPs).
        """
        if self.pose_sources_unknown or not self.pose_source_ids or self.expression_light_poses:
            return False
        names = ("t", "r", "s", "p", "pr", "scale", "pre_xform")
        for identity in self.pose_source_ids:
            node = hou.nodeBySessionId(identity)
            if node is None:
                return False
            try:
                for source in (node, *node.inputAncestors()):
                    if source.type().category() != hou.objNodeTypeCategory():
                        continue
                    if source.parm("constraints_on") is not None and source.evalParm("constraints_on"):
                        return False
                    if _pose_channels_driven(source, names):
                        return False
            except hou.Error:
                return False
        return True

    def run_narrow_check(self):
        """Debug checker (CYCLES_HOUDINI_NARROW_CHECK=1): compare the last narrow update with a complete read.

        Runs from an idle host poll, never between edits, and never applies its
        result: it records every top-level value of the complete read that differs
        from the accepted scene plus what the narrow update sent (lights per light
        and field; geometry by membership), so a dependency the narrow path missed
        becomes visible.
        """
        pending, self.narrow_check = self.narrow_check, None
        if pending is None or not self.rop or self.change_serial != self.applied_serial:
            return None
        kind, now, expected = pending
        if now != hou.time():
            return None
        full = native_scene.capture(self.rop, now)
        differences = []
        # material_resolver is a per-read lookup object (never equal between reads);
        # the materials it resolves are compared through "materials".
        skip = ("geometry", "source_diagnostics", "_blender_prototypes", "crop", "material_resolver")
        for name in sorted(set(full) | set(expected)):
            if name in skip or name.startswith("deleted_") or name.startswith("_"):
                continue
            a, b = expected.get(name), full.get(name)
            if name == "lights" and a is not None and b is not None:
                sent = {light["key"]: light for light in a}
                read = {light["key"]: light for light in b}
                for key in sorted(set(sent) | set(read)):
                    x, y = sent.get(key), read.get(key)
                    if x is None or y is None:
                        differences.append(dict(resource=key, field="membership", node=(x or y).get("source_path", "")))
                        continue
                    for field in sorted(set(x) | set(y)):
                        if x.get(field) != y.get(field):
                            differences.append(dict(resource=key, field=field, node=y.get("source_path", "")))
            elif name == "passes" and a is not None and b is not None:
                # The Cycles Render panel adds its pick planes to every complete read.
                pick = {"position", "depth"}
                if [x for x in a if x not in pick] != [x for x in b if x not in pick]:
                    differences.append(dict(resource=name, field=name, node=self.rop.path()))
            elif name in full and a != b:
                differences.append(dict(resource=name, field=name, node=self.rop.path()))
        if full.get("geometry") is not None and expected.get("geometry") is not None:
            keys = lambda requests: sorted((r["object_key"], r["geometry_key"]) for r in requests)
            if keys(full["geometry"]) != keys(expected["geometry"]):
                differences.append(dict(resource="geometry", field="membership", node=self.rop.path()))
        record = dict(kind=kind, differences=differences, time=time.monotonic())
        narrow_check_log.append(record)
        del narrow_check_log[:-64]
        trace("narrow_check", kind=kind, differences=differences)
        if differences:
            import sys

            print("Cycles narrow update check (" + kind + "): " + json.dumps(differences), file=sys.stderr, flush=True)
        return record

    def can_capture_camera_only(self, motion, now):
        # Camera transforms/intrinsics do not alter geometry samples. Shutter,
        # FPS, offset and sample-count edits do, even if only the camera emitted
        # a host notification. Keep those captures on Houdini's safe cook path.
        # The initial authoritative capture legitimately has serial zero.
        # Its retained motion contract, rather than a positive edit counter,
        # establishes that a camera-only follow-up can preserve the scene.
        return bool(
            self.camera_only_pending
            and self.last_motion is not None
            and not self.error
            and self.observed_time == now
            and self.last_motion == motion
        )

    def preview_object_poses(self):
        serial = self.pose_serial
        if (
            serial <= max(self.applied_serial, self.pose_preview_serial)
            or self.error
            or self.last_motion is None
            or not self.rop
        ):
            return None
        method = getattr(self.session, "preview_object_poses", None)
        if method is None:
            return None
        now = hou.time()
        if now != self.observed_time or native_scene.motion_configuration(self.rop, now) != self.last_motion:
            return None
        # Every ROP edit revokes transform-only classification. Avoid copying
        # and walking the entire device/parameter layout for every mouse move.
        if not self.transform_only_pending and native_scene.session_identity(
            native_scene.configuration(self.rop)
        ) != native_scene.session_identity(self.config):
            return None
        header = native_scene.capture(self.rop, now, camera_only=True)
        # Source identity is preserved through observer coalescing. An unrelated
        # mesh transform does no analytic-light evaluation. Unknown callers and
        # expression-driven transforms conservatively retain the broad route.
        keys = set(self.light_pose_sources) if self.pose_sources_unknown else set(self.expression_light_poses)
        if not self.pose_sources_unknown:
            for identity in self.pose_source_ids:
                keys.update(self.light_pose_dependencies.get(identity, ()))
        for key in keys:
            identity, path, local = self.light_pose_sources[key]
            node = hou.nodeBySessionId(identity)
            if node is None or node.path() != path:
                self.light_pose_overrides.pop(key, None)
                continue
            try:
                pose = native_scene.matrix_from_houdini(local * node.worldTransformAtTime(now))
            except (hou.Error, ValueError):
                continue  # Authoritative capture supplies recovery diagnostics.
            self.light_pose_overrides[key] = dict(key=key, source_path=path, transform=pose)
        # Navigation maps replace their previous value. Preserve previously
        # moved lights until the authoritative capture accepts the entire scene.
        header["light_poses"] = list(self.light_pose_overrides.values())
        result = self.apply(header, now, pose_preview=True)
        self.pose_preview_serial = serial
        return result

    def gesture_pose_update(self):
        """Houdini's own IPR update during a transform-only gesture: pose preview only.

        The Render View re-runs SOHO for every changed object, including each
        step of a light or object drag. A complete capture there costs Houdini's
        main thread a full scene walk per step and, for lights, the image waits
        for the whole capture pipeline. Our host poll already defers that
        capture until the gesture settles (defer_source_capture) and shows the
        poses through the navigation preview; follow the same rule here.
        Returns the generation to report, or None for the ordinary capture.
        """
        if not self.channel or local_final_active() or not self.defer_source_capture():
            return None
        self.preview_object_poses()
        return self.session.status()["generation"]

    def apply(self, update, now, *, reuse_if_unchanged=False, change_serial=None, pose_preview=False):
        if local_final_active():
            self.final_update_pending = True
            self.requested_serial = 0
            self.changed()
            return self.session.status()["generation"]
        self.final_update_pending = False
        if self.crop_override is not None:
            update["crop"] = self.crop_override
        if "geometry" in update:
            from ..scene import instancing

            self.pixel_subdivision = any(
                request.get("subdivision", {}).get("type", "none") != "none"
                and request.get("subdivision", {}).get("adaptive", True)
                and request.get("subdivision", {}).get("space", "pixel") == "pixel"
                for request in instancing.nested_requests(update["geometry"])
            ) and not bool(update.get("dicing", {}).get("camera_path") or update.get("dicing", {}).get("camera"))
        camera = (
            native_scene.camera_signature(update, now)
            if ("camera" in update or "camera_path" in update)
            else self.last_camera
        )
        frozen = bool(
            self.rop and self.rop.parm("tessellation_update") and not self.rop.evalParm("tessellation_update")
        )
        if frozen:
            self.camera_refinement_pending = False
        elif (
            hou.isUIAvailable()
            and self.pixel_subdivision
            and self.last_camera is not None
            and camera != self.last_camera
        ):
            self.camera_refinement_pending = True
            self.camera_changed_at = time.monotonic()
            self.camera_changed_with_button_down = _mouse_button_down()
        elif not hou.isUIAvailable() or not self.pixel_subdivision:
            self.camera_refinement_pending = False
        # This entry belongs to an interactive viewer. Final/batch captures do
        # not use this policy. Trace with the current camera and the last complete
        # dicing camera until the drag is released or the camera stops moving.
        update["defer_camera_subdivision"] = self.camera_refinement_pending
        if pose_preview:
            return self.session.preview_object_poses(update, at_time=now)
        if "world" in update:
            path = update["world"].get("source_path", "")
            node = hou.node(path) if path else None
            self.environment_pose_source = (
                (node.sessionId(), tuple(node.worldTransformAtTime(now).asTuple()))
                if node is not None and native_scene.world.is_environment(node)
                else None
            )
        objects = {r["object_key"] for r in update.get("geometry", [])}
        geometry = {r["geometry_key"] for r in update.get("geometry", [])}
        lights = {r["key"] for r in update.get("lights", [])}
        if "lights" in update:
            update["deleted_lights"] = sorted(self.source_lights - lights)
            poses = {}
            dependencies = {}
            expressions = set()
            capture_required = False
            ancestry_cache = {}
            expression_cache = {}

            def ancestry(source):
                identity = source.sessionId()
                if identity not in ancestry_cache:
                    ancestry_cache[identity] = {source, *source.inputAncestors()}
                return ancestry_cache[identity]

            def expression_driven(source):
                identity = source.sessionId()
                if identity not in expression_cache:
                    # Shared rig ancestors and multiple emitted sides are
                    # inspected once per authoritative capture.
                    expression_cache[identity] = (
                        _pose_channels_driven(source, ("t", "r", "s", "p", "pr", "scale", "xOrd", "rOrd", "pre_xform"))
                        or any(native_scene.light_pose_target_paths(source, now))
                        or bool(native_scene.object_scene.value(source, "constraints_on", now, False))
                    )
                return expression_cache[identity]

            for light in update["lights"]:
                path = light.get("source_path", "")
                node = hou.node(path) if path else None
                if node is None or node.type().category() != hou.objNodeTypeCategory():
                    # ROP-owned fallback lights are static. Other unsupported
                    # sources cannot safely defer their authoritative adapter.
                    if path and node != self.rop:
                        capture_required = True
                    continue
                try:
                    # Retain the adapter's local orientation (including reversed
                    # and two-sided areas), never reinterpret light/shader data
                    # during a transform preview.
                    m = light["transform"]
                    emitted = hou.Matrix4(
                        (m[0], m[4], m[8], 0, m[1], m[5], m[9], 0, m[2], m[6], m[10], 0, m[3], m[7], m[11], 1)
                    )
                    local = emitted * node.worldTransformAtTime(now).inverted()
                    poses[light["key"]] = (node.sessionId(), path, local)
                except hou.Error:
                    capture_required = True
                    continue
                try:
                    ancestors = set(ancestry(node))
                    parent = node.parent()
                    while parent is not None and parent.type().category() == hou.objNodeTypeCategory():
                        ancestors.update(ancestry(parent))
                        parent = parent.parent()
                    for ancestor in ancestors:
                        dependencies.setdefault(ancestor.sessionId(), set()).add(light["key"])
                        # Channel expressions, constraints, Look At, and Follow
                        # Path can all move a light independently of its inputs.
                        if expression_driven(ancestor):
                            expressions.add(light["key"])
                except hou.Error:
                    # A partial dependency index must never narrow this light.
                    expressions.add(light["key"])
            self.light_pose_sources = poses
            self.light_pose_dependencies = dependencies
            self.expression_light_poses = expressions
            self.light_pose_capture_required = capture_required
            self.light_pose_overrides.clear()
        # The HDK adapter owns the complete native resource ledger, including
        # packed children and shared prototypes absent from this request list.
        narrow = update.pop("_narrow", None)
        refresh_observer = update.pop("_refresh_observer", False)
        next_basis = update.pop("_basis", None)
        # A complete read is the basis later lights-only reads compare against.
        basis_update = dict(update) if "geometry" in update else None
        sent = {name: value for name, value in update.items() if not name.startswith("deleted_")} if narrow else None
        values = {}
        for name in (
            "materials",
            "lights",
            "background",
            "denoising",
            "transparent_background",
            "transparent_glass",
            "transparent_roughness_threshold",
        ):
            if name in update:
                values[name] = deepcopy(update[name])
                if self.last_values.get(name) == values[name]:
                    update.pop(name)
        try:
            generation = self.session.apply(update, at_time=now, reuse_if_unchanged=reuse_if_unchanged)
        except Exception as error:
            self.fail(error)
            raise
        self.last_values.update(values)
        self.last_camera = deepcopy(camera)
        if "geometry" in update:
            self.last_motion = deepcopy(update.get("motion"))
        if "lights" in values:
            self.source_lights = lights
        if "geometry" in update or change_serial is not None:
            if "geometry" in update:
                self.source_objects, self.source_geometry = objects, geometry
            self.applied_serial = self.change_serial if change_serial is None else change_serial
            self.requested_serial = 0
            self.observed_time = now
        self.error = ""
        if basis_update is not None:
            self.light_basis = native_scene.light_basis(self.rop, now, basis_update) if self.rop else None
            # Render SOPs that still report needsToCook() right after a complete read
            # (cooked at another shutter time, always-dirty nodes) are not evidence
            # of a move for pose_commit's guard.
            self.cook_baseline = set()
            for path in {request.get("object_path", "") for request in basis_update.get("geometry", ())}:
                node = hou.node(path) if path else None
                try:
                    sop = node.renderNode() if node is not None else None
                    if sop is not None and sop.needsToCook():
                        self.cook_baseline.add(sop.sessionId())
                except hou.Error:
                    pass
        elif narrow and narrow != "materials" and self.light_basis is not None:
            merged = dict(self.light_basis["update"], **sent)
            self.light_basis = (
                dict(next_basis, update=merged)
                if next_basis is not None
                else native_scene.light_basis(self.rop, now, merged)
            )
            if self.light_basis is not None and os.environ.get("CYCLES_HOUDINI_NARROW_CHECK") == "1":
                self.narrow_check = (narrow, now, dict(self.light_basis["update"]))
        # Recheck observer dependencies after a root Surface rewire, which can activate other resources.
        if self.observer and (
            "geometry" in update or ("materials" in values and narrow != "materials") or refresh_observer
        ):
            self.observer.refresh()
        elif self.observer and narrow == "linking" and self.change_serial == self.applied_serial:
            self.observer.remember_parameters(hou.nodeBySessionId(identity) for identity in self.linking_values_pending)
        return generation

    def fail(self, error):
        # Keep the last image and diagnostic. Explicit pre-transaction rejects
        # retain the accepted render; other host failures stop the obsolete
        # scene. A new edit permits another update.
        self.error = "Scene update failed: " + str(error)
        self.requested_serial = self.change_serial
        if self.session.status()["state"] != "closed" and not isinstance(error, _native.SceneUpdateRejected):
            self.stop_serial += 1
            self.final_update_pending = False
            self.session.cancel()
        if self.observer:
            self.observer.refresh()


from .ipr_suspend import LocalFinalSuspension

_local_finals = LocalFinalSuspension()
_local_final_jobs = {}


def local_final_active():
    return _local_finals.active


def begin_local_final():
    token = _local_finals.begin(entries())
    try:
        ensure_polling()
    except BaseException:
        _local_finals.end(token)
        raise
    return token


def end_local_final(token):
    _local_final_jobs.pop(token, None)
    return _local_finals.end(token)


def track_local_final(token, job):
    _local_final_jobs[token] = job


def abandon_local_final(token):
    """Rollback a viewer launch without releasing a still-running child job."""
    job = _local_final_jobs.get(token)
    if job is not None:
        job.stop()
    else:
        end_local_final(token)


def _poll_local_finals():
    from ..rop.background_jobs import TERMINAL

    for token, job in tuple(_local_final_jobs.items()):
        if job.status()["state"] in TERMINAL:
            end_local_final(token)


_entries: dict[str, Entry] = {}
_bridge: ControlBridge | None = None
_polling = False
_in_poll = False
_last_display = 0.0
_last_texture_poll = 0.0
_registered_shutdown = False
_retire_requested = set()
debug_events = deque(maxlen=64)


narrow_check_log = []


def trace(event, **fields):
    debug_events.append(dict(event=event, time=time.monotonic(), **fields))


def entries():
    return tuple(_entries.values())


def get(key):
    return _entries.get(key)


def session_from_configuration(config, **kwargs):
    device, width, height, samples, *cache = config
    if len(cache) not in (0, 2, 3, 5, 6, 7):
        raise ValueError("Unsupported native renderer configuration")
    # A foreground ROP owns frame order and progress. Its renderer belongs
    # in a child process. Farm jobs already run in a dedicated headless host.
    kwargs["execution"] = "worker" if kwargs.get("interactive", True) or hou.isUIAvailable() else "sync"
    return Session(
        device=device,
        width=width,
        height=height,
        samples=samples,
        use_texture_cache=bool(cache[0]) if cache else True,
        auto_texture_cache=bool(cache[1]) if cache else False,
        threads=int(cache[2]) if len(cache) >= 3 else 0,
        use_tiling=bool(cache[3]) if len(cache) >= 5 else False,
        tile_size=int(cache[4]) if len(cache) >= 5 else 2048,
        hair_subdivisions=int(cache[5]) if len(cache) >= 6 else 2,
        refit_meshes=bool(cache[6]) if len(cache) >= 7 and kwargs.get("interactive", True) else False,
        **kwargs,
    )


def for_rop(rop, config):
    key = f"rop:{rop.sessionId()}"
    entry = _entries.get(key)
    status = entry.session.status() if entry else None
    unusable = status and (status["state"] == "closed" or status.get("capture_restart_required", False))
    if entry and (native_scene.session_identity(entry.config) != native_scene.session_identity(config) or unusable):
        entry.session.close()
        replacement = session_from_configuration(config)
        try:
            if entry.socket_port:
                replacement.open_native_display("ip", entry.socket_port)
            if entry.paused:
                replacement.pause(True)
        except BaseException:
            replacement.close()
            raise
        # The persistent SOHO control pipe belongs to this host entry, not to a
        # particular Cycles backend. Preserve it while replacing the engine.
        entry.session = replacement
        entry.display_retry_at = 0.0
        entry.display_retry_failures = 0
        entry.display_reopened_at = 0.0
        if entry.config[1:3] != config[1:3]:
            entry.resize_crop(config[1:3])
        entry.config = config
        entry.source_objects.clear()
        entry.source_geometry.clear()
        entry.source_lights.clear()
        entry.last_values.clear()
        entry.last_soho.clear()
        entry.error = ""
        entry.color_notified = 0
        entry.last_camera = entry.last_motion = None
        entry.pixel_subdivision = False
        entry.camera_refinement_pending = False
        entry.camera_changed_with_button_down = False
        entry.tessellation_revision = 0
        entry.camera_only_pending = False
        entry.pose_serial = entry.pose_preview_serial = 0
        entry.transform_only_pending = False
        entry.light_pose_sources.clear()
        entry.light_pose_dependencies.clear()
        entry.expression_light_poses.clear()
        entry.light_pose_overrides.clear()
        entry.pose_source_ids.clear()
        entry.pose_sources_unknown = False
        entry.light_pose_capture_required = False
        entry.environment_pose_source = None
        entry.light_values_pending = False
        entry.linking_values_pending.clear()
        entry.material_values_pending = False
        entry.material_surface_rewired = False
        entry.light_basis = None
    if entry is None:
        entry = Entry(key, config, session_from_configuration(config))
        _entries[key] = entry
        _local_finals.retain_entry(entry)
    entry.rop = rop
    entry.config = config
    if entry.observer is None:
        entry.observer = native_scene.SceneObserver(
            rop,
            entry.changed,
            lambda: entry.session.native_material_paths(),
            track_deletions=True,
            camera_callback=lambda: entry.changed(camera_only=True),
            object_pose_source_callback=lambda node: entry.changed(object_pose=True, source_node=node),
            light_value_callback=lambda node, pose=False: entry.changed(
                object_pose=pose, source_node=node, light_value=True
            ),
            linking_value_callback=lambda identity: entry.changed(linking_receiver=identity),
            material_value_callback=lambda path, rewired: entry.changed(
                material_value=path, material_surface_rewired=rewired
            ),
            stylesheet_resources=lambda: entry.session.native_stylesheet_resources(),
            cop_sources=lambda: entry.session.native_cop_sources(),
        )
    ensure_polling()
    return entry


def attach(entry):
    _retire_requested.discard(entry.key)
    entry.views += 1


def detach(entry):
    entry.views = max(0, entry.views - 1)
    if entry.views == 0 and not entry.channel:
        if hou.isUIAvailable():
            # Python Panel destruction runs inside Houdini's interface teardown.
            # Retire the renderer from the next host event-loop poll instead.
            _retire_requested.add(entry.key)
        else:
            retire(entry.key)


def bind_native(entry, socket_port):
    global _bridge
    if _bridge is None:
        _bridge = ControlBridge()
    if entry.channel:
        _bridge.unbind(entry.channel)
    message = _bridge.bind(entry.key)
    entry.channel = message["channel"]
    if entry.socket_port != socket_port or entry.display_retry_at:
        entry.session.close_native_display()
        entry.session.open_native_display("ip", socket_port)
        entry.socket_port = socket_port
    entry.display_retry_at = 0.0
    entry.display_retry_failures = 0
    entry.display_reopened_at = 0.0
    return message


def _display_connection_failed(entry, now, error):
    # The rendering worker and its retained frame remain valid when only the
    # image socket fails. Keep the control binding so an actual viewer Stop or
    # close event still follows the normal cancellation path.
    entry.display_retry_failures = min(entry.display_retry_failures + 1, 6)
    delay = min(0.1 * (2 ** (entry.display_retry_failures - 1)), 2.0)
    entry.display_retry_at = now + delay
    entry.display_reopened_at = 0.0
    trace("display_connection_failed", key=entry.key, retry_in=delay, error=str(error))


def poll():
    global _in_poll
    if _in_poll:
        return
    _in_poll = True
    try:
        _poll_host()
    finally:
        _in_poll = False
    _stop_polling_when_idle()


def _stop_polling_when_idle():
    """With no session, retirement or local final left, stop the host poll.

    It runs on every Houdini event-loop pass (about 15 per second measured)
    and scans all pane tabs; after the last viewer closes nothing needs it.
    for_rop and begin_local_final restart it through ensure_polling.
    """
    global _polling
    if not _polling or _entries or _retire_requested or _local_finals.active or _local_final_jobs:
        return
    if not hou.isUIAvailable():
        return
    # Houdini allows a callback to remove itself (see the GUI test bootstrap).
    if poll in hou.ui.eventLoopCallbacks():
        hou.ui.removeEventLoopCallback(poll)
    _polling = False
    from PySide6 import QtWidgets

    try:
        QtWidgets.QApplication.instance().aboutToQuit.disconnect(close_all)
    except (RuntimeError, TypeError):
        pass  # Already disconnected by host teardown.


def _poll_host():
    global _last_display, _last_texture_poll
    for entry in tuple(_entries.values()):
        try:
            if entry.rop is not None:
                entry.rop.path()
        except hou.ObjectWasDeleted:
            retire(entry.key)
    for key in tuple(_retire_requested):
        _retire_requested.discard(key)
        entry = _entries.get(key)
        if entry and not entry.views and not entry.channel:
            if _local_finals.holds(entry):
                _retire_requested.add(key)
            else:
                retire(key)
    if _bridge:
        for key, channel, event, pid in _bridge.drain():
            entry = _entries.get(key)
            if not entry or entry.channel != channel:
                continue
            if event == "connected":
                entry.helper_pid = pid
            elif event == "closed":
                entry.channel = ""
                entry.helper_pid = None
                # Closing one display must not stop a render still owned by a
                # panel. A Stop action leaves its native pane present and must
                # continue to cancel the shared session.
                native_pane = any(
                    p.type() == hou.paneTabType.IPRViewer and p.ropNode() == entry.rop for p in hou.ui.paneTabs()
                )
                if native_pane or not entry.views:
                    entry.stop_serial += 1
                    entry.final_update_pending = False
                    entry.session.cancel()
                entry.session.close_native_display()
                entry.socket_port = ""
                entry.display_retry_at = 0.0
                entry.display_retry_failures = 0
                entry.display_reopened_at = 0.0
                if not entry.views:
                    retire(key)
    now = time.monotonic()
    if hou.isUIAvailable():
        mouse_down = _mouse_button_down()
        for entry in tuple(_entries.values()):
            drag_released = entry.camera_changed_with_button_down and not mouse_down
            if entry.camera_refinement_pending and (drag_released or now - entry.camera_changed_at >= 0.15):
                entry.camera_refinement_pending = False
                # Finishing navigation after Stop must not restart the renderer.
                if entry.session.status()["state"] not in ("closed", "cancelled"):
                    # Only the camera dicing policy changed. A pending source edit
                    # still takes the authoritative capture path.
                    entry.changed(camera_only=True)
    if now - _last_texture_poll >= 1.0:
        _last_texture_poll = now
        try:
            synchronize_color_configuration()
        except RuntimeError as error:
            for entry in tuple(_entries.values()):
                if entry.session.status()["state"] != "closed":
                    entry.fail("Color configuration update failed: " + str(error))
            return
        for entry in tuple(_entries.values()):
            if entry.observer:
                entry.observer.selection_changed()
            if entry.session.status()["state"] != "closed":
                entry.session.notify_color_configuration()
            status = entry.session.status()
            if status["state"] == "closed" or (status["state"] == "cancelled" and not entry.error):
                continue
            if status.get("color_update_pending"):
                if entry.color_notified != status["color_target_epoch"]:
                    entry.color_notified = status["color_target_epoch"]
                    entry.changed()
            else:
                entry.color_notified = 0
                if entry.session.textures_changed():
                    entry.changed()
    # Only a session shown in Houdini's own IPR viewer (a bound channel) reads
    # that viewer's state. Without one, skip scanning every pane tab per pass.
    if hou.isUIAvailable() and any(entry.channel for entry in _entries.values()):
        # Public native-view state controls the shared renderer. The helper is only a lifetime bridge.
        viewers = [p for p in hou.ui.paneTabs() if p.type() == hou.paneTabType.IPRViewer]
        for viewer in viewers:
            rop = viewer.ropNode()
            entry = _entries.get(f"rop:{rop.sessionId()}") if rop else None
            if entry and entry.channel and viewer.isActive():
                time_value = hou.time()
                if entry.observed_time != time_value:
                    entry.observed_time = time_value
                    entry.changed()
                paused = viewer.isPaused()
                if paused != entry.paused:
                    entry.session.pause(paused or entry.final_paused)
                    entry.paused = paused
                if viewer.isAutoUpdateOn() and not paused and not local_final_active():
                    entry.preview_object_poses()
                    _apply_narrow_directly(entry)
                if (
                    not paused
                    and not local_final_active()
                    and (viewer.isAutoUpdateOn() or entry.final_update_pending)
                    and entry.change_serial > entry.applied_serial
                    and entry.requested_serial != entry.change_serial
                    and not entry.defer_source_capture()
                    and time.monotonic() - entry.changed_at > (0.0 if entry.camera_only_pending else 0.08)
                ):
                    # SOHO can coalesce a property notification at the end of a prior
                    # update. Keep an independent host change serial until applied.
                    entry.requested_serial = entry.change_serial
                    entry.final_update_pending = False
                    viewer.startRender()
    for entry in tuple(_entries.values()):
        if (
            entry.narrow_check is not None
            and entry.change_serial == entry.applied_serial
            and time.monotonic() - entry.changed_at > 0.5
        ):
            entry.run_narrow_check()
    # Drain native Stop/Pause intent before releasing a final pause lease.
    _poll_local_finals()
    now = time.monotonic()
    if now - _last_display < 1 / 25:
        return
    _last_display = now
    for entry in tuple(_entries.values()):
        if entry.socket_port:
            if entry.display_retry_at and now < entry.display_retry_at:
                continue
            try:
                if entry.display_retry_at:
                    # Replacement owns the same destination. The native layer
                    # retires its previous writer without waiting on this poll.
                    entry.session.open_native_display("ip", entry.socket_port)
                    entry.display_retry_at = 0.0
                    entry.display_reopened_at = now
                entry.session.present_native()
                entry.previous_color_notice = False
                if entry.display_reopened_at and now - entry.display_reopened_at >= 1.0:
                    entry.display_retry_failures = 0
                    entry.display_reopened_at = 0.0
                status = entry.session.status()
                if entry.observer:
                    entry.observer.refresh_material_bindings(entry.session)
                if status["state"] == "failed":
                    entry.error = status["error"]
                    if not status.get("update_rejected"):
                        entry.session.cancel()
            except _native.PreviousColorConfiguration as notice:
                if not entry.previous_color_notice and hou.isUIAvailable():
                    hou.ui.setStatusMessage(str(notice))
                entry.previous_color_notice = True
            except _native.DisplayConnectionError as error:
                _display_connection_failed(entry, now, error)
            except RuntimeError as error:
                entry.error = str(error)
                entry.session.cancel()
                entry.session.close_native_display()
                entry.socket_port = ""
                entry.display_retry_at = 0.0
                entry.display_retry_failures = 0
                entry.display_reopened_at = 0.0
                if _bridge and entry.channel:
                    _bridge.unbind(entry.channel)
                    entry.channel = ""
                    entry.helper_pid = None
                if not entry.views:
                    retire(entry.key)


def _apply_narrow_directly(entry):
    """Render View: send a pending narrow update from the host poll, as pose previews are.

    Houdini's IPR viewer does not run the SOHO update while a mouse button is held
    (measured on screen: intensity and camera gestures changed the image only after
    release). A narrow update needs no SOHO pass: reuse the camera of the last SOHO
    update (the viewer's camera, which a narrow edit does not change).
    """
    if (
        not (entry.light_values_pending or entry.linking_values_pending or entry.material_values_pending)
        or entry.change_serial <= entry.applied_serial
        or not entry.rop
    ):
        return
    camera_update = entry.last_soho.get("camera_update")
    if not camera_update:
        return
    now = hou.time()
    try:
        motion = native_scene.motion_configuration(entry.rop, now)
        if motion != camera_update.get("motion"):
            return
        narrow = entry.narrow_update(now, motion, dict(camera_update))
        if narrow is not None:
            trace("narrow_direct", path=entry.narrow_reason)
            entry.apply(narrow, now, reuse_if_unchanged=True, change_serial=entry.change_serial)
    except (ValueError, hou.Error):
        return  # The SOHO update after release reports source errors.


def ensure_polling():
    global _polling, _registered_shutdown
    if not _registered_shutdown:
        # HIP replacement must release owned workers even when the registry is
        # exercised from hython, where no interface poll runs.
        hou.hipFile.addEventCallback(_scene_event)
        _registered_shutdown = True
    if not hou.isUIAvailable():
        return
    if not _polling:
        from PySide6 import QtWidgets

        # Houdini owns this callback's execution and lifetime. Only the panel's
        # widget-local refresh timer uses Qt; host scene operations use HOM's loop.
        hou.ui.addEventLoopCallback(poll)
        QtWidgets.QApplication.instance().aboutToQuit.connect(close_all)
        _polling = True


def _scene_event(event_type, **kwargs):
    if event_type in (hou.hipFileEventType.BeforeClear, hou.hipFileEventType.BeforeLoad):
        close_all(preserve_local_finals=True)


def retire(key):
    _retire_requested.discard(key)
    entry = _entries.pop(key, None)
    if entry:
        if entry.observer:
            entry.observer.close()
            entry.observer = None
        entry.rop = None
        if _bridge and entry.channel:
            _bridge.unbind(entry.channel)
        entry.channel = ""
        entry.socket_port = ""
        entry.helper_pid = None
        entry.session.close()
    if not _entries:
        # The pose memo of navigation_snapshot holds object tables of the last
        # session; nothing can reuse them once no session remains.
        from .. import _native

        release = getattr(_native, "release_navigation_memo", None)
        if release is not None:
            release()


def close_all(*, preserve_local_finals=False):
    global _bridge, _polling, _registered_shutdown
    if _polling:
        # Scene replacement and host teardown can already remove callbacks.
        # Repeated cleanup must still release sessions and the control bridge.
        if poll in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(poll)
        from PySide6 import QtWidgets

        QtWidgets.QApplication.instance().aboutToQuit.disconnect(close_all)
        _polling = False
    if _registered_shutdown:
        hou.hipFile.removeEventCallback(_scene_event)
        _registered_shutdown = False
    if preserve_local_finals:
        _local_finals.clear_entries()
    else:
        _local_finals.clear()
        _local_final_jobs.clear()
    for key in tuple(_entries):
        retire(key)
    if _bridge:
        _bridge.close()
        _bridge = None
    if preserve_local_finals and _local_finals.active:
        ensure_polling()


def _shutdown_resources():
    """Final interpreter guard: no HOM/Qt traversal after host UI teardown."""
    global _bridge
    for entry in tuple(_entries.values()):
        shutdown = getattr(entry.session, "shutdown", entry.session.close)
        shutdown()
    if _bridge:
        _bridge.close()
        _bridge = None


atexit.register(_shutdown_resources)
