# SPDX-License-Identifier: Apache-2.0
"""Actual Auto Update timeline-to-Qt-paint latency for resident Blender instances.

Launch with run-gui-test.ps1. Optional JSON settings live at
CYCLES_HOUDINI_TIMELINE_CONFIG. The artist HIP is copied before loading; no
artist file is saved. Timed edits call only hou.setFrame. Qt paint completion
and logical no-op acknowledgments are distinct from worker image arrival and
physical display scanout.
"""

import hashlib
import ctypes
from collections import Counter
import json
import os
from pathlib import Path
import shutil
import threading
import time
import traceback

import hou
import numpy as np
from PySide6 import QtCore, QtGui, QtTest

import cycles_houdini as ch
from cycles_houdini import blender_assets, blend_worker, device_selection, native_scene, queued, registry
from cycles_houdini.viewer import panel as panel_module
from support.private_scenes import artist_scene


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def image_values(image):
    """Used outside timing windows, never in ordinary paints/polls."""
    image = image.convertToFormat(QtGui.QImage.Format_RGBA8888).scaled(96, 54)
    return (
        np.frombuffer(image.constBits(), np.uint8, image.sizeInBytes())
        .reshape(54, image.bytesPerLine())[:, :384]
        .copy()
    )


class TimelineBenchmark:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        defaults = dict(
            hip=None,
            blend=None,
            target="Shape",
            form="attribute",
            count=51294,
            width=640,
            height=360,
            samples=256,
            repetitions=3,
            hold_seconds=60,
            timeout_seconds=360,
            freeze_static_lights=False,
        )
        config_path = os.environ.get("CYCLES_HOUDINI_TIMELINE_CONFIG")
        if config_path:
            defaults.update(json.loads(Path(config_path).read_text(encoding="utf-8-sig")))
        # The default scene and library are private artist files (see tests/support/private_scenes.py).
        defaults["hip"] = defaults["hip"] or str(artist_scene("Cycles_Test/Blender_Instancing.hiplc"))
        defaults["blend"] = defaults["blend"] or str(artist_scene("Cycles_Test/assets/Instance_Source.blend"))
        self.config = defaults
        assert self.config["form"] in ("attribute", "packed")
        if self.config.get("target_assignment"):
            assert self.config["target"] == "wine_bottles_01" and self.config["count"] % 2 == 0
        self.began = time.monotonic()
        self.phase = "setup"
        self.case = None
        self.restores = []
        self.paints = []
        self.events = []
        self.event_counts = {}
        self.trials = []
        self.conversions = []
        self.source = dict(evaluations=0, batches=0, wire_bytes=0, evaluated_frames=[])
        self.report = dict(
            result="running",
            host=ch.__file__,
            houdini=hou.applicationVersionString(),
            host_pid=os.getpid(),
            config=self.config,
            trials=self.trials,
            events=self.events,
            paints=self.paints,
            display_conversions=self.conversions,
            measurement="Original ImageCanvas.paintEvent return after QPainter.end for a current session/display epoch/generation/render_revision. This is Qt paint completion, not physical display scanout.",
            automatic_path="Real panel Auto Update; timed edits use only hou.setFrame. Automatic reuse_if_unchanged=True differs from the prior forced session.apply benchmark default False.",
            native_viewport="Artist geometry remains displayed; one foreground Cycles render panel. No synthetic worker-only frame timing is substituted.",
        )
        self.load_fixture()
        self.install_hooks()
        self.phase = "warm-source"
        reference = self.reference
        warm = [(reference, 1.0, True), (reference, 2.0, True)]
        if self.config.get("target_assignment"):
            warm.append((self.alternative_reference, 1.0, True))
        results = blender_assets.prepare_prototypes(warm)
        assert not any(isinstance(result, Exception) for result in results), results
        self.report["warm_source"] = dict(self.source, evaluated_frames=list(self.source["evaluated_frames"]))
        self.report["source_frame_fingerprints"] = [
            hashlib.sha256(
                json.dumps(
                    [
                        (member["geometry_key"], member["transform"], member["material_paths"])
                        for member in result["instances"]
                    ],
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            for result in results[:2]
        ]
        self.target_members = len(results[0]["instances"])
        if self.config.get("rigid_source"):
            before, after = (result["instances"] for result in results[:2])
            assert len(before) == len(after) == 4
            before_keys = [member["geometry_key"] for member in before]
            after_keys = [member["geometry_key"] for member in after]
            assert before_keys == after_keys, (before_keys, after_keys)
            assert all(first["transform"] != second["transform"] for first, second in zip(before, after))
            self.report["rigid_source"] = dict(
                geometry_keys=before_keys,
                frame_one_transforms=[member["transform"] for member in before],
                frame_two_transforms=[member["transform"] for member in after],
                interpretation="Resident mesh identities are unchanged; each collection member changes rigid pose at source frames1/2.",
            )
        if self.config.get("target_assignment"):
            self.assignment_members = self.target_members + len(results[2]["instances"])
            self.report["target_assignment"] = dict(
                references=[self.reference, self.alternative_reference],
                source_frames=[1.0, 1.0],
                members=[self.target_members, len(results[2]["instances"])],
                expected_objects=self.config["count"] // 2 * self.assignment_members,
                interpretation="Swap target references on stable point IDs; both targets remain present and resident at absolute source frame1.",
            )
        self.create_panel()
        self.mode_index = 0
        self.repetition = 0
        self.modes = list(
            self.config.get("modes", ["global-time-unchanged", "camera", "point-placement", "source-index"])
        )
        if self.config.get("target_assignment") and "target-assignment" not in self.modes:
            self.modes.append("target-assignment")
        self.stage = "initial"
        self.initial_render_clicked = False
        self.phase = "initial-ipr"
        self.last_tick = time.monotonic()
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.setInterval(10)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.save()

    def load_fixture(self):
        hip, blend = Path(self.config["hip"]), Path(self.config["blend"])
        copied = self.path.parent / ("isolated-" + hip.name)
        shutil.copy2(hip, copied)
        self.input_hashes = dict(hip=digest(hip), blend=digest(blend))
        self.report["inputs"] = dict(
            original_hip=str(hip), loaded_hip=str(copied), blend=str(blend), sha256=self.input_hashes
        )
        try:
            hou.hipFile.load(str(copied), suppress_save_prompt=True)
        except hou.LoadWarning:
            pass
        # Resolve unchanged artist-relative texture paths without copying or
        # saving artist assets. hipFile.path() remains the isolated copy.
        hou.putenv("HIP", str(hip.parent))
        hou.setFrame(1)
        light, parent = hou.node("/obj/cycles_area_light1"), hou.node("/obj/null1")
        if self.config["freeze_static_lights"]:
            assert light is not None and parent is not None and light.input(0) == parent
            rotation = parent.parm("ry")
            frame_one = rotation.eval()
            rotation.deleteAllKeyframes()
            rotation.set(frame_one)
            matrices = [list(light.worldTransformAtTime(hou.frameToTime(frame)).asTuple()) for frame in (1, 2, 3, 4)]
            assert all(matrix == matrices[0] for matrix in matrices), "Controlled light remains animated"
            self.report["lighting_policy"] = dict(
                frozen_parameter=rotation.path(),
                frame_one_value=frame_one,
                verified_frames=[1, 2, 3, 4],
                verified_world_matrices=matrices,
            )
        else:
            assert light is not None and parent is not None and light.input(0) == parent
            rotation = parent.parm("ry")
            frames = [1, 2, 3, 4]
            rotations = [rotation.evalAtFrame(frame) for frame in frames]
            matrices = [list(light.worldTransformAtTime(hou.frameToTime(frame)).asTuple()) for frame in frames]
            assert rotation.isTimeDependent() and len(set(rotations)) == len(frames), rotations
            assert all(matrices[index] != matrices[index - 1] for index in range(1, len(frames))), matrices
            self.report["lighting_policy"] = dict(
                artist_light_animation_retained=True,
                parameter=rotation.path(),
                keyframe_count=len(rotation.keyframes()),
                verified_frames=frames,
                verified_rotations=rotations,
                verified_world_matrices=matrices,
                interpretation="Global time changes also move the area light through animated null1.ry; this is an artist-context light-motion baseline, not a no-op control.",
            )
        self.obj = hou.node("/obj/geo1") or hou.node("/obj/bottles")
        self.wrangle = self.obj.node("pointwrangle1")
        scatter = self.obj.node("scatter1")
        scatter.bypass(False)
        self.wrangle.setInput(0, scatter)
        for name, value in (("forcetotal", 1), ("npts", self.config["count"])):
            scatter.parm(name).deleteAllKeyframes()
            scatter.parm(name).set(value)
        kind = "object" if self.config["target"] == "Shape" else "collection"
        self.reference = blend.resolve().as_posix() + "#" + kind + "=" + self.config["target"]
        self.alternative_reference = blend.resolve().as_posix() + "#object=Shape"
        self.author_points("global-time-unchanged")
        self.output = self.wrangle
        self.packed = None
        if self.config["form"] == "packed":
            self.packed = self.obj.createNode("cycles::blender_reference", "timeline_reference")
            self.packed.parm("frame").deleteAllKeyframes()
            self.packed.setParms(
                dict(file=blend.resolve().as_posix(), target_kind=kind, target=self.config["target"], frame=1)
            )
            remove = self.obj.createNode("attribdelete", "timeline_remove_instance")
            remove.setInput(0, self.wrangle)
            remove.parm("ptdel").set("instance index")
            self.output = self.obj.createNode("copytopoints", "timeline_packed_copies")
            self.output.setInput(0, self.packed)
            self.output.setInput(1, remove)
            self.output.parm("pack").set(0)
        self.output.setRenderFlag(True)
        self.output.setDisplayFlag(True)
        self.obj.setDisplayFlag(True)
        self.output.cook(force=True)
        assert len(self.wrangle.geometry().points()) == self.config["count"]
        self.report["point_positions_sha256"] = hashlib.sha256(
            np.asarray(self.wrangle.geometry().pointFloatAttribValues("P"), np.float32).tobytes()
        ).hexdigest()
        self.rop = hou.node("/out/cycles_render") or hou.node("/out/cam_1")
        self.camera = hou.node(self.rop.parm("camera").eval())
        self.camera_x = self.camera.parm("tx").eval()
        device = os.environ["CYCLES_HOUDINI_GUI_DEVICE"]
        if device == "OPTIX":
            device = next(item["id"] for item in ch.devices() if item["backend"] == "OPTIX" and "4090" in item["name"])
        self.device_ids = device_selection.set_devices(self.rop, device)
        self.rop.setParms(
            dict(
                source_mode="objects",
                objectroot="/obj",
                vobject=self.obj.path(),
                forceobject=self.obj.path(),
                resx=self.config["width"],
                resy=self.config["height"],
                samples=self.config["samples"],
                ipr_samples=self.config["samples"],
                use_denoise=0,
                ipr_use_denoise=0,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                allowmotionblur=0,
                render_threads=4,
                seed=0,
                animate_seed=0,
                viewer_interactive=1,
                resolution_percent=100,
                ipr_resolution_percent=100,
            )
        )
        if self.config.get("dlss"):
            self.rop.setParms(dict(ipr_use_denoise=1, ipr_denoiser="dlss", ipr_dlss_quality="native"))
        self.key = "rop:" + str(self.rop.sessionId())

    def entry(self):
        return registry.get(self.key)

    def author_points(self, mode):
        index = "((int(@Frame)%2)==0 ? 2.0 : 1.0)" if mode == "source-index" else "1.0"
        placement = "@P.x += .20*(@Frame-1);\n" if mode == "point-placement" else ""
        reference = json.dumps(self.reference)
        assignment = ""
        if mode == "target-assignment":
            reference = (
                "(((@ptnum+int(@Frame))%2)==0 ? " + reference + " : " + json.dumps(self.alternative_reference) + ")"
            )
            assignment = "i@id=@ptnum;\n"
            if self.config["form"] == "packed":
                assignment += 's@name=(((@ptnum+int(@Frame))%2)==0 ? "Wine" : "Shape");\n'
        self.wrangle.parm("snippet").set(
            "s@instance="
            + reference
            + "; f@index="
            + index
            + ";\n"
            + placement
            + assignment
            + "matrix3 m=ident(); 3@transform=m*.2;"
        )

    def context(self):
        entry = self.entry()
        if entry is None:
            return {}
        session = entry.session
        active = getattr(session, "_active_capture", None)
        return dict(
            change_serial=entry.change_serial,
            applied_serial=entry.applied_serial,
            requested_serial=entry.requested_serial,
            observed_time=entry.observed_time,
            in_host_capture=bool(getattr(session, "_in_host_capture", False)),
            active_submission=active.get("submission") if isinstance(active, dict) else None,
            active_stage=active.get("stage") if isinstance(active, dict) else None,
        )

    def record(self, kind, began, ended, **data):
        self.event_counts[kind] = self.event_counts.get(kind, 0) + 1
        if len(self.events) < 40000:
            self.events.append(
                dict(
                    kind=kind,
                    begin=began,
                    end=ended,
                    seconds=ended - began,
                    phase=self.phase,
                    trial=self.case["id"] if self.case else None,
                    thread=threading.get_ident(),
                    **data,
                )
            )

    def hook(self, owner, name, details=None, after=None):
        original = getattr(owner, name)

        def traced(*args, **kwargs):
            begin = time.monotonic()
            data = details(*args, **kwargs) if details else {}
            try:
                result = original(*args, **kwargs)
                if after:
                    data.update(after(result, *args, **kwargs))
                return result
            finally:
                self.record(
                    getattr(owner, "__name__", type(owner).__name__) + "." + name, begin, time.monotonic(), **data
                )

        setattr(owner, name, traced)
        self.restores.append((owner, name, original))

    def install_hooks(self):
        original_factory = registry.session_from_configuration

        def factory(*args, **kwargs):
            return original_factory(*args, **dict(kwargs, collect_diagnostics=True))

        registry.session_from_configuration = factory
        self.restores.append((registry, "session_from_configuration", original_factory))
        self.hook(
            registry.Entry,
            "changed",
            lambda *args, **kwargs: dict(
                **self.context(),
                options={key: value for key, value in kwargs.items() if isinstance(value, (str, bool, int, float))},
            ),
        )
        self.hook(
            registry.Entry,
            "apply",
            lambda entry, update, now, **kwargs: dict(
                **self.context(),
                at_time=now,
                camera_only="geometry" not in update,
                reuse_if_unchanged=kwargs.get("reuse_if_unchanged", False),
                pose_preview=kwargs.get("pose_preview", False),
            ),
            lambda result, *args, **kwargs: dict(generation=result),
        )

        def source_event(observer, **kwargs):
            node = kwargs.get("node")
            parm = kwargs.get("parm_tuple")
            try:
                path = node.path() if node is not None else ""
            except hou.ObjectWasDeleted:
                path = "<deleted>"
            return dict(
                **self.context(),
                node=path,
                event=str(kwargs.get("event_type")),
                parm=parm.name() if parm is not None else "",
                observer=id(observer),
            )

        self.hook(native_scene.SceneObserver, "_changed", source_event)
        self.hook(
            native_scene.SceneObserver,
            "refresh",
            lambda observer, *args, **kwargs: dict(
                observer=id(observer), nodes_before=len(observer.nodes), **self.context()
            ),
            lambda result, observer, *args, **kwargs: dict(nodes_after=len(observer.nodes)),
        )
        self.hook(
            native_scene,
            "capture",
            lambda rop, *args, **kwargs: dict(**self.context(), options=kwargs),
            lambda result, *args, **kwargs: dict(
                source_frames=sorted(
                    {
                        float(proto["source_frame"])
                        for request in result.get("geometry", [])
                        for role in ("point_instancing", "packed_blender")
                        for proto in request.get(role, {}).get("prototypes", [])
                        if "source_frame" in proto
                    }
                )
            ),
        )
        self.hook(
            queued.WorkerSession,
            "_start_capture",
            lambda session, request: dict(submission=request[3], at_time=request[1], **self.context()),
        )
        self.hook(queued.WorkerSession, "_advance_capture", lambda *args: self.context())
        self.hook(queued.ExternalBackend, "apply")
        self.hook(
            queued.QueuedController,
            "accept",
            lambda controller, reply, submission=None: dict(
                submission=submission,
                physical=reply.get("status", {}).get("generation"),
                direct_attribute_updates=reply.get("status", {}).get("direct_attribute_updates"),
                has_image=reply.get("frame") is not None,
            ),
        )
        self.hook(panel_module.RenderPanel, "request_update", lambda *args, **kwargs: self.context())
        original = blend_worker.evaluate_many

        def evaluated(source, executable, requests, *args, **kwargs):
            self.source["evaluations"] += len(requests)
            self.source["batches"] += 1
            self.source["evaluated_frames"].extend(request["frame"] for request in requests)
            return original(source, executable, requests, *args, **kwargs)

        blend_worker.evaluate_many = evaluated
        self.restores.append((blend_worker, "evaluate_many", original))
        original_recv = blend_worker._Pipe.recv_bytes

        def received(*args, **kwargs):
            result = original_recv(*args, **kwargs)
            self.source["wire_bytes"] += len(result)
            return result

        blend_worker._Pipe.recv_bytes = received
        self.restores.append((blend_worker._Pipe, "recv_bytes", original_recv))

    def create_panel(self):
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.panel = self.tab.activeInterfaceRootWidget()
        self.panel.set_rop(self.rop)
        self.panel.auto.setChecked(True)
        self.panel.preview_interactive.setChecked(True)
        window = self.panel.window()
        screen = QtGui.QGuiApplication.primaryScreen()
        geometry, available = screen.geometry(), screen.availableGeometry()
        ratio = screen.devicePixelRatio()
        physical = (round(geometry.width() * ratio), round(geometry.height() * ratio))
        self.report["primary_screen"] = dict(
            name=screen.name(),
            geometry=list(geometry.getRect()),
            available_geometry=list(available.getRect()),
            device_pixel_ratio=ratio,
            physical_resolution=physical,
        )
        self.save()
        if physical not in ((3840, 2160), (4096, 2160), (2160, 3840), (2160, 4096)):
            raise RuntimeError("Primary screen is not the requested main 4K monitor: " + repr(physical))
        window.resize(min(1120, available.width() - 80), min(780, available.height() - 80))
        window.move(available.left() + 40, available.top() + 40)
        window.show()
        original_convert = self.panel._display_queue.convert

        def converted(frame, settings):
            begin = time.monotonic()
            result = None
            try:
                result = original_convert(frame, dict(settings, collect_diagnostics=True))
                return result
            finally:
                self.conversions.append(
                    dict(
                        begin=begin,
                        end=time.monotonic(),
                        phase=self.phase,
                        trial=self.case["id"] if self.case else None,
                        render_revision=result.get("generation") if result else None,
                        native=result.get("diagnostics") if result else None,
                        settings=dict(settings),
                    )
                )

        self.panel._display_queue.convert = converted
        original_paint = type(self.panel.canvas).paintEvent

        def painted(canvas, event):
            metadata = dict(getattr(canvas, "frame_metadata", {}))
            begin = time.monotonic()
            original_paint(canvas, event)
            unchanged = metadata == dict(getattr(canvas, "frame_metadata", {}))
            if canvas is self.panel.canvas and metadata and unchanged and not canvas.image.isNull():
                self.painted(metadata, begin, time.monotonic())

        type(self.panel.canvas).paintEvent = painted
        self.restores.append((type(self.panel.canvas), "paintEvent", original_paint))

    def painted(self, metadata, begin, end):
        entry = self.entry()
        if entry is None or self.panel._session is not entry.session:
            return
        controller = entry.session._controller
        with controller.condition:
            current = dict(
                generation=controller.requested,
                render_revision=controller.physical,
                backend_epoch=controller.epoch,
                required_scene=controller.required_scene,
                completed_scene=controller.completed_scene,
            )
        on_main_monitor = self.panel_on_main_monitor()
        valid = (
            on_main_monitor
            and metadata.get("generation") == current["generation"]
            and metadata.get("render_revision") == current["render_revision"]
            and metadata.get("display_epoch") == self.panel._display_epoch
        )
        if len(self.paints) < 20000:
            self.paints.append(
                dict(
                    begin=begin,
                    end=end,
                    metadata=metadata,
                    current=current,
                    valid=valid,
                    on_main_monitor=on_main_monitor,
                    phase=self.phase,
                    trial=self.case["id"] if self.case else None,
                )
            )
        self.last_paint = dict(at=end, metadata=metadata, current=current, valid=valid)
        case = self.case
        if not case or not valid or current["backend_epoch"] != case["backend_epoch"]:
            return
        newer = metadata["generation"] > case["before_generation"]
        physical = metadata["render_revision"] > case["before_revision"]
        if newer:
            case.setdefault("logical_paint_seconds", end - case["started"])
        if not newer or not physical:
            return
        # Sample counts do not certify useful RGB: a cleared/current buffer can
        # precede the useful image. Inspect only a bounded 24-pixel grid for a
        # new candidate; full image inspection remains outside timing.
        if "first_preview_seconds" not in case:
            image = self.panel.canvas.image
            colors = [
                image.pixel(int(x * (image.width() - 1) / 5), int(y * (image.height() - 1) / 3))
                for y in range(4)
                for x in range(6)
            ]
            channels = [(value >> shift) & 255 for value in colors for shift in (0, 8, 16)]
            if max(channels) - min(channels) <= 1:
                if "first_blank_seconds" not in case:
                    case["first_blank_seconds"] = end - case["started"]
                    case["first_blank_metadata"] = metadata
                    rejected = image.copy()
                    path = self.path.parent / (self.phase + "-first-blank.png")
                    QtCore.QTimer.singleShot(0, lambda: rejected.save(str(path)))
                return
        authoritative = (
            current["completed_scene"] >= current["required_scene"]
            and current["completed_scene"] > case["before_scene"]
            and getattr(entry.session, "_active_capture", None) is None
            and getattr(entry.session, "_pending_capture", None) is None
        )
        if case["mode"] in ("point-placement", "source-index", "target-assignment") and not authoritative:
            case.setdefault("stale_geometry_preview_seconds", end - case["started"])
            case.setdefault("stale_geometry_preview_metadata", metadata)
            return
        if "first_preview_seconds" not in case:
            case["first_preview_seconds"] = end - case["started"]
            case["first_preview_metadata"] = metadata
            case["first_preview_image"] = self.panel.canvas.image.copy()
            QtCore.QTimer.singleShot(
                0, lambda c=case: c.setdefault("after_paint_event_loop_seconds", time.monotonic() - c["started"])
            )
        if (
            metadata.get("rendered_width") == self.config["width"]
            and metadata.get("rendered_height") == self.config["height"]
        ):
            case.setdefault("first_fullsize_seconds", end - case["started"])
            case.setdefault("first_fullsize_metadata", metadata)

    def cached_status(self):
        entry = self.entry()
        if entry is None:
            return None
        if entry.error or getattr(entry.session, "_capture_failure", ""):
            raise RuntimeError(entry.error or entry.session._capture_failure)
        status = entry.session._controller.status()
        if status.get("error") or status.get("state") == "failed":
            raise RuntimeError(status)
        return status

    def window_state(self):
        kernel = ctypes.windll.user32
        kernel.GetForegroundWindow.restype = ctypes.c_void_p
        handle = kernel.GetForegroundWindow()
        foreground_pid = ctypes.c_ulong()
        kernel.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.GetWindowThreadProcessId(handle, ctypes.byref(foreground_pid))
        window = self.panel.window()
        return dict(
            foreground_pid=foreground_pid.value,
            foreground_is_test=foreground_pid.value == os.getpid(),
            active_qt_window=window.isActiveWindow(),
            panel_visible=self.panel.isVisible(),
            canvas_visible=self.panel.canvas.isVisible(),
            panel_geometry=list(window.frameGeometry().getRect()),
            on_main_monitor=self.panel_on_main_monitor(),
            screen=window.screen().name(),
        )

    def panel_on_main_monitor(self):
        screen = QtGui.QGuiApplication.primaryScreen()
        window = self.panel.window()
        canvas = self.panel.canvas
        canvas_rect = QtCore.QRect(canvas.mapToGlobal(QtCore.QPoint(0, 0)), canvas.size())
        available = screen.availableGeometry()
        return (
            window.screen() == screen and available.contains(window.frameGeometry()) and available.contains(canvas_rect)
        )

    def contain_panel(self):
        """Houdini can replace/reposition the floating window after creation."""
        if self.panel_on_main_monitor():
            return
        screen = QtGui.QGuiApplication.primaryScreen()
        available = screen.availableGeometry()
        window = self.panel.window()
        window.showNormal()
        handle = window.windowHandle()
        if handle is not None:
            handle.setScreen(screen)
        window.setGeometry(
            available.left() + 80,
            available.top() + 80,
            min(1120, available.width() - 160),
            min(780, available.height() - 160),
        )
        window.move(available.left() + 80, available.top() + 80)
        window.update()
        self.last_paint = dict(valid=False)
        self.report["main_monitor_repositions"] = self.report.get("main_monitor_repositions", 0) + 1

    def settled(self, status):
        entry = self.entry()
        return (
            entry is not None
            and not status.get("pending_update")
            and getattr(entry.session, "_active_capture", None) is None
            and getattr(entry.session, "_pending_capture", None) is None
            and entry.change_serial == entry.applied_serial
            and not self.panel._pending
            and self.panel._last_time == hou.time()
            and not self.panel._in_render
            and not self.panel._render_queued
        )

    def nonblank_baseline(self, status):
        values = image_values(self.panel.canvas.image).reshape(54, 96, 4)
        rgb_range = int(values[:, :, :3].max()) - int(values[:, :, :3].min())
        if rgb_range > 1:
            self.blank_baseline_since = None
            return True
        now = time.monotonic()
        if getattr(self, "blank_baseline_since", None) is None:
            self.blank_baseline_since = now
            self.panel.canvas.image.save(str(self.path.parent / (self.phase + "-blank-baseline.png")))
        self.report["blank_baseline"] = dict(
            phase=self.phase,
            rgb_range=rgb_range,
            first_rgba=values[0, 0].tolist(),
            last_rgba=values[-1, -1].tolist(),
            display_options=self.panel.display_options(),
            metadata=dict(self.panel.canvas.frame_metadata),
            status=status,
        )
        if now - self.blank_baseline_since >= 3:
            raise AssertionError("Settled pre-trial IPR baseline remained blank for three seconds")
        return False

    def configure_mode(self):
        mode = self.modes[self.mode_index]
        self.case = None
        self.phase = "configure-" + mode
        self.camera.parm("tx").deleteAllKeyframes()
        self.camera.parm("tx").set(self.camera_x)
        self.author_points(mode)
        if mode == "camera":
            self.camera.parm("tx").setExpression(repr(self.camera_x) + " + .20*($F-1)", hou.exprLanguage.Hscript)
        if self.packed:
            self.packed.parm("frame").deleteAllKeyframes()
            if mode == "source-index":
                self.packed.parm("frame").setExpression("if($F%2,1,2)", hou.exprLanguage.Hscript)
            else:
                self.packed.parm("frame").set(1)
            if mode == "target-assignment":
                from cycles_houdini.blender import blend_catalogue

                self.packed.setParms(dict(multiple_references=1, references=2))
                blend_catalogue.initialize_rows(self.packed)
                for index in (1, 2):
                    self.packed.parm(f"ref_frame{index}").deleteAllKeyframes()
                self.packed.setParms(
                    dict(
                        ref_name1="Wine",
                        ref_target_kind1="collection",
                        ref_target1="wine_bottles_01",
                        ref_frame1=1,
                        ref_name2="Shape",
                        ref_target_kind2="object",
                        ref_target2="Shape",
                        ref_frame2=1,
                    )
                )
                frames = [
                    [self.packed.parm(f"ref_frame{i}").evalAtFrame(frame) for frame in (1, 2, 3, 4)] for i in (1, 2)
                ]
                assert frames == [[1.0] * 4, [1.0] * 4], frames
                self.report["assignment_packed_source_frames"] = frames
                intrinsic_frames = [
                    [
                        primitive.intrinsicValue("cycles_blender_frame")
                        for primitive in self.packed.geometryAtFrame(frame).prims()
                    ]
                    for frame in (1, 2)
                ]
                assert intrinsic_frames == [[1.0, 1.0], [1.0, 1.0]], intrinsic_frames
                self.report["assignment_packed_intrinsic_frames"] = intrinsic_frames
                self.output.setParms(dict(useidattrib=1, idattrib="name", targetattribs=1, applyattribs1="id"))
        hou.setFrame(1)
        self.report.setdefault("mode_time_dependence", {})[mode] = dict(
            wrangle=getattr(self.wrangle, "isTimeDependent", lambda: None)(),
            output=getattr(self.output, "isTimeDependent", lambda: None)(),
            camera=any(parm.isTimeDependent() for parm in self.camera.parms()),
            snippet_uses_frame="@Frame" in self.wrangle.parm("snippet").eval(),
        )
        # Outside the timed edit, queue the same automatic reuse policy once
        # to settle the changed fixture controls before measuring timeline input.
        self.panel.queue_render(reuse_if_unchanged=True)
        self.rest_until = time.monotonic() + 0.5
        self.stage = "prepare"

    def start_case(self, status):
        mode = self.modes[self.mode_index]
        self.phase = mode + "-" + str(self.repetition)
        before_image = image_values(self.panel.canvas.image)
        self.case = dict(
            id=len(self.trials) + len(self.report.get("disqualified_trials", [])),
            mode=mode,
            repetition=self.repetition,
            from_frame=hou.frame(),
            to_frame=2 + self.repetition,
            before_generation=status["generation"],
            before_revision=status["render_revision"],
            before_scene=status["requested_scene_submission"],
            before_status=dict(status),
            backend_epoch=status["backend_epoch"],
            before_source=dict(self.source, evaluated_frames=list(self.source["evaluated_frames"])),
            before_image=before_image,
            event_index=len(self.events),
            conversion_index=len(self.conversions),
            camera_before=list(self.camera.worldTransform().asTuple()),
            light_before=list(hou.node("/obj/cycles_area_light1").worldTransform().asTuple()),
            light_rotation_before=hou.parm("/obj/null1/ry").eval(),
        )
        with self.entry().session._controller.condition:
            self.case["before_diagnostics_generation"] = (
                self.entry().session._controller.cached.get("diagnostics", {}).get("generation")
            )
        self.case["started"] = time.monotonic()
        hou.setFrame(self.case["to_frame"])
        self.case["set_frame_return_seconds"] = time.monotonic() - self.case["started"]
        QtCore.QTimer.singleShot(
            0, lambda c=self.case: c.setdefault("timeline_event_loop_seconds", time.monotonic() - c["started"])
        )
        self.stage = "trial"

    def finish_case(self, status, reused=False):
        case = self.case
        entry = self.entry()
        case["logical_ack_seconds"] = case.get("logical_ack_seconds", time.monotonic() - case["started"])
        case["retained_physical_image"] = reused
        case["foreground_qualified"] = "foreground_lost_seconds" not in case
        case["main_monitor_qualified"] = "main_monitor_lost_seconds" not in case and self.panel_on_main_monitor()
        assert case["main_monitor_qualified"], "Timed panel left the requested main 4K monitor"
        case["source_delta"] = {
            name: self.source[name] - case["before_source"][name] for name in ("evaluations", "batches", "wire_bytes")
        }
        assert case["source_delta"]["evaluations"] == case["source_delta"]["wire_bytes"] == 0, case
        case["camera_after"] = list(self.camera.worldTransform().asTuple())
        case["light_after"] = list(hou.node("/obj/cycles_area_light1").worldTransform().asTuple())
        case["light_rotation_after"] = hou.parm("/obj/null1/ry").eval()
        if not self.config["freeze_static_lights"]:
            assert case["light_before"] != case["light_after"], case
            assert case["light_rotation_before"] != case["light_rotation_after"], case
            case["animated_light_transform_verified"] = True
        case["status"] = status
        if self.config.get("dlss"):
            assert status["denoiser"] == "dlss", status
        if self.config.get("dlss") and not self.config.get("require_direct_attributes"):
            counts = [
                event.get("direct_attribute_updates") or 0
                for event in self.events[case["event_index"] :]
                if event["kind"] == "QueuedController.accept"
                and event.get("trial") == case["id"]
                and event.get("physical") == status["render_revision"]
            ]
            assert status.get("direct_attribute_updates", 0) == 0 and not any(counts), (status, counts)
            case["dlss_fallback_verified"] = True
        if case["mode"] == "target-assignment":
            geometry = self.wrangle.geometry()
            references = geometry.pointStringAttribValues("instance")
            case["actual_point_assignments"] = dict(Counter(references))
            case["sample_point_references"] = references[:4]
            expected = self.config["count"] // 2
            assert case["actual_point_assignments"] == {self.reference: expected, self.alternative_reference: expected}
            assert references[0] == (self.reference if int(hou.frame()) % 2 == 0 else self.alternative_reference)
            assert geometry.pointIntAttribValues("id") == tuple(range(self.config["count"]))
            assert self.config["form"] == "packed" or geometry.findPointAttrib("name") is None
            case["stable_point_ids"] = True
            assert status["object_resources"] == expected * self.assignment_members, status
            assert status["geometry_conversions"] == case["before_status"]["geometry_conversions"], status
            case["assignment_source_frame"] = 1.0
        case["native_stats"] = ch._native.native_stats(entry.session._handle)
        if self.config.get("rigid_source") and case["mode"] == "source-index":
            expected = self.config["count"] * self.target_members
            combined = self.config.get("require_combined_pose_attributes", False)
            counter = (
                "object_attributes"
                if combined
                else "object_records"
                if self.config["form"] == "attribute"
                else "object_poses"
            )
            log = Path(entry.session.diagnostics()["controller"]["worker_log"])
            profiles = []
            for line in log.read_text(errors="replace").splitlines():
                if "CH_UPDATE_PROFILE " in line:
                    profile = json.loads(line.split("CH_UPDATE_PROFILE ", 1)[1])
                    if profile["generation"] == status["render_revision"] and profile.get(counter) == expected:
                        profiles.append(profile)
            assert profiles, (counter, expected, status)
            if combined:
                assert all(
                    profile.get("object_attribute_poses") == expected
                    and profile.get("object_records") == profile.get("object_poses") == 0
                    for profile in profiles
                ), profiles
            else:
                assert all(profile.get("object_attributes") == 0 for profile in profiles), profiles
            case["rigid_source_apply_proof"] = profiles
            assert not reused and case.get("first_preview_seconds") is not None, case
            assert status["geometry_conversions"] == case["before_status"]["geometry_conversions"], status
            case["rigid_source_fallback_verified"] = not combined
            case["rigid_source_combined_verified"] = combined
        if self.config.get("require_direct_attributes") and case["mode"] == "source-index":
            expected = self.config["count"] * self.target_members
            proof = [
                {key: event.get(key) for key in ("submission", "physical", "direct_attribute_updates")}
                for event in self.events[case["event_index"] :]
                if event["kind"] == "QueuedController.accept"
                and event.get("trial") == case["id"]
                and event.get("physical") == status["render_revision"]
                and event.get("direct_attribute_updates") == expected
            ]
            if status.get("direct_attribute_updates") == expected:
                proof.append(
                    dict(
                        physical=status["render_revision"],
                        direct_attribute_updates=expected,
                        source="current authoritative status",
                    )
                )
            case["direct_attribute_apply_proof"] = proof
            assert proof, (
                "Current physical generation did not use the direct attribute path",
                status,
                case["native_stats"],
            )
        if self.config.get("require_population_reuse") and case["mode"] in ("global-time-unchanged", "camera"):
            counter = "direct_population_reuses" if self.config["form"] == "attribute" else "packed_population_reuses"
            assert case["native_stats"][counter] == 1, (counter, case["native_stats"])
        case["diagnostics"] = entry.session.diagnostics()
        case["registry"] = self.context()
        image = case.pop("first_preview_image", None)
        before = case.pop("before_image")
        if image is not None:
            after = image_values(image)
            case["display_mean_absolute_change"] = float(
                np.mean(np.abs(after.astype(np.int16) - before.astype(np.int16)))
            )
            case["display_sha256"] = hashlib.sha256(after).hexdigest()
            case["nonblank_rgb_range"] = int(after.reshape(54, 96, 4)[:, :, :3].max()) - int(
                after.reshape(54, 96, 4)[:, :, :3].min()
            )
            assert case["nonblank_rgb_range"] > 1, "Blank image cannot satisfy a paint latency trial"
            suffix = (
                ""
                if case["foreground_qualified"]
                else "-foreground-disqualified-" + str(len(self.report.get("disqualified_trials", [])))
            )
            image.save(str(self.path.parent / (self.phase + suffix + "-first-painted.png")))
        if case["mode"] in ("camera", "point-placement", "target-assignment"):
            assert not reused and image is not None and case["display_mean_absolute_change"] > 0.001, case
            if case["mode"] == "camera":
                assert case["camera_before"] != case["camera_after"]
        if self.config.get("rigid_source") and case["mode"] == "source-index":
            assert image is not None and case["display_mean_absolute_change"] > 0.001, case
        case.pop("before_source")
        if not case["foreground_qualified"]:
            case["disqualified_reason"] = "foreground lost during timed trial"
            self.report.setdefault("disqualified_trials", []).append(case)
            retries = self.report.setdefault("foreground_retries", {})
            retries[self.phase] = retries.get(self.phase, 0) + 1
            assert retries[self.phase] <= 2, "Foreground was lost on three attempts of " + self.phase
            self.retry_restore_frame = case["from_frame"]
            self.case = None
            self.stage = "retry-restore"
            self.save()
            return
        self.trials.append(case)
        self.case = None
        self.repetition += 1
        if self.repetition >= self.config["repetitions"]:
            self.mode_index += 1
            self.repetition = 0
            self.stage = "next-mode"
        else:
            self.stage = "rest"
        self.rest_until = time.monotonic() + 0.3
        self.save()

    def tick(self):
        if getattr(self, "busy", False):
            return
        self.busy = True
        try:
            now = time.monotonic()
            if now - self.began > self.config["timeout_seconds"]:
                raise TimeoutError("GUI timeline benchmark exceeded its limit")
            if self.case is None:
                self.contain_panel()
            if self.stage == "initial" and not self.initial_render_clicked and self.panel_on_main_monitor():
                self.initial_render_clicked = True
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
            status = self.cached_status()
            window = self.window_state()
            if self.case is None and now - getattr(self, "last_state_save", 0) >= 2:
                self.last_state_save = now
                self.report["live_state"] = dict(
                    at=now,
                    stage=self.stage,
                    status=status,
                    window=window,
                    registry=self.context(),
                    paint=getattr(self, "last_paint", None),
                    panel=dict(
                        pending=self.panel._pending,
                        render_queued=self.panel._render_queued,
                        in_render=self.panel._in_render,
                        stopped=self.panel._stopped,
                        auto=self.panel.auto.isChecked(),
                        button_enabled=self.panel.render_button.isEnabled(),
                    ),
                )
                self.save()
            if status is None:
                if self.stage == "initial" and now - self.began > 90:
                    raise TimeoutError("No initial live IPR entry within90seconds; inspect live_state")
                return
            latest = getattr(self, "last_paint", {})
            if self.stage == "initial":
                if (
                    latest.get("valid")
                    and self.settled(status)
                    and window["foreground_is_test"]
                    and window["on_main_monitor"]
                ):
                    if not self.nonblank_baseline(status):
                        return
                    self.report["initial_display_options"] = self.panel.display_options()
                    self.report["initial_status"] = status
                    self.phase = "held-visible-baseline"
                    self.report["hold_started"] = now
                    self.hold_until = now + self.config["hold_seconds"]
                    self.stage = "hold"
                    self.save()
                    self.panel.window().grab().save(str(self.path.parent / "held-baseline.png"))
                elif now - self.began > 90:
                    raise TimeoutError("Initial IPR did not reach a foreground current paint; inspect live_state")
            elif (
                self.stage == "hold"
                and now >= self.hold_until
                and window["foreground_is_test"]
                and window["on_main_monitor"]
            ):
                self.configure_mode()
            elif (
                self.stage in ("prepare", "rest")
                and now >= self.rest_until
                and latest.get("valid")
                and self.settled(status)
                and window["foreground_is_test"]
                and window["on_main_monitor"]
            ):
                if self.nonblank_baseline(status):
                    self.start_case(status)
            elif self.stage == "retry-restore" and window["foreground_is_test"] and window["on_main_monitor"]:
                hou.setFrame(self.retry_restore_frame)
                self.rest_until = now + 0.3
                self.stage = "prepare"
            elif self.stage == "next-mode" and now >= self.rest_until:
                if self.mode_index == len(self.modes):
                    self.finish()
                else:
                    self.configure_mode()
            elif self.stage == "trial":
                case = self.case
                if not window["foreground_is_test"]:
                    case.setdefault("foreground_lost_seconds", now - case["started"])
                if not window["on_main_monitor"]:
                    case.setdefault("main_monitor_lost_seconds", now - case["started"])
                    raise RuntimeError("Timed panel left the requested main 4K monitor")
                if self.settled(status) and self.entry().observed_time == hou.time():
                    case.setdefault("logical_ack_seconds", now - case["started"])
                    if (
                        "first_blank_seconds" in case
                        and "first_preview_seconds" not in case
                        and status["state"] == "complete"
                    ):
                        case.setdefault("blank_settled_at", now)
                        if now - case["blank_settled_at"] >= 3:
                            current = self.panel.canvas.image.copy()
                            current.save(str(self.path.parent / (self.phase + "-settled-current.png")))
                            rgb = image_values(current).reshape(54, 96, 4)[:, :, :3]
                            case["settled_current_rgb_range"] = int(rgb.max()) - int(rgb.min())
                            case["settled_current_metadata"] = dict(self.panel.canvas.frame_metadata)
                            case["settled_status"] = status
                            case["native_stats"] = ch._native.native_stats(self.entry().session._handle)
                            raise AssertionError("No useful current Qt paint after three seconds of completed IPR")
                    if (
                        status["render_revision"] == case["before_revision"]
                        and case["mode"] in ("global-time-unchanged", "source-index")
                        and (
                            case["mode"] != "source-index"
                            or status["committed_scene_submission"] > case["before_scene"]
                        )
                        and now - case["started"] >= 0.2
                    ):
                        self.finish_case(status, reused=True)
                    elif "first_fullsize_seconds" in case:
                        self.finish_case(status)
                if self.case is not None and now - case["started"] > 45:
                    raise TimeoutError("No current Qt paint or logical reuse acknowledgment for " + self.phase)
        except Exception:
            self.report.update(result="failed", error=traceback.format_exc())
            self.finish(failed=True)
        finally:
            self.busy = False

    def save(self):
        self.report["phase"] = self.phase
        self.report["event_counts"] = self.event_counts
        self.report["source"] = self.source
        self.report["active_trial"] = {
            key: value for key, value in (self.case or {}).items() if key not in ("before_image", "first_preview_image")
        }
        self.path.write_text(json.dumps(self.report, indent=2, default=str), encoding="utf-8")

    def finish(self, failed=False):
        if getattr(self, "finishing", False):
            return
        self.finishing = True
        self.timer.stop()
        self.report["wall_seconds"] = time.monotonic() - self.began
        if not failed:
            assert self.input_hashes == dict(hip=digest(self.config["hip"]), blend=digest(self.config["blend"]))
            self.report["result"] = "passed"
        self.save()
        self.exit_failed = failed
        hou.ui.postEventCallback(self.cleanup)

    def cleanup(self):
        try:
            self.panel.dispose()
            registry.close_all()
        finally:
            for owner, name, original in reversed(self.restores):
                setattr(owner, name, original)
        # Pane destruction while a Qt paint callback is unwinding is unsafe.
        # Leave pane lifetime to Houdini shutdown from its host callback.
        QtCore.QTimer.singleShot(
            200,
            lambda: hou.ui.postEventCallback(lambda: hou.exit(1 if self.exit_failed else 0, suppress_save_prompt=True)),
        )


workflow = TimelineBenchmark()
