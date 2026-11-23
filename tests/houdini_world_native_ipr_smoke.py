# SPDX-License-Identifier: Apache-2.0
"""One native IPR viewer, persistent SOHO pipe and heavy SOP world updates."""

import json
import os
from pathlib import Path
import runpy
import time
import traceback

import hou
import numpy as np
import OpenImageIO as OIIO
from cycles_houdini import registry, menu_actions

os.environ["CYCLES_HOUDINI_BENCH_SHARED_VIEWS"] = "0"
os.environ["CYCLES_HOUDINI_BENCH_SAMPLES"] = "65536"
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
HeavyResponse = runpy.run_path(
    str(root / "tests/houdini_heavy_response_benchmark.py"), run_name="native_world_fixture"
)["HeavyResponse"]


class NativeWorld(HeavyResponse):
    create_render_panel = False

    def configure_scene(self):
        self.ground.parm("resolution").set(560)
        self.environment = menu_actions.create_environment()
        self.environment.parmTuple("light_color").set((0.5, 0.25, 0.125))
        self.environment.parm("light_intensity").set(1)
        self.rop.parm("world_source").set("scene")
        self.events = []
        self.expected = np.array((0.5, 0.25, 0.125))
        self.asset = self.path.with_suffix(".hdr.exr")

    def __init__(self):
        super().__init__()
        self.finished = False
        self.report["render_viewers"] = ["native"]
        self.viewer = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
        self.viewer.setRopNode(self.rop)
        self.viewer.setPreview(True)
        self.viewer.setAutoUpdate(True)
        self.viewer.setDelay(0.02)
        self.viewer.startRender()

    def ready(self, entry):
        if entry.error:
            raise RuntimeError(entry.error)
        status = entry.session.status()
        if status["state"] == "failed":
            raise RuntimeError(status["error"])
        frame = entry.session.frame_info()
        if (
            not entry.helper_pid
            or not frame
            or frame["generation"] != status["generation"]
            or status["generation"] <= self.before
        ):
            return False
        try:
            if self.viewer.imageResolution() != (960, 540):
                return False
            # The upper corner sees the world rather than the courtyard. Its
            # constant radiance is independent of geometry conversion/sampling.
            pixel = np.asarray(self.viewer.pixel("C", 3, 535)[:3])
            if not np.allclose(pixel, self.expected, rtol=0, atol=0.002):
                return False
        except hou.OperationFailed:
            return False
        self.pixel = pixel.tolist()
        return True

    def edit(self, stage):
        self.before = registry.get(self.key).session.status()["generation"]
        self.stage = stage
        self.report["phase"] = stage

    def write(self, value):
        pixels = np.broadcast_to(np.asarray(value, np.float32), (64, 128, 3)).copy()
        spec = OIIO.ImageSpec(128, 64, 3, OIIO.FLOAT)
        spec.attribute("oiio:ColorSpace", "data")
        output = OIIO.ImageOutput.create(str(self.asset))
        assert output.open(str(self.asset), spec) and output.write_image(pixels) and output.close()
        stat = self.asset.stat()
        os.utime(self.asset, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))

    def tick(self):
        if self.tick_busy or self.finished:
            return
        self.tick_busy = True
        try:
            if time.monotonic() - self.began > 180:
                raise TimeoutError("Native world IPR stalled at " + self.stage)
            if self.stage == "closing":
                closed = self.retiring_session.status()
                self.report["closing"] = closed
                self.save()
                if closed["state"] == "failed":
                    raise RuntimeError(closed["error"])
                if closed["state"] != "closed":
                    return
                assert closed["device_allocation_bytes"] == closed["owned_geometry_bytes"] == 0, closed
                self.report["closed"] = closed
                self.finish()
                return
            entry = registry.get(self.key)
            self.report.update(
                events=self.events,
                current=dict(
                    stage=self.stage,
                    status=entry.session.status() if entry else None,
                    rop_errors=self.rop.errors(),
                    bridge=list(registry.debug_events),
                ),
            )
            self.save()
            if entry is None or not self.ready(entry):
                return
            status = entry.session.status()
            assert len(registry.entries()) == 1 and entry.views == 0 and self.panel is None
            assert self.report["fixture"]["mesh_triangles"] >= 1000000
            if self.stage == "initial":
                self.conversions = status["geometry_conversions"]
                self.renderer_pid = status["renderer_pid"]
                self.helper_pid = entry.helper_pid
            else:
                assert status["geometry_conversions"] == self.conversions
                assert status["renderer_pid"] == self.renderer_pid and entry.helper_pid == self.helper_pid
            assert self.renderer_pid != os.getpid() and self.helper_pid != os.getpid()
            assert status["render_device_ids"] == [self.device], status["render_device_ids"]
            issues = status["source_diagnostics"]
            assert bool(issues) == (self.stage == "portal-enabled"), issues
            assert all(
                issue["code"] == "world.unsupported_setting" and "env_portalenable" in issue["message"]
                for issue in issues
            )
            self.events.append(
                dict(
                    stage=self.stage,
                    status=status,
                    diagnostics=entry.session.diagnostics(),
                    pixel=self.pixel,
                    helper_pid=entry.helper_pid,
                    native_planes=list(self.viewer.planes()),
                    soho=entry.last_soho,
                )
            )
            self.save()
            if self.stage == "initial":
                self.edit("portal-enabled")
                self.expected *= 2
                self.environment.setParms(dict(env_portalenable=1, light_intensity=2))
            elif self.stage == "portal-enabled":
                self.edit("portal-repaired")
                self.expected *= 0.5
                self.environment.setParms(dict(env_portalenable=0, light_intensity=1))
            elif self.stage == "portal-repaired":
                self.edit("intensity")
                self.expected *= 2
                self.environment.parm("light_intensity").set(2)
            elif self.stage == "intensity":
                self.edit("environment-file")
                self.expected = np.array((0.25, 0.5, 1))
                self.environment.parmTuple("light_color").set((1, 1, 1))
                self.environment.parm("light_intensity").set(1)
                self.write(self.expected)
                self.environment.setParms(dict(env_map=str(self.asset), cycles_environment_colorspace="data"))
            elif self.stage == "environment-file":
                self.edit("disk-only")
                self.expected *= 2
                self.write(self.expected)
            elif self.stage == "disk-only":
                self.edit("constant-source")
                self.expected = np.array((0.125, 0.25, 0.5))
                self.rop.parmTuple("background").set(tuple(self.expected))
                self.rop.parm("world_source").set("color")
            elif self.stage == "constant-source":
                self.retiring_session = entry.session
                self.viewer.killRender()
                registry.close_all()
                self.stage = "closing"
                self.report["phase"] = self.stage
                self.save()
        except Exception:
            self.finish(traceback.format_exc())
        finally:
            self.tick_busy = False

    def finish(self, error=None):
        if self.finished:
            return
        self.finished = True
        self.timer.stop()
        if self.callback in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.callback)
        registry.close_all()
        self.report.update(result="failed" if error else "passed", error=error, events=self.events)
        self.save()
        hou.exit(1 if error else 0, suppress_save_prompt=True)


workflow = NativeWorld()
