# SPDX-License-Identifier: Apache-2.0
"""Break and repair native dense fields and effective bindings in one worker."""

import argparse
import json
import time
import traceback

import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import final_render, native_scene, registry
from dense_volume_fixture import BASE, CASES, Case, IGNORED, CONFLICT, fixture, check

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
rop, source, assignment, dest = fixture(device)
entry = registry.for_rop(rop, native_scene.configuration(rop))
report = dict(result="running", device=device, checks=[])
try:
    owned = ch._native.convert_preserved_volume(ch._native.preserve_volume(source.path(), hou.time()))
    fields = {field["name"]: field for field in owned["fields"]}
    assert set(fields) == {"density", "temperature", "color", "velocity", "custom_integer"}, fields
    np.testing.assert_allclose(fields["color"]["voxel_zero"], (0.15, 0.35, 0.7), atol=1e-6)
    np.testing.assert_allclose(fields["velocity"]["voxel_zero"], (1, 0, 0), atol=1e-6)
    assert fields["custom_integer"]["voxel_zero"][0] == 7, fields
    assert all(field["active_voxels"] == 19 * 13 * 11 for field in fields.values()), fields
    report["typed_fields"] = owned
    for case in CASES:
        source.parm("mode").set(case.mode)
        assignment.parm("binding").set(case.binding)
        frame = entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
        status = entry.session.status()
        observation = check(frame, status, case)
        report["checks"].append(dict(mode=case.mode, binding=case.binding, status=status, **observation))
        if case.density is None:
            for offset in (0.01, 0):
                source.parent().parm("tx").set(offset)
                frame = entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
                moved = entry.session.status()
                check(frame, moved, case)
                assert all(
                    moved[key] == status[key]
                    for key in ("dense_field_conversions", "geometry_conversions", "extractions")
                ), moved
                report.setdefault("unavailable_navigation", []).append(moved)
    # Omitting a source from the next authoritative capture must release its
    # recovery cache and warnings, while the other SOP components remain usable.
    source.parm("mode").set(8)
    entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
    assert entry.session.status()["retained_dense_voxel_bytes"] > 0
    source.parent().node("OUT").setInput(0, None)
    entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
    deleted = entry.session.status()
    assert deleted["retained_dense_voxel_bytes"] == 0 and not deleted["source_diagnostics"], deleted
    report["source_removal"] = deleted
    source.parm("mode").set(0)
    source.parent().node("OUT").setInput(0, assignment)
    # Merge compacts its input list when port zero is disconnected.
    source.parent().node("OUT").setInput(1, source.parent().node("sentinel_material"))
    frame = entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
    check(frame, entry.session.status(), BASE)
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
    # Exercise the actual ordered ROP output path with frame-correct recovery.
    # The fourth frame skips NaN density and uses the later field's material.
    sequence = (BASE, Case(4, 0.35, IGNORED), BASE, Case(96, 1.4, IGNORED | CONFLICT, 1), BASE)
    source.parm("mode").setExpression("(0,4,0,96,0)[max(0,min(4,int(hou.frame()+.5)-1))]", hou.exprLanguage.Python)
    assignment.parm("binding").setExpression("1 if int(hou.frame()+.5)==4 else 0", hou.exprLanguage.Python)
    rop.setParms(dict(persistent_data=1, picture=str(dest / "frame.$F4.exr")))
    rop.render(frame_range=(1, 5, 1))
    summary = rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    report["sequence"] = []
    for index, case in enumerate(sequence, 1):
        output = oiio.ImageBuf(str(dest / f"frame.{index:04d}.exr"))
        assert not output.has_error, output.geterror()
        spec = output.spec()
        channels = list(spec.channelnames)
        image = np.flipud(output.get_pixels()[:, :, [channels.index(c) for c in ("R", "G", "B")]])
        plane = dict(pixels=image.astype(np.float32).tobytes(), channels=3)
        issues = json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        state = dict(
            state="complete",
            motion_warnings=[],
            source_diagnostics=issues,
            geometry_resources=2,
            object_resources=2,
            points=0,
        )
        observation = check(dict(width=80, height=48, planes=dict(combined=plane)), state, case)
        report["sequence"].append(dict(frame=index, diagnostics=issues, **observation))
    report["final"] = summary
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (dest / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
