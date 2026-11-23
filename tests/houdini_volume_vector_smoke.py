# SPDX-License-Identifier: Apache-2.0
"""Rendered native vectors, source/shader repairs and demand-only ownership."""

import argparse
import json
import time
import traceback

import hou
import numpy as np
import OpenImageIO as oiio
from cycles_houdini import final_render, native_scene, registry
from volume_vector_fixture import BASE, CASES, Case, Fixture, SHADER, check

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
f = Fixture(device)
entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
report = dict(result="running", device=device, events=[])
try:

    def render(case, label, *, shader_only=False, camera_only=False):
        payload = (
            dict(material_resolver=native_scene.resolve_materials)
            if shader_only
            else dict(camera_path=f.camera.path())
            if camera_only
            else native_scene.capture(f.rop)
        )
        frame = entry.session.wait(entry.apply(payload, hou.time()))
        status = entry.session.status()
        image, measured = check(frame, status, case)
        report["events"].append(
            dict(
                label=label,
                mode=case.mode,
                root=case.root,
                measured=measured,
                status=status,
                diagnostics=entry.session.diagnostics(),
            )
        )
        return image, status

    for index, case in enumerate(CASES):
        f.control.parm("mode").set(case.mode)
        f.read.parm("attribute").set(case.root)
        image, status = render(case, "Recovery " + str(index))
        if index == 0:
            baseline = image
        if case.mode in (4, 7, 8) and case.root == "tint":
            difference = float(np.abs(image - baseline).mean())
            assert difference < 0.0012, (case, difference)
            report.setdefault("reference_differences", []).append(dict(mode=case.mode, difference=difference))
    f.control.parm("mode").set(1)
    _, broken = render(CASES[1], "Retained incomplete vector")
    for root, case in (("tint.x", Case(1, root="tint.x", color=(0.2, 0.2, 0.2), fields=0)), ("tint", CASES[1])):
        f.read.parm("attribute").set(root)
        _, status = render(case, "Shader-only " + root, shader_only=True)
        assert all(status[key] == broken[key] for key in ("extractions", "dense_field_conversions")), status
    f.control.parm("mode").set(0)
    _, before = render(BASE, "Repair retained source")
    for value in (0.7, 0.35):
        f.density.parm("initialval1").set(value)
        _, status = render(BASE, "Unrelated density " + str(value))
        assert status["volume_vector_merges"] == before["volume_vector_merges"], status
    f.vector.parm("initialval1").set(0.3)
    changed = Case(color=(0.3, 0.4, 0.8))
    _, status = render(changed, "Changed vector component")
    assert status["volume_vector_merges"] == before["volume_vector_merges"] + 1, status
    f.vector.parm("initialval1").set(0.2)
    _, before = render(BASE, "Restore vector component")
    for offset in (0.01, -0.01, 0):
        f.camera.parm("tx").set(offset)
        _, status = render(BASE, "Camera " + str(offset), camera_only=True)
        assert all(
            status[key] == before[key]
            for key in ("extractions", "dense_field_conversions", "volume_vector_merges", "geometry_conversions")
        ), status
    # Standard Volume Info Color needs the same scalar-triplet adapter.
    f.vector.parm("name").set("color")
    f.emit.setNamedInput("color", f.info, "color")
    render(BASE, "Standard color field")
    f.vector.parm("name").set("tint")
    f.emit.setNamedInput("color", f.read, "color")
    render(BASE, "Named color restored")
    # Shader demand can repeatedly add/remove one alias without keeping history.
    allocations = [[], []]
    for index in range(32):
        for phase, (root, case) in enumerate(
            (("tint.x", Case(root="tint.x", color=(0.2, 0.2, 0.2), fields=0)), ("tint", BASE))
        ):
            f.read.parm("attribute").set(root)
            _, status = render(case, "Demand cycle " + str(index) + " " + root, shader_only=True)
            allocations[phase].append(status["device_allocation_bytes"])
    # Keep the entire allocation history. Compare the last sixteen repetitions
    # after allocator/image-table warm-up, with exact alias ownership per edit.
    for values in allocations:
        assert max(values[16:]) - min(values[16:]) <= 8192, values
    report["warm_allocations"] = allocations
    session = entry.session
    registry.close_all()
    deadline = time.monotonic() + 15
    while session.status()["state"] != "closed" and time.monotonic() < deadline:
        time.sleep(0.01)
    report["closed"] = session.status()
    assert all(
        report["closed"][key] == 0
        for key in (
            "volume_vector_fields",
            "volume_vector_source_bytes",
            "owned_geometry_bytes",
            "device_allocation_bytes",
        )
    ), report["closed"]
    f.control.parm("mode").setExpression("(0,1,0,2,0)[max(0,min(4,int(hou.frame()+.5)-1))]", hou.exprLanguage.Python)
    f.rop.setParms(dict(persistent_data=1, picture=str(f.dest / "vector.$F4.exr")))
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    report["sequence"] = []
    for index, broken in enumerate((False, True, False, True, False), 1):
        image = oiio.ImageBuf(str(f.dest / f"vector.{index:04d}.exr"))
        assert not image.has_error, image.geterror()
        issues = json.loads(image.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert {d["code"] for d in issues} == (SHADER if broken else set()), issues
        pixels = image.get_pixels()
        assert np.isfinite(pixels).all()
        measured = pixels[21:27, 37:43, :3].mean((0, 1))
        if broken:
            assert measured[0] > measured[1] + 0.03 and measured[2] > measured[1] + 0.03, measured
        else:
            np.testing.assert_allclose(measured, (1.2, 1.4, 1.8), atol=0.006, rtol=0)
        np.testing.assert_allclose(pixels[24, 70, :3], (0.1, 0.7, 0.2), atol=1e-4)
        report["sequence"].append(dict(frame=index, diagnostics=issues, measured=measured.tolist()))
    report.update(result="passed", final=summary)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (f.dest / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], events=len(report["events"]))))
