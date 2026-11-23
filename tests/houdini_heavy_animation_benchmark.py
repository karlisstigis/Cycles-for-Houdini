# SPDX-License-Identifier: Apache-2.0
"""Five fully rendered heavy frames in one persistent session and one viewer."""

import hashlib
import json
import math
import os
from pathlib import Path
import runpy
import time
import traceback
import hou
import numpy as np
import OpenImageIO as OIIO
from cycles_houdini import registry

# Same actual heavy SOP inventory as the response experiment, a finite sample
# budget for complete frames, and one panel. No native IPR is opened here.
os.environ["CYCLES_HOUDINI_BENCH_SHARED_VIEWS"] = "0"
os.environ["CYCLES_HOUDINI_BENCH_SAMPLES"] = "128"
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
HeavyResponse = runpy.run_path(
    str(root / "tests/houdini_heavy_response_benchmark.py"), run_name="heavy_animation_fixture"
)["HeavyResponse"]


class HeavyAnimation(HeavyResponse):
    expected_geometry_conversions = 1

    def configure_scene(self):
        hou.setFps(24)
        hou.playbar.setFrameRange(1, 5)
        hou.setFrame(1)
        self.ground.parm("resolution").set(560)
        self.report["adaptive_fixture"] = runpy.run_path(
            str(root / "examples/sop-fx-courtyard/adaptive_scene.py"), run_name="adaptive_animation_fixture"
        )["upgrade"]()
        self.hero.parm("twist").setExpression("2.7 + 0.12 * ($F - 1)", hou.exprLanguage.Hscript)
        ground_noise = hou.node("/mat/courtyard_ground_noise/ground_noise")
        ground_w = ground_noise.parm("w") or ground_noise.parm("W")
        # Real per-frame linear image assets drive true geometry displacement.
        # Multiscale periodic noise translates and changes amplitude over time.
        self.map_directory = self.path.parent / "displacement-maps"
        self.map_directory.mkdir()
        size = 256
        rng = np.random.default_rng(7183)
        spectral = np.fft.rfft2(rng.normal(size=(size, size)))
        fy = np.fft.fftfreq(size)[:, None]
        fx = np.fft.rfftfreq(size)[None, :]
        spectrum = spectral * np.exp(-(fx * fx + fy * fy) / 0.002) / (fx * fx + fy * fy + 0.0001)
        noise = np.fft.irfft2(spectrum, s=(size, size))
        noise = (noise - noise.mean()) / np.max(np.abs(noise - noise.mean()))
        self.displacement_maps = {}
        for number in range(1, 6):
            values = np.asarray(
                0.5 + (0.24 + 0.035 * (number - 1)) * np.roll(noise, 13 * (number - 1), axis=1), np.float32
            )
            pixels = np.repeat(values[:, :, None], 3, axis=2)
            asset = self.map_directory / f"displacement-{number:04d}.exr"
            output = OIIO.ImageOutput.create(str(asset))
            assert output and output.open(str(asset), OIIO.ImageSpec(size, size, 3, OIIO.FLOAT)), OIIO.geterror()
            assert output.write_image(pixels), output.geterror()
            assert output.close(), output.geterror()
            self.displacement_maps[number] = dict(
                path=str(asset),
                sha256=hashlib.sha256(asset.read_bytes()).hexdigest(),
                minimum=float(values.min()),
                maximum=float(values.max()),
            )
        assert len({v["sha256"] for v in self.displacement_maps.values()}) == 5
        self.height_texture = self.stone.createNode("cycles::image_texture::1", "animated_displacement_map")
        self.height_texture.parm("filename").set(str(self.map_directory / "displacement-$F4.exr"))
        self.height_texture.parm("colorspace").set("data")
        self.height_texture.parm("interpolation").set("linear")
        self.height_texture.setNamedInput("vector", self.stone.node("stone_coordinates"), "UV")
        luminance = self.stone.createNode("cycles::rgb_to_bw::1", "animated_displacement_height")
        luminance.setNamedInput("color", self.height_texture, "color")
        self.stone.node("true_stone_displacement").setNamedInput("height", luminance, "val")
        self.report.update(
            frames=[],
            render_viewers=["cycles"],
            animation=dict(
                first=1,
                last=5,
                fps=24,
                animated_parameter=self.hero.path() + "/twist",
                expression="2.7 + 0.12 * ($F - 1)",
                displacement_parameter=self.height_texture.path() + "/filename",
                displacement_assets=self.displacement_maps,
                map_color_space="data",
                ground_noise_parameter=ground_w.path(),
                ground_noise_expression=ground_w.expression(),
            ),
            scope="Five complete million-triangle SOP frames; deforming adaptive turbine, animated true displacement maps and ground noise, one persistent IPR worker and one Cycles panel. Throughput includes display/output; first-canvas observation uses host idleness and is not the Qt-paint latency benchmark.",
        )

    def __init__(self):
        super().__init__()
        self.sequence_frame = 1
        self.sequence_started = self.frame_started = time.monotonic()
        self.report["setup_seconds"] = self.sequence_started - self.began
        self.report["phase"] = "frame-1"
        self.stage = "frame"
        self.frame_apply_index = 0
        self.previous_status = None
        self.previous_transport = dict()
        self.first_canvas = None
        self.memory_event = None
        self.memory_started = None
        self.sequence_pid = None
        self.save()

    def finish_frame(self, entry):
        status = entry.session.status()
        frame = entry.session.frame()
        assert frame["complete"] and frame["generation"] == status["generation"], frame
        pixels = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32)
        assert np.isfinite(pixels).all() and pixels.std() > 0.05
        assert len(registry.entries()) == 1 and self.viewer is None
        assert self.panel.entry is entry
        assert self.report["fixture"]["mesh_triangles"] >= 1000000
        assert status["subdivision_control_faces"] > 0 and status["subdivision_triangles"] > 0
        conversions = status["geometry_conversions"] - (self.previous_status or {}).get("geometry_conversions", 0)
        if self.previous_status is not None:
            assert conversions == self.expected_geometry_conversions, (self.sequence_frame, conversions, status)
            assert status["displacement_rebuilds"] > self.previous_status["displacement_rebuilds"], status
            assert status["subdivision_tessellations"] > self.previous_status["subdivision_tessellations"], status
        expected_map = self.displacement_maps[self.sequence_frame]
        assert Path(self.height_texture.parm("filename").eval()) == Path(expected_map["path"])
        assert hashlib.sha256(Path(expected_map["path"]).read_bytes()).hexdigest() == expected_map["sha256"]
        diagnostics = entry.session.diagnostics()
        if self.mode != "sync":
            assert diagnostics["session_cancel_calls"] == 0, diagnostics
            assert status["renderer_pid"] == self.sequence_pid
        output = self.path.parent / f"frame-{self.sequence_frame:04d}.exr"
        output_started = time.monotonic()
        entry.session.write_exr(output)
        output_seconds = time.monotonic() - output_started
        assert output.is_file() and output.stat().st_size > 100000
        preview = self.path.parent / f"frame-{self.sequence_frame:04d}.png"
        assert self.panel.canvas.image.save(str(preview))
        finished = time.monotonic()
        records = self.report["applies"][self.frame_apply_index :]
        self.frame_record = dict(
            frame=self.sequence_frame,
            time_seconds=hou.time(),
            twist=self.hero.parm("twist").eval(),
            generation=status["generation"],
            first_canvas_seconds=self.first_canvas,
            complete_canvas_seconds=self.complete_at - self.frame_started,
            frame_with_output_seconds=finished - self.frame_started,
            cumulative_with_output_seconds=finished - self.sequence_started,
            host_apply_seconds=sum(r["host_seconds"] for r in records),
            apply_count=len(records),
            output_seconds=output_seconds,
            exr_bytes=output.stat().st_size,
            image_sha256=hashlib.sha256(pixels.tobytes()).hexdigest(),
            geometry_conversion_delta=conversions,
            status=status,
            diagnostics=diagnostics,
            displacement_map=expected_map,
            texture_inputs=entry.session.texture_inputs(),
            host_memory=__import__("cycles_houdini").process_memory(),
            outputs=dict(exr=str(output), png=str(preview)),
        )
        self.report["phase"] = f"memory-frame-{self.sequence_frame}"
        self.memory_started = time.monotonic()
        self.memory_event = None
        if self.mode != "sync":
            self.memory_event = entry.session._controller.control("memory")
        self.stage = "memory"
        self.save()

    def tick(self):
        if self.tick_busy:
            return
        self.tick_busy = True
        try:
            if time.monotonic() - self.began > 210:
                raise TimeoutError("Five-frame heavy animation exceeded its limit")
            entry = registry.get(self.key)
            if entry is None:
                return
            if self.stage == "frame":
                if not self.ready(entry):
                    return
                if self.first_canvas is None:
                    self.first_canvas = time.monotonic() - self.frame_started
                info = entry.session.frame_info()
                status = entry.session.status()
                if status["state"] != "complete" or not info["complete"] or self.panel._sequence < info["sequence"]:
                    return
                if self.sequence_pid is None:
                    self.sequence_pid = status.get("renderer_pid", os.getpid())
                    self.report["renderer_pid"] = self.sequence_pid
                self.complete_at = time.monotonic()
                self.finish_frame(entry)
            elif self.stage == "memory":
                if self.memory_event is not None and not self.memory_event.is_set():
                    return
                if self.mode != "sync":
                    self.frame_record["renderer_memory"] = entry.session._controller.backend_memory
                transport = self.frame_record["diagnostics"].get("controller", {}).get("transport", {})
                self.frame_record["transport_delta"] = {
                    k: value - self.previous_transport.get(k, 0) for k, value in transport.items()
                }
                self.previous_transport = dict(transport)
                self.frame_record["memory_observation_seconds"] = time.monotonic() - self.memory_started
                self.report["frames"].append(self.frame_record)
                self.previous_status = self.frame_record["status"]
                self.save()
                if self.sequence_frame == 5:
                    assert len({r["image_sha256"] for r in self.report["frames"]}) == 5
                    self.report["sequence_total_seconds"] = time.monotonic() - self.sequence_started
                    self.report["sum_frame_with_output_seconds"] = sum(
                        r["frame_with_output_seconds"] for r in self.report["frames"]
                    )
                    self.report["interframe_and_observation_seconds"] = (
                        self.report["sequence_total_seconds"] - self.report["sum_frame_with_output_seconds"]
                    )
                    self.report["final_diagnostics"] = entry.session.diagnostics()
                    self.panel.window().grab().save(str(self.path.with_suffix(".panel.png")))
                    self.finish()
                self.sequence_frame += 1
                self.before = entry.session.status()["generation"]
                self.first_canvas = None
                self.frame_apply_index = len(self.report["applies"])
                self.report["phase"] = f"frame-{self.sequence_frame}"
                self.frame_started = time.monotonic()
                self.stage = "frame"
                hou.setFrame(self.sequence_frame)
        except Exception:
            self.report.update(result="failed", error=traceback.format_exc())
            self.save()
            self.timer.stop()
            hou.ui.removeEventLoopCallback(self.callback)
            try:
                registry.close_all()
            except Exception:
                pass
            hou.exit(1, suppress_save_prompt=True)
        finally:
            self.tick_busy = False


if __name__ == "__main__":
    workflow = HeavyAnimation()
