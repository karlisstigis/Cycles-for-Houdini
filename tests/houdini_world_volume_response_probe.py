# SPDX-License-Identifier: Apache-2.0
"""Measure world-volume IPR cost and GUI responsiveness in the artist scene.

Read-only diagnostic: loads the artist scene (never saves it), renders the
Cycles viewer panel with the scene's own Test_2 settings, then repeats with a
default Principled Volume plugged into the world and with one setting changed
at a time. Each configuration renders for a fixed window and records:

- sample rate and time to the first image,
- GUI event-loop latency (an 8 ms heartbeat; gaps are UI stalls),
- time spent in the panel's own poll/present code on the GUI thread,
- CPU use of the render worker and of Houdini, GPU utilization (nvidia-smi).
"""

from support.paths import artifacts_root

from collections import defaultdict
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import traceback

import hou
import numpy as np
from PySide6 import QtCore
import cycles_houdini as ch
from cycles_houdini import device_selection, native_scene, registry
from cycles_houdini.panel import RenderPanel

WINDOW_SECONDS = 15.0
STALL_SECONDS = 0.05
CORES = os.cpu_count() or 1


class ProcessClock:
    """CPU seconds and priority of one process (Windows)."""

    def __init__(self, pid):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
        kernel.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.GetPriorityClass.argtypes = [wintypes.HANDLE]
        self.kernel, self.pid = kernel, pid
        # PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_INFORMATION
        self.handle = kernel.OpenProcess(0x1000 | 0x0200, False, pid)

    def cpu_seconds(self):
        values = [ctypes.c_ulonglong() for _ in range(4)]
        if not self.handle or not self.kernel.GetProcessTimes(self.handle, *(ctypes.byref(v) for v in values)):
            return None
        return (values[2].value + values[3].value) / 1e7

    def set_priority(self, below_normal):
        return bool(self.handle and self.kernel.SetPriorityClass(self.handle, 0x4000 if below_normal else 0x20))

    def priority(self):
        return self.kernel.GetPriorityClass(self.handle) if self.handle else None


