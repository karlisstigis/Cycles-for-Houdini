# SPDX-License-Identifier: Apache-2.0
"""Isolate instanced motion/camera tracking across five animation frames."""

import argparse, json, os, traceback
from pathlib import Path
import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from packed_deformation_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
f = Fixture(device)
rows = int(os.environ.get("CYCLES_HOUDINI_PACKED_FRAME_ROWS", "2"))
f.motion.parm("test_rows").set(rows)
f.motion.parm("test_speed").set(0.5)
f.targets.setParms(dict(rows=4, cols=4))
f.targets.parmTuple("size").set((6, 6))
f.obj.parm("tx").set(0.08)
f.camera.parm("tx").setExpression("rint($FF)-1+.12", hou.exprLanguage.Hscript)
report = dict(result="running", device=device, rows=rows, checks=[])
try:
    with ch.Session(
        device=device,
        width=f.width,
        height=f.height,
        samples=256,
        threads=2,
        interactive=True,
        collect_diagnostics=True,
    ) as session:
        for number in range(1, 6):
            hou.setFrame(number)
            frame = session.wait(session.apply(f.update(sop=f.nested, now=hou.time()), at_time=hou.time()))
            status = session.status()
            pixels = f.pixels(frame)
            x = -1.04
            y = -1.0
            measured = f.coverage(frame, x, y)
            image = f.dest / f"instances-frame-{number}-{device == 'CPU'}.png"
            output = oiio.ImageOutput.create(str(image))
            assert output.open(str(image), oiio.ImageSpec(f.width, f.height, 3, oiio.UINT8))
            assert output.write_image((np.clip(np.flipud(pixels[:, :, :3]), 0, 1) ** (1 / 2.2) * 255).astype(np.uint8))
            assert output.close()
            bbox = f.motion.geometry().boundingBox()
            field = f.pixels(frame, "position")
            column = round((x / 4 + 0.5) * f.width)
            row = round((y / 2.25 + 0.5) * f.height)
            check = dict(
                frame=number,
                time=hou.time(),
                coverage=measured,
                camera_matrix=list(f.camera.worldTransform().asTuple()),
                source_bounds=[tuple(bbox.minvec()), tuple(bbox.maxvec())],
                root_transform=list(f.nested.geometry().prim(0).intrinsicValue("packedfulltransform")),
                positions=field[row - 1 : row + 1, column - 1 : column + 1].mean((0, 1)).tolist(),
                status=status,
                image=str(image),
            )
            check["nominal_packed"] = ch._native.convert_preserved_packed_geometry(
                ch._native.preserve_packed_geometry(f.nested.path(), hou.time())
            )
            report["checks"].append(check)
            assert (
                status["geometry_resources"] == 1
                and status["object_resources"] == 16
                and not status["source_diagnostics"]
            ), status
            nominal = check["nominal_packed"]["instances"][5]
            assert np.allclose(nominal["minimum"][:2], (number * 0.4 - 1.6, -1.6), atol=1e-5), nominal
            assert abs(measured - 0.8) < 0.085, (number, measured)
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    (f.dest / "frames-report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result="passed", frames=len(report["checks"]))))
