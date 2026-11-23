# SPDX-License-Identifier: Apache-2.0
"""Artist action to generation-correct completed Qt canvas paint.

Run with scripts/run-gui-test.ps1 (isolated preferences and process-tree watchdog).
CH_DISPLAY_REPETITIONS defaults to 5. CH_DISPLAY_CASES selects comma-separated
warm_edit,frame_edit,restart phases after the initial Render. The separate opt-in
session_replacement case injects internal session.close(); it is a transition
diagnostic, not a normal artist action or a comparable restart measurement.
CH_DISPLAY_REFERENCE_MANIFEST optionally
names JSON {domain: "display_rgb_u8", maximum_rmse: 0.01, references: {key: npy}}.
References must be independently rendered, full-size RGB uint8 arrays using the
same camera, display transform, exposure, denoising and scene. RMSE is normalized
to [0,1]. It is a comparable quality metric only with those conditions matched.
No reference is generated from the timed render. Qt paint completion is not a
hardware/compositor scanout timestamp. No artist HIP is loaded or saved.
"""

import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import time
import traceback

import hou
import numpy as np
from PySide6 import QtCore, QtTest
import cycles_houdini as ch
from cycles_houdini import registry
from cycles_houdini.device_selection import set_devices
from support.displayed_frame_metrics import ActionPaints, distribution


def process_memory(pid):
    """Windows process accounting, not physical GPU residency."""
    try:
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t)
                for name in (
                    "PeakWorkingSetSize",
                    "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage",
                    "PagefileUsage",
                    "PeakPagefileUsage",
                    "PrivateUsage",
                )
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        handle = kernel.OpenProcess(0x1000 | 0x10, False, int(pid))
        if not handle:
            raise OSError(ctypes.get_last_error())
        try:
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                raise OSError(ctypes.get_last_error())
            return dict(
                available=True,
                working_set_bytes=counters.WorkingSetSize,
                private_bytes=counters.PrivateUsage,
                peak_working_set_bytes=counters.PeakWorkingSetSize,
            )
        finally:
            kernel.CloseHandle(handle)
    except Exception as error:
        return dict(available=False, reason=str(error))


