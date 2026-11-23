# SPDX-License-Identifier: Apache-2.0
"""Independent camera-space heights, cached camera edits and unaffected Generated."""

import argparse
import json
import traceback

import cycles_houdini as ch
from camera_coordinate_displacement_fixture import Fixture, CASES

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
device = json.loads(device) if device.startswith("[") else device
f = Fixture(device)
report = dict(result="running", device=device, checks=[])
path = f.dest / "report.json"
try:
    with ch.Session(
        device=device,
        width=f.width,
        height=f.height,
        samples=16,
        threads=2,
        execution="worker",
        use_texture_cache=False,
        collect_diagnostics=True,
    ) as retained:
        for case in CASES:
            f.configure(case)
            states = [
                ("initial", lambda: None, None),
                ("camera translation", lambda: f.camera.parm("tz").set(5.6), True),
                ("camera rotation", lambda: f.camera.parm("rz").set(18), True),
                ("orthographic width", lambda: f.camera.parm("orthowidth").set(4.1), case == "window"),
                ("pixel aspect", lambda: f.rop.parm("pixel_aspect").set(1.2), case == "window"),
                ("perspective projection", lambda: f.camera.parm("projection").set(0), case == "window"),
                ("perspective focal length", lambda: f.camera.parm("focal").set(42), case == "window"),
                ("clip range only", lambda: f.camera.parm("near").set(0.2), False),
            ]
            for index, (label, edit, rebuild) in enumerate(states):
                before = retained.status()
                edit()
                frame, status = f.render(retained, f.update(camera_only=index > 0))
                with ch.Session(
                    device=device,
                    width=f.width,
                    height=f.height,
                    samples=16,
                    threads=2,
                    execution="worker",
                    use_texture_cache=False,
                ) as reference:
                    expected, _ = f.render(reference, f.reference_update())
                errors = f.compare(frame, expected)
                if index:
                    assert status["geometry_conversions"] == before["geometry_conversions"], status
                    assert status["displacement_rebuilds"] == before["displacement_rebuilds"] + int(rebuild), (
                        case,
                        label,
                        before,
                        status,
                    )
                report["checks"].append(dict(case=case, label=label, expected_rebuild=rebuild, status=status, **errors))
                path.write_text(json.dumps(report, indent=2))
        closed_source = retained.status()
    closed = retained.status()
    assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0, closed
    # Dimensions are immutable within a core Session. Viewer resizing uses a
    # new Session, so check both aspect and resolution with independent sources.
    for width, height in ((192, 96), (128, 128)):
        sized = Fixture(device, width=width, height=height)
        sized.configure("window")
        for projection in (1, 0):
            sized.camera.parm("projection").set(projection)
            with ch.Session(
                device=device,
                width=width,
                height=height,
                samples=16,
                threads=2,
                execution="worker",
                use_texture_cache=False,
            ) as resized:
                frame, status = sized.render(resized, sized.update())
            with ch.Session(
                device=device,
                width=width,
                height=height,
                samples=16,
                threads=2,
                execution="worker",
                use_texture_cache=False,
            ) as reference:
                expected, _ = sized.render(reference, sized.reference_update())
            report["checks"].append(
                dict(
                    case="window",
                    label="new-session dimensions",
                    width=width,
                    height=height,
                    projection=projection,
                    status=status,
                    **sized.compare(frame, expected),
                )
            )
    report.update(result="passed", closed=closed, final_source=closed_source)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
finally:
    path.write_text(json.dumps(report, indent=2))
assert report["result"] == "passed", report.get("error")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
