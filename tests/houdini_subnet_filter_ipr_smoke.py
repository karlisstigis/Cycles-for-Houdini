# SPDX-License-Identifier: Apache-2.0
"""Geometry OBJ subnet filtering, hidden prototypes and automatic live repairs."""

from support.paths import artifacts_root

import argparse
import json
import os
import time
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import hou
import numpy as np
from PySide6 import QtWidgets
import cycles_houdini as ch
from cycles_houdini import instancing, object_properties, registry
from cycles_houdini.device_selection import set_devices
from cycles_houdini.panel import RenderPanel

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
args = parser.parse_args()
out = artifacts_root() / "subnet-filter-20261028"
out.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=args.device, checks=[])
root = hou.node("/obj")
source = root.createNode("subnet", "filter_source")
for name, x in (("left", -0.7), ("right", 0.7)):
    member = source.createNode("geo", name, run_init_scripts=False)
    box = member.createNode("box")
    box.parmTuple("size").set((0.6, 0.8, 0.3))
    box.setRenderFlag(True)
    member.parm("tx").set(x)
scatter = root.createNode("geo", "filter_scatter", run_init_scripts=False)
points = scatter.createNode("attribwrangle")
points.parm("class").set(0)
points.parm("snippet").set('int p=addpoint(0,{0,0,0}); setpointattrib(0,"instance",p,"../filter_source");')
packed_box = scatter.createNode("box", "packed_source")
packed_box.parmTuple("t").set((1.5, 0, 0))
packed_box.parmTuple("size").set((0.4, 0.8, 0.3))
packed = scatter.createNode("pack")
packed.setInput(0, packed_box)
merge = scatter.createNode("merge")
merge.setInput(0, points)
merge.setInput(1, packed)
merge.setRenderFlag(True)
source.setDisplayFlag(False)
assert len(instancing.subnet_members(scatter, source, hou.time())) == 2
assert object_properties.install(scatter)
assert scatter.evalParm("cycles_subnet_filter") == "all"
assert len(instancing.subnet_members(scatter, source, hou.time())) == 2
source.setDisplayFlag(True)
assert scatter.parm("cycles_subnet_pattern").parmTemplate().conditionals()
# Upgrade an existing Cycles tab without losing authored expressions/properties.
scatter.parm("cycles_dielectric_priority").set(7)
group = scatter.parmTemplateGroup()
folder = object_properties.properties_folder(group)
folder.setTags({**folder.tags(), "cycles_properties_version": "13"})
group.replace(folder.name(), folder)
group.remove("cycles_subnet_filter")
group.remove("cycles_subnet_pattern")
scatter.setParmTemplateGroup(group)
assert object_properties.install(scatter)
assert scatter.evalParm("cycles_dielectric_priority") == 7
assert not object_properties.install(scatter)
scatter.parm("cycles_subnet_filter").set("display")
# A previously authored display filter survives a version upgrade.
group = scatter.parmTemplateGroup()
folder = object_properties.properties_folder(group)
folder.setTags({**folder.tags(), "cycles_properties_version": "14"})
group.replace(folder.name(), folder)
scatter.setParmTemplateGroup(group)
assert object_properties.install(scatter)
assert scatter.evalParm("cycles_subnet_filter") == "display"
native = root.createNode("instance", "native_control")
object_properties.install(native)
assert native.parm("cycles_subnet_filter") is None
native.parm("subnet_filter").set("pattern")
native.parm("subnet_pattern").set("left")
assert [n.name() for n in instancing.subnet_members(native, source, hou.time())] == ["left"]
report["checks"].append("Tab upgrade preserves properties; Instance OBJ uses only its native controls")

