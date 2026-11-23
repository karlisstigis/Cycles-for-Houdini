# SPDX-License-Identifier: Apache-2.0
"""Independent point-style emission references, local recovery and five output frames."""

import argparse
import copy
import json
from pathlib import Path
import traceback
import hou
import numpy as np
import OpenImageIO as oiio
from cycles_houdini import final_render, registry
from point_subject_stylesheet_fixture import Fixture, sheet
from geometry_key_binding_fixture import COLORS, TINT

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.add_argument("--report")
args = parser.parse_args()
f = Fixture(args.device)
path = Path(args.report) if args.report else f.dest / "report.json"
report = dict(result="running", scope=__doc__, device=f.target_ids, checks=[])


def save():
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")


try:
    entry = f.session()
    authored = f.authored()
    for label, action in f.steps():
        report.update(phase=label)
        save()
        action()
        frame = f.update()
        status = entry.session.status()
        measured = f.assert_pixels(frame)
        assert status["state"] == "complete" and not status["error"], status
        assert status["render_device_ids"] == f.target_ids and status["geometry_resources"] == 3, status
        assert status["points"] == 3 and status["curves"] == 2 and f.authored() == authored, status
        assert {issue["code"] for issue in status["source_diagnostics"]} == f.codes, status
        assert all(
            issue["source"] and issue["message"] and issue["action"] for issue in status["source_diagnostics"]
        ), status
        if report["checks"]:
            previous = report["checks"][-1]
            assert status["renderer_pid"] == report["checks"][0]["status"]["renderer_pid"], status
            if "camera navigation" in label:
                assert status["geometry_conversions"] == previous["status"]["geometry_conversions"], (previous, status)
        report["checks"].append(
            dict(
                label=label,
                measured=measured,
                expected_curves=f.expected_curves.tolist(),
                expected_particles=f.expected_particles.tolist(),
                status=status,
                references=entry.session.native_material_references(),
            )
        )
        save()
    entry.session.close(wait=True)
    closed = entry.session.status()
    assert closed["state"] == "closed" and closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0, (
        closed
    )
    report["closed"] = closed
    registry.close_all()

    # Artist-authored native styles change on the requested frame. Frame two
    # loses a material, frame four has unrepresentable curve-key variation;
    # frame three/five repair. Every image must retain independent mesh height.
    f.local_points(5)
    f.obj.parm("tx").set(0)
    f.camera.parm("tx").set(0)
    healthy = copy.deepcopy(f.config)
    missing = copy.deepcopy(healthy)
    missing["point_style"][0] = sheet(material="/mat/batch_point_style_missing")
    varying = copy.deepcopy(healthy)
    varying["point_style"][0] = [sheet(5), sheet(1), sheet(5), sheet(5)]
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
    f.rop.setParms(dict(picture=str(f.dest / "point-styles.$F4.exr"), persistent_data=1))
    report.update(phase="Writing five ordered final frames")
    save()
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    report["batch"] = []
    for number in range(1, 6):
        output = f.dest / f"point-styles.{number:04d}.exr"
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

        expected = np.asarray(COLORS[2:4]) * 5
        if number == 2:
            expected[0] = (1, 0, 1)
        if number == 4:
            expected[0] = np.asarray(COLORS[2]) * 2
        measured = np.asarray([sample(x, 0)[0] for x in (-1, 1)])
        np.testing.assert_allclose(measured, expected, atol=8e-5, rtol=0)
        particle = np.asarray([sample(x, 1.25)[0] for x in (-1, 0, 1)])
        np.testing.assert_allclose(particle, np.asarray(TINT) * np.asarray([5, 6, 7])[:, None], atol=8e-5, rtol=0)
        control, z = sample(0, -1.5)
        np.testing.assert_allclose(control, (0.12, 0.24, 0.36), atol=8e-5, rtol=0)
        np.testing.assert_allclose(z, 0.15, atol=3e-5, rtol=0)
        issues = json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        codes = {"material.missing"} if number == 2 else {"material.unsupported_binding"} if number == 4 else set()
        assert {issue["code"] for issue in issues} == codes, issues
        report["batch"].append(
            dict(
                frame=number,
                path=str(output),
                curves=measured.tolist(),
                particles=particle.tolist(),
                diagnostics=issues,
            )
        )
        save()
    report.update(result="passed", phase="complete", frames=5, final=summary)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    try:
        registry.close_all()
    finally:
        save()
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]), report=str(path))))
