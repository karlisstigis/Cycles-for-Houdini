# SPDX-License-Identifier: Apache-2.0
"""Artist scene: event-loop latency, shader strategy edits and Stop/restart in IPR."""

import json
import math
import os
from pathlib import Path
import time
import traceback
import hou
from PySide6 import QtCore
from cycles_houdini.panel import RenderPanel
from cycles_houdini import registry
from support.private_scenes import artist_scene

destination = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
hou.hipFile.load(str(artist_scene("Cycles_Test/hdri_test.hiplc")), suppress_save_prompt=True, ignore_load_warnings=True)
rop = hou.node("/out/cycles_render")
camera = hou.node("/obj/cam1")
light = hou.node("/obj/cycles_disk_light")
shader = hou.node("/mat/toy/surface_shader")
toy = hou.node("/obj/testgeometry_rubbertoy1")
rop.setParms(
    dict(
        resx=640,
        resy=360,
        ipr_resolution_percent=100,
        ipr_samples=1048576,
        ipr_adaptive_sampling=0,
        ipr_use_denoise=0,
        resampled_light_sampling=1,
    )
)
panel = RenderPanel()
panel.resize(1350, 800)
panel.set_rop(rop)
panel.show()
panel.auto.setChecked(True)
panel.render()
report = dict(
    result="running",
    runtime=__import__("cycles_houdini").__file__,
    events=[],
    method="Native Houdini parameter edits during live GPU rendering; Qt event-loop heartbeat",
    phase_names={1: "camera", 2: "light and object", 3: "material", 4: "RLS toggle", 5: "Stop/restart"},
    edited_during_accumulation=False,
)
base_camera = camera.evalParm("tx")
base_rotation = camera.evalParm("ry")
base_light = light.evalParm("ry")
base_toy = toy.evalParm("tx")
base_toy_rotation = toy.evalParm("ry")
phase = 0
step = 0
generation = 0
pid = None
busy = False
last_edit = 0.0
previous_tick = None
gaps = []
started = time.monotonic()
phase_start = started
timer = QtCore.QTimer(hou.qt.mainWindow())
timer.setInterval(20)


def save():
    destination.write_text(json.dumps(report, indent=2))


def finish(error=None):
    timer.stop()
    panel.dispose()
    panel.close()
    registry.close_all()
    report.update(result="failed" if error else "passed", error=error)
    save()
    hou.ui.postEventCallback(lambda: hou.exit(1 if error else 0, suppress_save_prompt=True))


def next_phase(status):
    global phase, step, gaps, previous_tick, phase_start, generation
    if gaps:
        ordered = sorted(gaps)
        report["events"].append(
            dict(
                phase=phase,
                edits=step,
                max_gap=max(gaps),
                p95_gap=ordered[int(0.95 * len(ordered))],
                ticks=len(gaps),
                generation=status["generation"],
                renderer_pid=status["renderer_pid"],
            )
        )
        assert ordered[int(0.95 * len(ordered))] < 0.12, report["events"][-1]
        assert max(gaps) < 0.75, report["events"][-1]
    phase += 1
    step = 0
    gaps = []
    previous_tick = None
    phase_start = time.monotonic()
    generation = status["generation"]
    save()


def ready(entry, status):
    frame = entry.session.frame_info()
    return (
        frame
        and frame["generation"] == status["generation"]
        and entry.change_serial == entry.applied_serial
        and not status.get("capture_pending")
        and not status.get("pending_update")
        and status.get("current_sample", 0) > 0
    )


def tick():
    global phase, step, busy, pid, last_edit, previous_tick, generation
    if busy:
        return
    busy = True
    try:
        now = time.monotonic()
        assert now - started < 150, (phase, step)
        if not panel.entry:
            return
        entry = panel.entry
        status = entry.session.status()
        assert not entry.error and not status["error"], (phase, entry.error, status)
        if pid is None and status["renderer_pid"]:
            pid = status["renderer_pid"]
        if status["renderer_pid"]:
            assert status["renderer_pid"] == pid
        if phase == 0:
            if not ready(entry, status):
                return
            assert status["image_iris_table_bytes"] > 0, "Artist RGB iris must stay enabled"
            next_phase(status)
            return
        if previous_tick is not None:
            gaps.append(now - previous_tick)
        previous_tick = now
        if phase in (1, 2):
            if step < 35 and now - last_edit > 0.10:
                report["edited_during_accumulation"] |= status["state"] == "rendering"
                if phase == 1:
                    camera.parm("tx").set(base_camera + 0.1 * math.sin(step * 0.2))
                    camera.parm("ry").set(base_rotation + math.sin(step * 0.16))
                elif step % 2:
                    toy.parm("tx").set(base_toy + 0.1 * math.sin(step * 0.2))
                    toy.parm("ry").set(base_toy_rotation + 3 * math.sin(step * 0.2))
                else:
                    light.parm("ry").set(base_light + 3 * math.sin(step * 0.2))
                last_edit = now
                step += 1
            if step == 35 and ready(entry, status):
                next_phase(status)
        elif phase == 3:
            values = [(0.25, 1.0), (0.053, 1.0), (0.053, 0.5), (0.053, 1.0)]
            if (
                step < len(values)
                and ready(entry, status)
                and now - last_edit > 0.2
                and (step == 0 or status["generation"] > generation)
            ):
                roughness, metallic = values[step]
                shader.setParms(dict(roughness=roughness, metallic=metallic))
                generation = status["generation"]
                last_edit = now
                step += 1
            elif step == len(values) and ready(entry, status) and status["generation"] > generation:
                next_phase(status)
        elif phase == 4:
            if step == 0:
                rop.parm("resampled_light_sampling").set(0)
                generation = status["generation"]
                step = 1
            elif ready(entry, status) and status["generation"] > generation:
                assert status["rendering"]["resampled_light_sampling"] == (step == 2), status
                if step == 1:
                    rop.parm("resampled_light_sampling").set(1)
                    generation = status["generation"]
                    step = 2
                else:
                    next_phase(status)
                    panel.cancel()
                    QtCore.QTimer.singleShot(100, panel.render)
        elif phase == 5 and ready(entry, status) and status["generation"] > generation:
            assert report["edited_during_accumulation"]
            next_phase(status)
            finish()
    except BaseException:
        finish(traceback.format_exc())
    finally:
        busy = False


timer.timeout.connect(tick)
timer.start()
