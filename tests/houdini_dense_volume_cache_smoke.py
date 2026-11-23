# SPDX-License-Identifier: Apache-2.0
"""Unchanged dense fields reuse conversion; retired names cannot retain GPU grids."""

import argparse
import json
import time
import traceback

import hou
from cycles_houdini import native_scene, registry
from dense_volume_fixture import BASE, fixture, check

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
rop, source, assignment, dest = fixture(device)
rename = source.parent().createNode("attribwrangle", "rename_one_unused_field")
rename.setInput(0, assignment)
rename.addSpareParmTuple(hou.StringParmTemplate("field_name", "Field name", 1, default_value=("custom_integer",)))
rename.setParms({"class": 1, "group": "@name=custom_integer", "snippet": 's@name=chs("field_name");'})
source.parent().node("OUT").setInput(0, rename)
entry = registry.for_rop(rop, native_scene.configuration(rop))
report = dict(result="running", device=device, edits=[], camera_edits=[])
try:

    def render():
        frame = entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
        status = entry.session.status()
        check(frame, status, BASE)
        return status

    baseline = previous = render()
    assert baseline["dense_field_conversions"] == 5, baseline
    for index in range(20):
        rename.parm("field_name").set("unused_field_" + str(index))
        status = render()
        report["edits"].append(status)
        assert status["dense_field_conversions"] == previous["dense_field_conversions"] + 1, status
        assert status["renderer_pid"] == baseline["renderer_pid"], status
        assert status["retained_dense_voxel_bytes"] == baseline["retained_dense_voxel_bytes"], status
        previous = status
    allocations = [row["device_allocation_bytes"] for row in report["edits"][4:]]
    assert max(allocations) - min(allocations) <= 8192, allocations
    values = source.parent().createNode("volumewrangle", "edit_temperature_only")
    values.setInput(0, rename)
    values.addSpareParmTuple(hou.FloatParmTemplate("temperature_value", "Temperature", 1, default_value=(2.0,)))
    values.setParms(dict(group="@name=temperature", snippet='@temperature=chf("temperature_value");'))
    source.parent().node("OUT").setInput(0, values)
    previous = render()
    report["voxel_edits"] = []
    for value in (3.0, 5.0, 2.0):
        values.parm("temperature_value").set(value)
        status = render()
        assert status["dense_field_conversions"] == previous["dense_field_conversions"] + 1, status
        report["voxel_edits"].append(status)
        previous = status
    camera = hou.node(rop.evalParm("camera"))
    for offset in (0.01, -0.01, 0):
        camera.parm("tx").set(offset)
        entry.session.wait(entry.apply(dict(camera_path=camera.path()), hou.time()))
        status = entry.session.status()
        assert status["dense_field_conversions"] == previous["dense_field_conversions"], status
        assert status["geometry_conversions"] == previous["geometry_conversions"], status
        report["camera_edits"].append(status)
    session = entry.session
    registry.close_all()
    deadline = time.monotonic() + 15
    while session.status()["state"] != "closed" and time.monotonic() < deadline:
        time.sleep(0.01)
    report["closed"] = session.status()
    assert (
        report["closed"]["owned_geometry_bytes"]
        == report["closed"]["device_allocation_bytes"]
        == report["closed"]["retained_dense_voxel_bytes"]
        == 0
    ), report["closed"]
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (dest / "cache-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], edits=len(report["edits"]))))