class GpuMonitor:
    def __init__(self):
        self.samples, self.process, self.error = [], None, None
        executable = shutil.which("nvidia-smi") or r"C:\Windows\System32\nvidia-smi.exe"
        try:
            self.process = subprocess.Popen(
                [
                    executable,
                    "--query-gpu=index,name,utilization.gpu,memory.used",
                    "--format=csv,noheader,nounits",
                    "-lms",
                    "250",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                creationflags=0x08000000,
            )
        except OSError as error:
            self.error = str(error)
            return
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.process.stdout:
            parts = [part.strip() for part in line.split(",")]
            if len(parts) == 4:
                try:
                    self.samples.append((time.perf_counter(), parts[1], float(parts[2]), float(parts[3])))
                except ValueError:
                    pass

    def summary(self, start, end):
        rows = defaultdict(list)
        for at, name, utilization, memory in tuple(self.samples):
            if start <= at <= end:
                rows[name].append((utilization, memory))
        return {
            name: dict(
                mean_utilization=round(float(np.mean([v[0] for v in values])), 1),
                max_memory_mib=float(max(v[1] for v in values)),
                samples=len(values),
            )
            for name, values in rows.items()
        } or self.error

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.kill()


def summarize(values):
    if not values:
        return None
    values = np.array(values)
    return dict(
        count=int(values.size),
        total_ms=round(float(values.sum()) * 1000, 1),
        p95_ms=round(float(np.quantile(values, 0.95)) * 1000, 2),
        max_ms=round(float(values.max()) * 1000, 2),
    )


class Probe:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.source = Path(
            os.environ.get(
                "CYCLES_HOUDINI_PROFILE_SCENE",
                str(artifacts_root() / "examples/sop-fx-courtyard/Test.hiplc"),
            )
        )
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        hou.hipFile.load(self.source.as_posix(), suppress_save_prompt=True, ignore_load_warnings=True)
        self.rop = hou.node("/out/Test_2")
        if os.environ.get("CYCLES_HOUDINI_PROBE_SET") == "synthetic":
            # Independent scene-complexity control, matching the simple Blender
            # probe's layout. Retain the same worker/viewer presentation path.
            import math

            root = hou.node("/obj").createNode("subnet", "world_volume_control")
            camera = root.createNode("cam")
            camera.setParms(dict(ty=-8, tz=5, rx=math.degrees(math.atan2(8, 5))))
            geo = root.createNode("geo", run_init_scripts=False)
            floor = geo.createNode("grid")
            floor.setParms(dict(type=0, orient=0, rows=2, cols=2))
            floor.parmTuple("size").set((20, 20))
            for x in range(-2, 3):
                for y in range(-2, 3):
                    geo = root.createNode("geo", run_init_scripts=False)
                    box = geo.createNode("box")
                    geo.setParms(dict(tx=x * 2, ty=y * 2, tz=0.5))
                    light = root.createNode("hlight::2.0")
                    light.setParms(dict(light_type="point", tx=x * 2 + 0.5, ty=y * 2, tz=2, light_intensity=80))
            simple_world = ch.create_world_material(name="world_volume_control")
            simple_world.node("background_shader").parmTuple("color").set((0.05, 0.05, 0.05))
            self.rop.setParms(
                dict(
                    objectroot=root.path(),
                    vobject="*",
                    lights="*",
                    camera=camera.path(),
                    world_material=simple_world.path(),
                    resx=1440,
                    resy=810,
                    resolution_percent=100,
                    ipr_resolution_percent=0,
                    source_mode="objects",
                    use_material_assignments=0,
                    material_kind="diffuse",
                    volume_bounce=1,
                    viewer_interactive=1,
                    ipr_samples=256,
                )
            )
        self.device = os.environ["CYCLES_HOUDINI_GUI_DEVICE"]
        device_selection.set_devices(self.rop, [self.device])
        # The scene's world must be the authored material world.
        self.rop.parm("world_source").set("material")
        self.world = hou.node(self.rop.evalParm("world_material"))
        self.output = next(child for child in self.world.children() if child.type().name() == "cycles::world_output::1")
        self.volume = self.world.createNode("cycles::principled_volume::1", "probe_world_volume")
        self.base = {
            name: self.rop.parm(name).eval()
            for name in (
                "viewer_interactive",
                "samples",
                "use_denoise",
                "denoise_use_gpu",
                "volume_bounce",
                "ipr_use_denoise",
                "world_volume_sampling",
                "ipr_adaptive_sampling",
                "denoise_start_sample",
                "render_threads",
            )
        }
        others = [d["id"] for d in ch.devices() if d["backend"] == "OPTIX" and d["id"] != self.device]
        interactive_final = dict(viewer_interactive=0, samples=4096, use_denoise=1, denoise_use_gpu=0)
        self.configs = [
            dict(name="ipr_no_volume", volume=False),
            dict(name="ipr_volume_equiangular", volume=True),
            dict(name="ipr_volume_distance", volume=True, parms=dict(world_volume_sampling="distance")),
            dict(name="ipr_volume_bounce0", volume=True, parms=dict(volume_bounce=0)),
            dict(name="ipr_volume_no_denoise", volume=True, parms=dict(ipr_use_denoise=0)),
            dict(name="final_volume_cpu_denoise", volume=True, parms=interactive_final),
            dict(
                name="final_volume_cpu_denoise_worker_below_normal",
                volume=True,
                parms=interactive_final,
                below_normal=True,
            ),
            dict(name="final_volume_gpu_denoise", volume=True, parms=dict(interactive_final, denoise_use_gpu=1)),
            dict(name="final_no_volume_cpu_denoise", volume=False, parms=interactive_final),
        ]
        if others:
            self.configs.append(dict(name="ipr_volume_other_gpu", volume=True, device=others[0]))
        if os.environ.get("CYCLES_HOUDINI_PROBE_SET") == "profile":
            self.configs = [
                dict(name="profile_equiangular", volume=True, parms=dict(ipr_adaptive_sampling=0, ipr_use_denoise=0))
            ]
        elif os.environ.get("CYCLES_HOUDINI_PROBE_SET") == "navigation":
            global WINDOW_SECONDS
            WINDOW_SECONDS = 90.0
            self.configs = [
                dict(
                    name="mouse_navigation_equiangular",
                    volume=True,
                    parms=dict(ipr_adaptive_sampling=0, ipr_use_denoise=0, ipr_samples=100000),
                )
            ]
            self.navigation_changes = []
            self.navigation_transform = None
            obj = hou.node("/obj/toy")
            obj.setCurrent(True, clear_all_selected=True)
            viewer = hou.ui.paneTabOfType(hou.paneTabType.SceneViewer)
            viewer.setPwd(hou.node("/obj"))
            viewer.curViewport().setCamera(self.rop.parm("camera").evalAsNode())
            viewer.enterCurrentNodeState()
            hou.qt.mainWindow().setWindowTitle("Cycles world volume navigation regression")
            hou.qt.mainWindow().showMaximized()
        elif os.environ.get("CYCLES_HOUDINI_PROBE_SET") == "certify":
            common = dict(ipr_adaptive_sampling=0, ipr_use_denoise=0)
            self.configs = [
                dict(name="equiangular_cold", volume=True, parms=common),
                dict(name="equiangular_warm", volume=True, parms=common),
                dict(name="distance", volume=True, parms=dict(common, world_volume_sampling="distance")),
                dict(
                    name="multiple_importance",
                    volume=True,
                    parms=dict(common, world_volume_sampling="multiple_importance"),
                ),
                dict(name="equiangular_return", volume=True, parms=common),
            ]
        elif os.environ.get("CYCLES_HOUDINI_PROBE_SET") == "density":
            common = dict(ipr_adaptive_sampling=0, ipr_use_denoise=0)
            self.configs = [
                dict(name="density_" + str(density), volume=True, parms=common, density=density)
                for density in (1.0, 0.01, 0.1, 1.0, 40.0)
            ]
        elif os.environ.get("CYCLES_HOUDINI_PROBE_SET") == "shadow":
            common = dict(ipr_adaptive_sampling=0, ipr_use_denoise=0)
            self.configs = [
                dict(name="no_motion", volume=True, parms=dict(common, allowmotionblur=0)),
                dict(name="world_light_only", volume=True, parms=dict(common, lights="")),
                dict(name="one_instance", volume=True, parms=common, instances=1),
                dict(name="ten_instances", volume=True, parms=common, instances=10),
                dict(name="all_instances", volume=True, parms=common, instances=79),
            ]
            for name in ("allowmotionblur", "lights"):
                self.base[name] = self.rop.evalParm(name)
            obj = hou.node("/obj/toy")
            source = obj.renderNode()
            self.instance_blast = obj.createNode("blast", "probe_instance_count")
            self.instance_blast.setInput(0, source)
            self.instance_blast.setParms(dict(group="0-78", negate=1))
            self.instance_blast.setRenderFlag(True)
        elif os.environ.get("CYCLES_HOUDINI_PROBE_SET") == "kernels":
            # GPU work granularity (Cycles' concurrent path states) against UI
            # stalls, with sampling that runs for the whole window; and the
            # Final viewer mode when the CPU denoiser runs only at the end.
            # The factor is read when a worker starts. Render Threads is part of
            # the session identity (CPU-side only for a GPU render): a distinct
            # value starts a fresh worker through the normal viewer path.
            def continuous(threads):
                return dict(ipr_adaptive_sampling=0, render_threads=threads)

            end_only = dict(interactive_final, denoise_start_sample=4096)
            self.configs = [
                dict(name="ipr_volume_states_default", volume=True, parms=continuous(0), factor=None),
                dict(name="ipr_volume_states_0.25", volume=True, parms=continuous(31), factor="0.25"),
                dict(name="ipr_volume_states_0.0625", volume=True, parms=continuous(30), factor="0.0625"),
                dict(name="ipr_no_volume_states_default", volume=False, parms=continuous(29), factor=None),
                dict(name="ipr_no_volume_states_0.25", volume=False, parms=continuous(28), factor="0.25"),
                dict(name="final_no_volume_cpu_denoise_at_end", volume=False, parms=end_only, factor=None),
                dict(name="final_volume_cpu_denoise_at_end", volume=True, parms=end_only),
            ]
        elif os.environ.get("CYCLES_HOUDINI_PROBE_SET") == "isolate":
            # Hold resolution, denoising and adaptive sampling constant while
            # isolating world-volume integration and light sampling. Changes
            # stay in this disposable Houdini process; never save the source.
            common = dict(ipr_adaptive_sampling=0, ipr_use_denoise=0)
            self.configs = [
                dict(name="no_volume_control", volume=False, parms=common),
                dict(name="volume_equiangular", volume=True, parms=common),
                dict(name="volume_ray_marching", volume=True, parms=dict(common, volume_biased=1)),
                dict(name="volume_no_light_tree", volume=True, parms=dict(common, light_tree=0)),
                dict(name="volume_zero_bounces", volume=True, parms=dict(common, volume_bounce=0)),
                dict(name="volume_distance", volume=True, parms=dict(common, world_volume_sampling="distance")),
                dict(name="volume_equiangular_repeat", volume=True, parms=common),
            ]
            # Restore every experimental parameter between variants.
            for name in ("volume_biased", "light_tree"):
                self.base[name] = self.rop.evalParm(name)
        elif os.environ.get("CYCLES_HOUDINI_PROBE_SET") == "synthetic":
            common = dict(ipr_adaptive_sampling=0, ipr_use_denoise=0)
            self.configs = [
                dict(name="synthetic_equiangular", volume=True, parms=common),
                dict(name="synthetic_distance", volume=True, parms=dict(common, world_volume_sampling="distance")),
                dict(name="synthetic_equiangular_repeat", volume=True, parms=common),
            ]
        elif os.environ.get("CYCLES_HOUDINI_PROBE_SET") == "content":
            common = dict(ipr_adaptive_sampling=0, ipr_use_denoise=0)
            self.configs = [
                dict(name="authored_control", volume=True, parms=common),
                dict(name="lights_world_only", volume=True, parms=dict(common, vobject="")),
                dict(
                    name="uniform_surface_material",
                    volume=True,
                    parms=dict(common, use_material_assignments=0, material_kind="diffuse"),
                ),
                dict(name="authored_repeat", volume=True, parms=common),
            ]
            for name in ("vobject", "use_material_assignments", "material_kind"):
                self.base[name] = self.rop.evalParm(name)
        elif os.environ.get("CYCLES_HOUDINI_PROBE_SET") == "components":
            common = dict(ipr_adaptive_sampling=0, ipr_use_denoise=0)
            self.configs = [
                dict(name="packed_toys_only", volume=True, parms=dict(common, vobject="toy")),
                dict(name="ground_only", volume=True, parms=dict(common, vobject="circle1")),
                dict(name="both_components", volume=True, parms=common),
            ]
            self.base["vobject"] = self.rop.evalParm("vobject")
        self.panel = None
        self.gpu = GpuMonitor()
        self.houdini = ProcessClock(os.getpid())
        self.report = dict(
            result="running",
            device=self.device,
            source=str(self.source),
            source_sha256=self.digest,
            cores=CORES,
            window_seconds=WINDOW_SECONDS,
            scene_settings=self.base,
            runs=[],
        )
        self.timings = defaultdict(list)
        self.restores = []

        def wrap(owner, name, label):
            original = getattr(owner, name)

            def measured(*args, **kwargs):
                began = time.perf_counter()
                try:
                    return original(*args, **kwargs)
                finally:
                    self.timings[label].append(time.perf_counter() - began)

            setattr(owner, name, measured)
            self.restores.append((owner, name, original))

        for owner, name in (
            (RenderPanel, "poll"),
            (RenderPanel, "present_available"),
            (native_scene, "capture"),
            (registry.Entry, "apply"),
        ):
            wrap(owner, name, owner.__name__ + "." + name)
        # Create the panel after wrapping: its timer binds poll at construction.
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(900, 650))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.index = -1
        self.run = None
        self.started = time.monotonic()
        self.busy = False
        self.gaps = []
        self.heartbeat = QtCore.QTimer()
        self.heartbeat.setTimerType(QtCore.Qt.PreciseTimer)
        self.heartbeat.setInterval(8)
        self.heartbeat.timeout.connect(self.beat)
        self.last_beat = time.perf_counter()
        self.heartbeat.start()
        hou.ui.addEventLoopCallback(self.tick)

    def beat(self):
        now = time.perf_counter()
        self.gaps.append((now, now - self.last_beat))
        self.last_beat = now

    def configure(self, config):
        parms = dict(self.base)
        parms.update(config.get("parms", {}))
        if "instances" in config:
            self.instance_blast.parm("group").set("0-" + str(config["instances"] - 1))
        self.rop.setParms(parms)
        device_selection.set_devices(self.rop, [config.get("device", self.device)])
        if "density" in config:
            self.volume.parm("density").set(config["density"])
        index = self.output.inputNames().index("volume")
        if config["volume"]:
            self.output.setNamedInput("volume", self.volume, "volume")
        else:
            self.output.setInput(index, None)

    def start(self):
        self.index += 1
        if self.index >= len(self.configs):
            self.finish()
            return
        config = self.configs[self.index]
        session = self.panel.entry.session if self.panel.entry else None
        before = session.status().get("generation", 0) if session else 0
        if session:
            self.panel.cancel()
        if "factor" in config:
            # Read when the worker creates its GPU queue: start a new worker.
            if config["factor"] is None:
                os.environ.pop("CYCLES_CONCURRENT_STATES_FACTOR", None)
            else:
                os.environ["CYCLES_CONCURRENT_STATES_FACTOR"] = config["factor"]
        self.configure(config)
        self.timings.clear()
        self.panel.queue_render()
        self.run = dict(
            config=config,
            requested=time.perf_counter(),
            before=before,
            session=session,
            began=None,
            first_frame=None,
            samples=[],
            worker=None,
            worker_cpu=None,
            houdini_cpu=None,
            status=None,
        )

    def tick(self):
        if self.busy:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.started < 600, "World volume probe timed out"
            if self.panel is None:
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.rop)
                self.start()
                return
            run, entry = self.run, self.panel.entry
            if run is None or entry is None:
                return
            status = entry.session.status()
            assert not entry.error and status["state"] != "failed", (entry.error, status)
            now = time.perf_counter()
            if hasattr(self, "navigation_changes"):
                transform = tuple(hou.node("/obj/toy").worldTransform().asTuple())
                if transform != self.navigation_transform:
                    self.navigation_changes.append(
                        dict(seconds=now - run["requested"], transform=transform, generation=status["generation"])
                    )
                    self.navigation_transform = transform
            if run["began"] is None:
                # A device or resolution change replaces the session (and worker).
                newer = entry.session is not run["session"] or status["generation"] > run["before"]
                if newer and status["state"] in ("rendering", "complete") and not status.get("pending_update"):
                    run["began"] = now
                    run["worker"] = ProcessClock(status["renderer_pid"])
                    run["worker_cpu"] = run["worker"].cpu_seconds()
                    run["houdini_cpu"] = self.houdini.cpu_seconds()
                    if run["config"].get("below_normal"):
                        run["priority_set"] = run["worker"].set_priority(True)
                elif now - run["requested"] > 120:
                    raise AssertionError("Render did not start: " + json.dumps(status, default=str)[:2000])
                return
            frame = entry.session.frame_info()
            if frame and frame["generation"] == status["generation"]:
                if run["first_frame"] is None:
                    run["first_frame"] = now - run["requested"]
                sample = status.get("current_sample", 0)
                if not run["samples"] or run["samples"][-1][1] != sample:
                    run["samples"].append((now, sample))
            if status["state"] == "complete" and "complete" not in run:
                run["complete"] = now - run["began"]
            run["status"] = status
            if now - run["began"] >= WINDOW_SECONDS or ("complete" in run and now - run["began"] > 1):
                self.record(now)
                self.start()
        except Exception:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

    def record(self, now):
        run = self.run
        began, wall = run["began"], now - run["began"]
        gaps = [gap for at, gap in self.gaps if began <= at <= now]
        stalls = [gap for gap in gaps if gap > STALL_SECONDS]
        samples = run["samples"]
        rate = None
        if len(samples) >= 2 and samples[-1][0] > samples[0][0]:
            rate = (samples[-1][1] - samples[0][1]) / (samples[-1][0] - samples[0][0])
        worker_cpu, houdini_cpu = run["worker"].cpu_seconds(), self.houdini.cpu_seconds()
        status = run["status"] or {}
        rendering = status.get("rendering", {})
        result = dict(
            name=run["config"]["name"],
            device=run["config"].get("device", self.device),
            volume=run["config"]["volume"],
            parms=run["config"].get("parms", {}),
            seconds=round(wall, 2),
            first_image_seconds=None if run["first_frame"] is None else round(run["first_frame"], 2),
            completed_seconds=None if "complete" not in run else round(run["complete"], 2),
            samples_reached=samples[-1][1] if samples else 0,
            target_samples=rendering.get("samples"),
            samples_per_second=None if rate is None else round(rate, 2),
            gui=dict(
                heartbeats=len(gaps),
                heartbeat=summarize(gaps),
                stalls_over_50ms=len(stalls),
                stalled_seconds=round(sum(stalls), 2),
                stalled_fraction=round(sum(stalls) / wall, 3) if wall else None,
            ),
            gui_thread_code={name: summarize(values) for name, values in self.timings.items()},
            worker_cpu_percent_of_machine=None
            if None in (worker_cpu, run["worker_cpu"])
            else round((worker_cpu - run["worker_cpu"]) / wall / CORES * 100, 1),
            houdini_cpu_percent_of_machine=None
            if None in (houdini_cpu, run["houdini_cpu"])
            else round((houdini_cpu - run["houdini_cpu"]) / wall / CORES * 100, 1),
            worker_priority_class=run["worker"].priority(),
            priority_set=run.get("priority_set"),
            gpu=self.gpu.summary(began, now),
            denoiser=status.get("denoiser"),
            volume_ray_marching=status.get("volume_ray_marching"),
            render_device_ids=status.get("render_device_ids"),
            state=status.get("state"),
            # Fewer concurrent path states must show up as less device memory.
            device_allocation_mib=round(status.get("device_allocation_bytes", 0) / 2**20, 1),
            device_allocation_peak_mib=round(status.get("device_allocation_peak_bytes", 0) / 2**20, 1),
            resolution=None,
            settings={
                k: rendering.get(k)
                for k in ("samples", "adaptive_sampling", "volume_bounce", "max_bounce", "light_tree", "volume_biased")
                if k in rendering
            },
        )
        frame = self.panel.entry.session.frame_info() if self.panel.entry else None
        if frame:
            result["resolution"] = [frame.get("rendered_width"), frame.get("rendered_height")]
        if run["config"].get("below_normal"):
            run["worker"].set_priority(False)
        try:
            controller = self.panel.entry.session.diagnostics().get("controller", {})
            result["transport"] = controller.get("transport")
            result["gpu_scheduling"] = controller.get("gpu_scheduling")
        except Exception as error:
            result["transport"] = repr(error)
        if hasattr(self, "navigation_changes"):
            result["navigation_changes"] = self.navigation_changes
        result["concurrent_states_factor"] = os.environ.get("CYCLES_CONCURRENT_STATES_FACTOR")
        result["sample_timeline"] = [[round(at - began, 2), value] for at, value in samples]
        self.report["runs"].append(result)
        self.path.write_text(json.dumps(self.report, indent=2, default=str), encoding="utf-8")

    def finish(self, error=None):
        self.heartbeat.stop()
        if self.tick in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.tick)
        self.gpu.close()
        if self.panel:
            self.panel.dispose()
        registry.close_all()
        for owner, name, original in reversed(self.restores):
            setattr(owner, name, original)
        assert hashlib.sha256(self.source.read_bytes()).hexdigest() == self.digest, "The artist scene changed"
        self.report.update(
            result="failed" if error else "passed", error=error, elapsed_seconds=time.monotonic() - self.started
        )
        self.path.write_text(json.dumps(self.report, indent=2, default=str), encoding="utf-8")
        hou.exit(1 if error else 0, suppress_save_prompt=True)


probe = Probe()