class Benchmark:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.resolution = tuple(int(v) for v in os.getenv("CH_DISPLAY_RESOLUTION", "640x360").split("x"))
        self.samples = int(os.getenv("CH_DISPLAY_SAMPLES", "16"))
        self.repetitions = int(os.getenv("CH_DISPLAY_REPETITIONS", "5"))
        assert min(self.resolution) > 0 and self.samples > 0 and self.repetitions > 0
        self.reference_manifest = None
        self.references = {}
        reference_files = {}
        reference_path = os.getenv("CH_DISPLAY_REFERENCE_MANIFEST")
        if reference_path:
            manifest_path = Path(reference_path).resolve()
            self.reference_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            assert self.reference_manifest["domain"] == "display_rgb_u8"
            for key, filename in self.reference_manifest["references"].items():
                reference_file = Path(filename)
                if not reference_file.is_absolute():
                    reference_file = manifest_path.parent / reference_file
                reference = np.load(reference_file, allow_pickle=False)
                assert reference.dtype == np.uint8 and reference.shape == (self.resolution[1], self.resolution[0], 3)
                self.references[key] = reference
                reference_files[key] = dict(
                    path=str(reference_file), sha256=hashlib.sha256(reference_file.read_bytes()).hexdigest()
                )
        self.sessions = []  # Retain Python identities; id reuse cannot alias a retired session.
        requested_device = os.environ.get("CYCLES_HOUDINI_GUI_DEVICE", "CPU")
        devices = ch.devices()
        self.device = next((device["id"] for device in devices if device["id"] == requested_device), None)
        if self.device is None and requested_device in ("CUDA", "OPTIX"):
            self.device = next((device["id"] for device in devices if device["backend"] == requested_device), None)
        assert self.device is not None, (requested_device, devices)
        hou.setFrame(1)
        self.obj = hou.node("/obj").createNode("geo", "display_benchmark_subject", run_init_scripts=False)
        sphere = self.obj.createNode("sphere")
        sphere.parm("type").set("polymesh")
        sphere.setDisplayFlag(True)
        sphere.setRenderFlag(True)
        self.obj.parm("tx").setExpression("0.35*sin($F*37)", hou.exprLanguage.Hscript)
        self.camera = hou.node("/obj").createNode("cam", "display_benchmark_camera")
        self.camera.parm("tz").set(7)
        self.rop = hou.node("/out").createNode("cycles_ipr", "display_benchmark_render")
        set_devices(self.rop, self.device)
        self.rop.setParms(
            dict(
                camera=self.camera.path(),
                resx=self.resolution[0],
                resy=self.resolution[1],
                ipr_samples=self.samples,
                ipr_adaptive_sampling=0,
                ipr_use_denoise=0,
                allowmotionblur=0,
                world_source="color",
            )
        )
        self.rop.parmTuple("background").set((0.3, 0.3, 0.3))
        self.report = dict(
            result="running",
            schema=1,
            scope=__doc__,
            cases=[],
            diagnostics=[],
            instrumentation=[
                dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                for path in (Path(__file__), Path(__file__).parent / "support/displayed_frame_metrics.py")
            ],
            runtime_identity=json.loads(os.environ.get("CYCLES_HOUDINI_TEST_IDENTITY", "{}")),
            measurement=dict(
                clock="time.perf_counter",
                start="immediately before artist API action / Render click",
                finish="canvas.framePainted at completed Qt paint; no compositor/scanout guarantee",
                heartbeat_interval_ms=5,
                sample_counts_comparable_between_renderers=False,
            ),
            fixture=dict(
                resolution=self.resolution,
                samples=self.samples,
                denoising=False,
                adaptive_sampling=False,
                scene="generated sphere, fixed camera, constant environment",
                saved=False,
            ),
            cache_conditions=dict(
                preferences=os.environ.get("HOUDINI_USER_PREF_DIR"),
                cold_startup="first Render in this new isolated Houdini process; excludes Houdini launch",
                warm_edit="same renderer process; object transform edit",
                restart="Stop then Render; resident session may be reused",
                session_replacement="opt-in internal close injection diagnostic; not a normal artist action",
                os_filesystem_cache="uncontrolled",
                gpu_driver_kernel_cache="uncontrolled",
                asset_cache=os.environ.get("CYCLES_HOUDINI_ASSET_CACHE", "isolated preferences default"),
            ),
            hardware=dict(
                platform=platform.platform(),
                devices=devices,
                requested_device=requested_device,
                selected_device=self.device,
                backend_alias_selection="first enumerated matching backend",
            ),
            capabilities=ch.capabilities(),
            quality=dict(
                available=bool(reference_path),
                reference_manifest=reference_path,
                domain="display-referred 8-bit RGB, normalized RMSE; no raw radiometric quality claim",
                reference_files=reference_files,
                reference_loading="before all timed actions",
                timestamp="qualifying completed-paint timestamp, before RGB comparison",
                overhead="RGB comparison time reported separately; may delay subsequent paints",
                reason=None if reference_path else "No independently supplied display RGB reference/threshold",
            ),
            unavailable_metrics=dict(
                compositor_scanout="No hardware presentation timestamp available",
                total_gpu_residency="Session allocations are not total GPU residency",
                karma_presentation="Karma IPR has no compatible generation-tagged framePainted hook in this harness; husk internal times must be reported separately",
            ),
        )
        phase_kinds = os.getenv("CH_DISPLAY_CASES", "warm_edit,frame_edit,restart").split(",")
        assert phase_kinds and set(phase_kinds).issubset({"warm_edit", "frame_edit", "restart", "session_replacement"})
        self.plan = [("cold_startup", 0)] + [
            (kind, repeat) for repeat in range(self.repetitions) for kind in phase_kinds
        ]
        self.report["requested_phase_kinds"] = phase_kinds
        self.report["unavailable_metrics"]["artist_session_replacement"] = (
            "Not measured: internal session.close injection was inconclusive in a repeated sequence. "
            "A future artist replacement case must use a normal user action such as switching ROPs."
        )
        self.panel = None
        self.case = self.meter = None
        self.preparing = None
        self.error = None
        self.finished = False
        self.started = self.last_beat = time.perf_counter()
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.timer.setInterval(5)
        self.timer.timeout.connect(self.beat)
        self.timer.start()
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(950, 700))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        hou.ui.addEventLoopCallback(self.tick)
        self.save()

    def token(self, session):
        if session is None:
            return None
        for index, candidate in enumerate(self.sessions):
            if session is candidate:
                return index + 1
        self.sessions.append(session)
        return len(self.sessions)

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2, default=str), encoding="utf-8")

    def beat(self):
        now = time.perf_counter()
        if self.case is not None:
            self.case["ui_heartbeat_gaps_seconds"].append(now - self.last_beat)
        self.last_beat = now

    def begin(self, prepared=None):
        if prepared is None:
            kind, repeat = self.plan.pop(0)
            before = dict(getattr(self.panel.canvas, "frame_metadata", {}))
            old = self.panel.entry.session if self.panel.entry else None
            setup_started = time.perf_counter()
            if kind in ("restart", "session_replacement"):
                # Stop is asynchronous. A click on its temporarily disabled
                # Render button would silently do nothing. Wait in HOM's loop.
                QtTest.QTest.mouseClick(self.panel.stop_button, QtCore.Qt.LeftButton)
                if kind == "session_replacement":
                    old.close()
                self.preparing = (kind, repeat, before, old, setup_started)
                return
        else:
            kind, repeat, before, old, setup_started = prepared
            self.preparing = None
        reference_key = f"{kind}:{repeat % 2}"
        self.case = dict(
            kind=kind,
            repetition=repeat,
            reference_key=reference_key,
            preparation_seconds=time.perf_counter() - setup_started,
            before_session=self.token(old),
            before_frame=before,
            ui_heartbeat_gaps_seconds=[],
            quality_check_seconds=0.0,
            state_transitions=[],
        )
        if kind == "session_replacement":
            self.case["scope"] = "internal session.close injection diagnostic; excluded from primary timing summaries"
        started = time.perf_counter()
        self.last_beat = started
        self.meter = ActionPaints(
            started,
            self.token(old),
            before,
            self.resolution,
            allow_replacement=kind in ("cold_startup", "session_replacement"),
        )
        self.case["artist_action_perf_counter"] = started
        if kind == "warm_edit":
            self.obj.parm("ty").set(0.2 if repeat % 2 == 0 else -0.2)
        elif kind == "frame_edit":
            hou.setFrame(8 if repeat % 2 == 0 else 1)
        else:
            QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
        self.case["artist_action_call_seconds"] = time.perf_counter() - started

    def painted(self, generation):
        if self.case is None or self.finished:
            return
        try:
            now = time.perf_counter()  # Timestamp before status/pixel inspection.
            session = self.panel.entry.session if self.panel.entry else None
            if session is None:
                return
            status = session._controller.status()  # Cached; never pump a HOM capture in paint callbacks.
            metadata = dict(self.panel.canvas.frame_metadata)
            accepted = self.meter.observe(
                now,
                self.token(session),
                generation,
                metadata,
                status,
                self.panel._display_epoch,
                visible=self.panel.canvas.isVisible(),
            )
            if not accepted or self.reference_manifest is None or self.meter.first_full_resolution_sample is None:
                return
            if "fixed_quality_seconds" in self.case:
                return
            reference_key = self.case["reference_key"]
            if self.panel.canvas.display_mode == "gpu":
                self.case["fixed_quality_unavailable"] = (
                    "GL framebuffer RGB comparison is unsupported; QImage backing alone does not certify GL output"
                )
                return
            if reference_key not in self.references:
                self.case["fixed_quality_unavailable"] = "No independent reference for " + reference_key
                return
            if (metadata.get("rendered_width"), metadata.get("rendered_height")) != self.resolution:
                return
            check_started = time.perf_counter()
            reference = self.references[reference_key]
            image = getattr(self.panel.canvas, "image", None)
            live_image = getattr(self.panel, "_live_image", None)
            if image is None or image.isNull() or image.depth() != 32 or live_image is None:
                self.case["fixed_quality_unavailable"] = "No compatible current 32-bit displayed QImage"
                return
            if image.cacheKey() != live_image.cacheKey() or not self.panel.displaying_live():
                self.case["fixed_quality_unavailable"] = (
                    "Displayed QImage is retained/reference content rather than current live pixels"
                )
                return
            # QImage formats may be BGRA in memory; QColor-independent conversion is explicit.
            from PySide6 import QtGui

            converted = image.convertToFormat(QtGui.QImage.Format_RGBA8888)
            rgba = np.ndarray(
                (converted.height(), converted.bytesPerLine() // 4, 4), np.uint8, buffer=converted.constBits()
            )
            rgb = rgba[:, : converted.width(), :3]
            assert reference.dtype == np.uint8 and reference.shape == rgb.shape
            rmse = float(np.sqrt(np.mean(((rgb.astype(np.float32) - reference) / 255.0) ** 2)))
            self.case["quality_check_seconds"] += time.perf_counter() - check_started
            self.case["last_display_rgb_rmse"] = rmse
            if rmse <= self.reference_manifest["maximum_rmse"]:
                self.case["fixed_quality_seconds"] = now - self.meter.started
        except Exception:
            self.error = traceback.format_exc()

    def tick(self):
        if self.finished:
            return
        try:
            if self.error:
                raise RuntimeError(self.error)
            assert time.perf_counter() - self.started < 240, "Whole benchmark watchdog"
            if self.panel is None:
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.rop)
                self.panel.auto.setChecked(True)
                self.panel.canvas.framePainted.connect(self.painted)
                self.report["display"] = dict(
                    mode=self.panel.canvas.display_mode, canvas_class=type(self.panel.canvas).__name__
                )
                self.begin()
                return
            if self.case is None:
                if self.preparing is not None:
                    if time.perf_counter() - self.preparing[-1] >= 15:
                        if self.preparing[0] == "session_replacement":
                            self.finish(
                                diagnostic_reason="Internal close injection did not finish retirement preparation within 15 seconds"
                            )
                            return
                        raise TimeoutError("Stop acknowledgement preparation watchdog")
                    retired = (
                        self.preparing[0] != "session_replacement"
                        or self.preparing[3]._controller.status()["state"] == "closed"
                    )
                    if retired and self.panel.render_button.isEnabled():
                        self.begin(self.preparing)
                elif self.plan:
                    self.begin()
                else:
                    self.finish()
                return
            if time.perf_counter() - self.meter.started >= 40:
                if self.case["kind"] == "session_replacement":
                    self.finish(
                        diagnostic_reason="Internal close injection produced no attributable replacement paint within 40 seconds"
                    )
                    return
                raise TimeoutError(self.case["kind"] + " action watchdog")
            entry = self.panel.entry
            if entry is None:
                return
            status = entry.session.status()
            transition = dict(
                session=self.token(entry.session),
                state=status["state"],
                generation=status.get("generation"),
                render_revision=status.get("render_revision"),
                pending_update=status.get("pending_update"),
                entry_error=entry.error,
            )
            if not self.case["state_transitions"] or transition != self.case["state_transitions"][-1]["state"]:
                if len(self.case["state_transitions"]) < 32:
                    self.case["state_transitions"].append(
                        dict(seconds=time.perf_counter() - self.meter.started, state=transition)
                    )
            if (
                self.case["kind"] == "session_replacement"
                and self.token(entry.session) == self.case["before_session"]
                and status["state"] == "closed"
            ):
                return  # Queued Render has not replaced the explicitly retired old session yet.
            assert status["state"] != "failed" and not entry.error and not status.get("error"), status
            full = self.meter.first_full_resolution_sample
            quality_ready = (
                self.reference_manifest is None
                or "fixed_quality_seconds" in self.case
                or "fixed_quality_unavailable" in self.case
            )
            displayed = getattr(self.panel.canvas, "frame_metadata", {})
            if (
                full is None
                or not quality_ready
                or status.get("pending_update")
                or status["state"] != "complete"
                or displayed.get("samples", 0) < self.samples
            ):
                return
            self.case.update(
                first_visible=self.meter.first_visible,
                first_full_resolution_sample=full,
                rejected_paints=self.meter.rejections,
                settled_seconds=time.perf_counter() - self.meter.started,
                final_status=status,
                host_memory=process_memory(os.getpid()),
                renderer_memory=process_memory(status["renderer_pid"]),
            )
            gaps = self.case.pop("ui_heartbeat_gaps_seconds")
            gaps.append(time.perf_counter() - self.last_beat)
            self.case["ui_heartbeat_gap_seconds"] = distribution(gaps)
            target = "diagnostics" if self.case["kind"] == "session_replacement" else "cases"
            self.report[target].append(self.case)
            self.case = self.meter = None
            self.save()
        except Exception:
            self.finish(traceback.format_exc())

    def finish(self, error=None, diagnostic_reason=None):
        if self.finished:
            return
        self.finished = True
        self.timer.stop()
        if self.tick in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.tick)
        for kind in dict.fromkeys(case["kind"] for case in self.report["cases"]):
            cases = [case for case in self.report["cases"] if case["kind"] == kind]
            self.report.setdefault("summary_seconds", {})[kind] = dict(
                first_visible=distribution(case["first_visible"]["seconds"] for case in cases),
                first_full_resolution_sample=distribution(
                    case["first_full_resolution_sample"]["seconds"] for case in cases
                ),
                fixed_quality=distribution(case.get("fixed_quality_seconds") for case in cases),
                largest_ui_gap=distribution(case["ui_heartbeat_gap_seconds"]["maximum"] for case in cases),
            )
        self.report.update(
            result="inconclusive" if diagnostic_reason else "failed" if error else "passed",
            error=error,
            diagnostic_reason=diagnostic_reason,
            wall_seconds=time.perf_counter() - self.started,
        )
        if self.case is not None:
            self.case["ui_heartbeat_gap_seconds"] = distribution(self.case.pop("ui_heartbeat_gaps_seconds"))
            self.case["rejected_paints"] = self.meter.rejections
            self.case["first_visible"] = self.meter.first_visible
            self.case["first_full_resolution_sample"] = self.meter.first_full_resolution_sample
            if self.panel.entry:
                self.case["final_status"] = self.panel.entry.session._controller.status()
            target = "unfinished_diagnostic" if self.case["kind"] == "session_replacement" else "unfinished_case"
            self.report[target] = self.case
        if self.panel is not None and not error and not diagnostic_reason:
            capture = self.path.with_suffix(".canvas.png")
            self.report["final_canvas_capture"] = dict(
                path=str(capture),
                saved=self.panel.canvas.grab().save(str(capture)),
                scope="Qt canvas capture after all timed cases; no hardware/compositor presentation claim",
                metadata=dict(getattr(self.panel.canvas, "frame_metadata", {})),
            )
        self.save()
        if self.panel is not None:
            self.panel.dispose()
        registry.close_all()
        hou.exit(1 if error else 0, suppress_save_prompt=True)


if __name__ == "__main__":
    benchmark = Benchmark()
