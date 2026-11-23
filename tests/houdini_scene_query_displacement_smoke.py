# SPDX-License-Identifier: Apache-2.0
"""Query faults retain independent height, surface use and assignment identity."""

import argparse
import json
from pathlib import Path
import traceback

import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene, registry
from scene_query_displacement_fixture import Fixture, QUERIES, plane

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.add_argument("--report", type=Path)
args = parser.parse_args()
device = json.loads(args.device) if args.device.startswith("[") else args.device
f = Fixture(device)
path = args.report or f.dest / "report.json"
path.parent.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=device, scope=__doc__, checks=[])
try:
    with ch.Session(
        device=device,
        width=f.width,
        height=f.height,
        samples=32,
        threads=2,
        execution="worker",
        use_texture_cache=False,
    ) as session:
        for kind in QUERIES:
            modes = [
                ("valid", "true"),
                ("whole", "true"),
                ("whole", "true"),
                ("add", "true"),
                ("vector_add", "true"),
                ("coupled", "true"),
                ("normal", "true"),
                ("add", "both"),
                ("valid", "true"),
                ("disabled", "true"),
                ("linked_disabled", "true"),
                ("disconnected", "true"),
                ("unused", "true"),
                ("bump", "bump"),
            ]
            if kind == "ambient_occlusion":
                modes[-1:-1] = [("global_ao", "true"), ("linked_global_ao", "true")]
            for index, (mode, method) in enumerate(modes):
                f.configure(kind, mode, method)
                frame, status = f.render(session, f.update())
                measured = f.check(frame, status)
                with ch.Session(
                    device=device,
                    width=f.width,
                    height=f.height,
                    samples=32,
                    threads=2,
                    execution="worker",
                    use_texture_cache=False,
                ) as reference:
                    expected, ref_status = f.render(reference, f.reference_update())
                    assert not ref_status["source_diagnostics"], ref_status
                errors = f.compare(frame, expected)
                report["checks"].append(dict(case=f.case, step=index, status=status, errors=errors, **measured))
                path.write_text(json.dumps(report, indent=2))
                # Identical accepted captures retain both the warning and the
                # original geometry; repairing never needs a manual refresh.
                if index == 2:
                    before = status
                    frame, status = f.render(session, f.update())
                    f.check(frame, status)
                    assert (
                        status["geometry_conversions"] == before["geometry_conversions"]
                        and status["displacement_rebuilds"] == before["displacement_rebuilds"]
                    ), status
        f.configure("raycast", "add")
        frame, status = f.render(session, f.update())
        before = status
        f.camera.parm("tx").set(0.05)
        frame, status = f.render(session, f.update(camera_only=True))
        f.check(frame, status)
        assert (
            status["geometry_conversions"] == before["geometry_conversions"]
            and status["displacement_rebuilds"] == before["displacement_rebuilds"]
        ), status
        report["checks"].append(dict(case="camera edit with query warning", status=status))
    report["closed"] = session.status()
    assert report["closed"]["owned_geometry_bytes"] == report["closed"]["device_allocation_bytes"] == 0
    f.configure("raycast", "valid", surface_query=False)
    f.rop.setParms(dict(persistent_data=1, picture=str(f.dest / "sequence.$F4.exr")))
    sequence = ("valid", "whole", "add", "valid", "vector_add")

    def edit(rop, event_type, at_time):
        if event_type == hou.ropRenderEventType.PreFrame:
            f.configure("raycast", sequence[int(round(hou.timeToFrame(at_time))) - 1], surface_query=False)

    f.rop.addRenderEventCallback(edit, run_before_script=True)
    try:
        f.rop.render(frame_range=(1, 5, 1))
    finally:
        f.rop.removeRenderEventCallback(edit)
    report["batch"] = []
    for number, mode in enumerate(sequence, 1):
        filename = f.dest / f"sequence.{number:04}.exr"
        image = oiio.ImageInput.open(str(filename))
        assert image, filename
        spec = image.spec()
        pixels = image.read_image(format=oiio.FLOAT)
        issues = json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        f.check_diagnostics(issues, warning=mode in ("whole", "add", "vector_add"))
        columns = list(spec.channelnames)
        alpha = pixels[..., columns.index("A")]
        x = pixels[..., columns.index("position.X")]
        z = pixels[..., columns.index("position.Z")]
        target = (alpha > 0.999) & (np.abs(x + 0.9) < 0.4)
        assert target.sum() > 100 and np.isfinite(pixels).all()
        np.testing.assert_allclose(z[target], 0 if mode == "whole" else 0.25, atol=3e-5, rtol=0)
        report["batch"].append(dict(frame=number, mode=mode, diagnostics=issues, path=str(filename)))
        image.close()
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
finally:
    registry.close_all()
    path.write_text(json.dumps(report, indent=2))
assert report["result"] == "passed", report.get("error")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]), frames=len(report.get("batch", [])))))
