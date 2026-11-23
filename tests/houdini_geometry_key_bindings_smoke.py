# SPDX-License-Identifier: Apache-2.0
"""Independent emission, local binding repair, deformation and ordered output."""

import argparse
import copy
import json
from pathlib import Path
import traceback

import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import final_render, registry
from geometry_key_binding_fixture import Fixture, COLORS


parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.add_argument("--report")
args = parser.parse_args()
f = Fixture(args.device)
path = Path(args.report) if args.report else f.dest / "report.json"
report = dict(result="running", scope=__doc__, device=f.target_ids, checks=[])
entry = None


def save():
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")


try:
    entry = f.session()
    authored = f.authored()
    for index, (label, action) in enumerate(f.steps()):
        report.update(phase=label)
        save()
        action()
        frame = f.update()
        status = entry.session.status()
        measured = f.assert_pixels(frame)
        assert status["state"] == "complete" and not status["error"], status
        assert status["render_device_ids"] == f.target_ids, status
        assert {issue["code"] for issue in status["source_diagnostics"]} == f.codes, status
        assert status["geometry_resources"] == 3 and status["points"] == 3 and status["curves"] == 2, status
        assert f.authored() == authored, "Binding capture changed an authored material"
        assert all(
            issue["source"] and issue["message"] and issue["action"] for issue in status["source_diagnostics"]
        ), status
        if report["checks"]:
            assert status["renderer_pid"] == report["checks"][0]["status"]["renderer_pid"], status
        if "camera navigation" in label:
            previous = report["checks"][-1]["status"]
            assert status["geometry_conversions"] == previous["geometry_conversions"], (previous, status)
        if f.config.get("animate"):
            assert status["motion_cooks"] > 0 and not status["motion_warnings"], status
        report["checks"].append(
            dict(
                label=label,
                expected_curves=f.expected_curves.tolist(),
                expected_particles=f.expected_particles.tolist(),
                measured=measured,
                status=status,
                references=entry.session.native_material_references(),
            )
        )
        save()

    # Inspect independently owned positions from the no-stylesheet path. Each
    # source key translates at 1.44 units/sec; shutter endpoints are +/-1/96s.
    f.deformation()
    lease = ch._native.preserve_deformation(f.curves.path(), 0.0, hou.fps(), 0.5, 3, f.materials[0].path())
    owned = ch._native.convert_preserved_deformation(lease)
    del lease
    assert len(owned["components"]) == 1 and owned["components"][0]["kind"] == "curves", owned
    samples = owned["components"][0]["samples"]
    assert len(samples) == 3, owned
    np.testing.assert_allclose([sample["first"][0] for sample in samples], (-1.0, -1.015, -0.985), atol=2e-7, rtol=0)
    np.testing.assert_allclose([sample["sums"][0] for sample in samples], (0.0, -0.12, 0.12), atol=2e-6, rtol=0)
    report["deformation_reference"] = owned
    save()

    entry.session.close(wait=True)
    closed = entry.session.status()
    assert closed["state"] == "closed" and closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0, (
        closed
    )
    report["closed"] = closed
    registry.close_all()

    # Five ordered final frames use the same component-local recovery policy.
    f.uniform()
    f.obj.parm("tx").set(0)
    healthy = copy.deepcopy(f.config)
    missing = copy.deepcopy(healthy)
    missing["point_paths"][0] = "/mat/key_binding_batch_missing"
    varying = copy.deepcopy(healthy)
    varying["primitive"] = [m.path() for m in f.materials[2:4]]
    varying["point_paths"] = ["", ""]
    varying["vertex_paths"][0] = [f.materials[4].path(), "", "", ""]
    expression = (
        "import json\nframe=int(hou.frame())\nreturn json.dumps("
        + repr(missing)
        + " if frame==2 else "
        + repr(varying)
        + " if frame==4 else "
        + repr(healthy)
        + ")"
    )
    f.curves.parm("payload").setExpression(expression, hou.exprLanguage.Python)
    f.rop.setParms(dict(picture=str(f.dest / "keys.$F4.exr"), persistent_data=1))
    report.update(phase="Writing five ordered final frames")
    save()
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    report["batch"] = []
    for number in range(1, 6):
        output = f.dest / f"keys.{number:04d}.exr"
        reader = oiio.ImageInput.open(str(output))
        assert reader, output
        spec = reader.spec()
        pixels = reader.read_image(format=oiio.FLOAT)
        reader.close()
        channels = list(spec.channelnames)
        prefix = "Combined." if "Combined.R" in channels else ""
        rgb = pixels[:, :, [channels.index(prefix + c) for c in "RGB"]]
        positions = pixels[:, :, [channels.index("position." + c) for c in "XYZ"]]
        alpha = pixels[:, :, channels.index(prefix + "A")]

        def sample(x, y):
            mask = (alpha > 0.999) & (np.abs(positions[:, :, 0] - x) < 0.06) & (np.abs(positions[:, :, 1] - y) < 0.10)
            assert mask.sum() >= 4, (number, x, y, int(mask.sum()))
            return rgb[mask].mean(0), positions[mask, 2]

        expected = np.asarray(COLORS[2:4])
        expected[0] = (1, 0, 1) if number == 2 else expected[0]
        measured = np.asarray([sample(x, 0)[0] for x in (-1, 1)])
        np.testing.assert_allclose(measured, expected, atol=8e-5, rtol=0)
        control, z = sample(0, -1.5)
        np.testing.assert_allclose(control, (0.12, 0.24, 0.36), atol=8e-5, rtol=0)
        np.testing.assert_allclose(z, 0.15, atol=3e-5, rtol=0)
        issues = json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        codes = {"material.missing"} if number == 2 else {"material.unsupported_binding"} if number == 4 else set()
        assert {issue["code"] for issue in issues} == codes, issues
        report["batch"].append(dict(frame=number, path=str(output), curves=measured.tolist(), diagnostics=issues))
        save()
    report.update(result="passed", phase="complete", final=summary)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    try:
        registry.close_all()
    finally:
        save()
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]), report=str(path))))
