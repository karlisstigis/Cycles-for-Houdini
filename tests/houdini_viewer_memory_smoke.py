# SPDX-License-Identifier: Apache-2.0
"""Real single/multiple GPU telemetry, worker attribution and live viewer edits."""

from support.paths import artifacts_root
import json
import os
from pathlib import Path
import time
import hou
from PySide6 import QtWidgets
import cycles_houdini as ch
from cycles_houdini import device_selection, registry, viewer_memory
from cycles_houdini.panel import RenderPanel

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
fake_host = dict(pid=1, host=dict(working_set_bytes=100 * 2**20))
fake_worker = dict(pid=2, host=dict(working_set_bytes=200 * 2**20))
assert viewer_memory.compact(fake_host, fake_worker, ["CPU"], []) == "Mem: 300 MiB"
assert viewer_memory.compact(fake_host, fake_host, ["CPU"], []) == "Mem: 100 MiB"
assert viewer_memory.compact(dict(pid=1), None, [], []) == "Mem: N/A"
available = ch.devices()
gpus = [d for d in available if d["backend"] == "OPTIX"]
assert len(gpus) >= 2, "This workstation check requires both installed NVIDIA GPUs"
destination = artifacts_root() / "stage3/viewer-memory"
destination.mkdir(parents=True, exist_ok=True)
geo = hou.node("/obj").createNode("geo", run_init_scripts=False)
box = geo.createNode("box")
camera = hou.node("/obj").createNode("cam")
camera.parm("tz").set(5)
records = []


def wait(panel, predicate):
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        app.processEvents()
        panel.poll()
        if predicate():
            return
        time.sleep(0.01)
    raise TimeoutError(panel.memory_info.text() + "\n" + panel.info.text())


for selection in [(d["id"],) for d in gpus] + [tuple(d["id"] for d in gpus), ("CPU",)]:
    rop = hou.node("/out").createNode("cycles_ipr")
    rop.parm("source_mode").set("sop")
    rop.parm("soppath").set(box.path())
    rop.parm("camera").set(camera.path())
    rop.parm("resx").set(64)
    rop.parm("resy").set(48)
    rop.parm("ipr_samples").set(8)
    rop.parm("ipr_use_denoise").set(0)
    rop.parm("allowmotionblur").set(0)
    device_selection.set_devices(rop, selection)
    panel = RenderPanel()
    panel.set_rop(rop)
    panel.resize(1400, 650)
    panel.show()
    assert panel.memory_details.isChecked()
    panel.auto.setChecked(False)
    panel.render()
    wait(
        panel,
        lambda: panel.entry and panel.entry.session.status()["state"] == "complete" and not panel.canvas.image.isNull(),
    )
    session = panel.entry.session
    initial = session.status()["generation"]
    frame = session.frame_info()
    panel.memory_details.setChecked(True)
    snapshot = [None]

    def collected():
        snapshot[0] = session.memory_snapshot()
        return snapshot[0] is not None

    wait(panel, collected)
    panel.poll_memory(force=True)
    host = ch.process_memory()
    text = panel.memory_info.toolTip()
    assert panel.memory_info.text().startswith("Mem: ")
    assert ("GPU:" in panel.memory_info.text()) == bool([d for d in gpus if d["id"] in selection])
    assert panel.memory_info.parentWidget() is panel.viewer_status
    app.processEvents()
    canvas_bounds = panel.canvas.geometry()
    panel.memory_details.setChecked(False)
    app.processEvents()
    assert not panel.memory_info.isVisible() and panel.canvas.geometry() == canvas_bounds
    panel.memory_details.setChecked(True)
    app.processEvents()
    assert panel.memory_info.isVisible() and panel.canvas.geometry() == canvas_bounds
    assert snapshot[0]["pid"] == session.status()["renderer_pid"]
    assert snapshot[0]["pid"] != os.getpid(), "GPU counters came from the artist process"
    expected = [d for d in gpus if d["id"] in selection]
    for gpu in gpus:
        assert (gpu["name"] in text) == (gpu in expected), text
    assert "AMD" not in text and "unavailable" not in text and "pending" not in text, text
    if not expected:
        assert "CPU rendering" in text
    for gpu in expected:
        identity = viewer_memory.adapter_identity(gpu["physical_id"])
        adapter = next(a for a in snapshot[0]["gpu"]["adapters"] if (a["luid"], a["node"]) == identity)
        assert adapter["local"]["current_usage_bytes"] > 1024**2, adapter
    # Labels are presentation only; physically distinct, identically named cards
    # must still map to different adapters, even with reverse catalogue ordering.
    renamed = [dict(d, name="Identical GPU") for d in reversed(available)]
    duplicate_text, _ = viewer_memory.describe(host, snapshot[0], selection, renamed)
    assert duplicate_text.count("Identical GPU:") == len(expected), duplicate_text
    missing_text, _ = viewer_memory.describe(host, dict(pid=snapshot[0]["pid"]), selection, available)
    if expected:
        assert "unavailable" in missing_text and "0.0 MiB" not in missing_text.split("renderer GPU usage")[-1]
    assert session.status()["generation"] == initial and session.frame_info() == frame
    # Display edits and queries must leave tracing unchanged; a real SOP edit
    # must still update, then repair normally while telemetry remains enabled.
    panel.exposure.setValue(1)
    panel.poll_memory(force=True)
    assert session.status()["generation"] == initial
    for size in (1.3, 1.0):
        box.parmTuple("size").set((size,) * 3)
        panel.render()
        wait(panel, lambda: session.status()["generation"] > initial and session.status()["state"] == "complete")
        initial = session.status()["generation"]
    records.append(
        dict(
            selected=selection,
            host=host,
            renderer=snapshot[0],
            text=text,
            compact=panel.memory_info.text(),
            repaired_generation=initial,
        )
    )
    panel.grab().save(str(destination / ("viewer-" + str(len(records)) + ".png")))
    panel.dispose()
    panel.close()
    registry.close_all()

(destination / "report.json").write_text(json.dumps(dict(result="passed", records=records), indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", selections=len(records))))