camera = root.createNode("cam", "filter_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
rop = hou.node("/out").createNode("cycles_ipr", "filter_render")
rop.setParms(
    dict(
        source_mode="objects",
        vobject="filter_scatter",
        camera=camera.path(),
        resx=96,
        resy=64,
        viewer_interactive=1,
        ipr_samples=1048576,
        ipr_adaptive_sampling=0,
        ipr_use_denoise=0,
        allowmotionblur=0,
        material_kind="emission",
        use_material_assignments=0,
    )
)
rop.parmTuple("basecolor").set((0.25, 0.5, 0.75))
rop.parmTuple("background").set((0, 0, 0))
set_devices(rop, args.device)
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
panel = RenderPanel()
panel.set_rop(rop)
panel.auto.setChecked(True)
panel.show()


def await_image(label, previous, expected):
    if len(expected) == 2:
        expected = (*expected, True)
    deadline = time.monotonic() + 45
    logged = False
    while time.monotonic() < deadline:
        app.processEvents()
        panel.poll()
        if panel.entry:
            session = panel.entry.session
            status = session.status()
            assert not panel.entry.error and status["state"] != "failed", (label, panel.entry.error, status)
            info = session.frame_info()
            if (
                info
                and info.get("current_scene")
                and status["render_revision"] > previous
                and not status.get("pending_update")
                and not status.get("capture_pending")
            ):
                frame = session.frame()
                plane = frame["planes"].get("combined")
                if plane:
                    pixels = np.frombuffer(plane["pixels"], np.float32).reshape(
                        frame["height"], frame["width"], plane["channels"]
                    )
                    colors = [pixels[frame["height"] // 2, int(frame["width"] * x), :3] for x in (0.325, 0.675, 0.875)]
                    if not logged:
                        print(label, status["state"], info["samples"], [v.tolist() for v in colors], flush=True)
                        logged = True
                    if all(
                        np.allclose(color, (0.25, 0.5, 0.75) if present else (0, 0, 0), atol=0.005)
                        for color, present in zip(colors, expected)
                    ):
                        report["checks"].append(
                            dict(
                                name=label,
                                revision=status["render_revision"],
                                samples=info["samples"],
                                state=status["state"],
                                colors=[v.tolist() for v in colors],
                            )
                        )
                        print("PASS", label, flush=True)
                        return status
        time.sleep(0.01)
    raise AssertionError((label, panel.info.text(), panel.entry.session.status()))


try:
    panel.render()
    status = await_image("Displayed source includes both members", -1, (True, True))
    pid = status["renderer_pid"]
    assert status["state"] == "rendering"

    def edit(label, action, expected):
        before = panel.entry.session.status()["render_revision"]
        action()
        status = await_image(label, before, expected)
        assert status["renderer_pid"] == pid
        return status

    edit(
        "Disable packed primitives while point instances remain",
        lambda: scatter.parm("cycles_render_packed").set(0),
        (True, True, False),
    )
    edit("Re-enable packed primitives", lambda: scatter.parm("cycles_render_packed").set(1), (True, True, True))
    edit("Hide source: Displayed Objects removes members", lambda: source.setDisplayFlag(False), (False, False))
    edit(
        "All Objects restores hidden source instances",
        lambda: scatter.parm("cycles_subnet_filter").set("all"),
        (True, True),
    )
    edit(
        "Pattern selects left member under hidden source",
        lambda: scatter.setParms(dict(cycles_subnet_filter="pattern", cycles_subnet_pattern="left")),
        (True, False),
    )
    edit(
        "Empty pattern result removes instances",
        lambda: scatter.parm("cycles_subnet_pattern").set("absent_*"),
        (False, False),
    )
    edit(
        "Pattern repair restores right member",
        lambda: scatter.parm("cycles_subnet_pattern").set("right"),
        (False, True),
    )
    edit(
        "Displayed Objects excludes hidden source again",
        lambda: scatter.parm("cycles_subnet_filter").set("display"),
        (False, False),
    )
    edit(
        "Showing source repairs empty displayed selection automatically",
        lambda: source.setDisplayFlag(True),
        (True, True),
    )
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    panel.dispose()
    panel.close()
    registry.close_all()
    (out / "subnet-filter-ipr.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
