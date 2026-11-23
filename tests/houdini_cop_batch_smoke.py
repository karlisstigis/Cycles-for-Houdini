# SPDX-License-Identifier: Apache-2.0
"""Native ROP writes all five COP-driven frames across source failure/repair."""

from support.paths import artifacts_root
import argparse, json, os, traceback
from pathlib import Path
import hou
import numpy as np
import OpenImageIO as OIIO
from shader_bypass_preview_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
args = parser.parse_args()
f = Fixture(args.device)
dest = artifacts_root() / "stage3/textures/cop-batch"
dest.mkdir(parents=True, exist_ok=True)
network = hou.node("/img").createNode("copnet", "batch_cops")
network.setParms(dict(setres=1, res1=32, res2=16, setprecision=1, precision=1))
source = network.createNode("constant", "source")
source.parm("signature").set("f3")
source.parmTuple("f3").set((0.2, 0.25, 0.5))
source.parm("f3r").setExpression(".1+$F*.1", hou.exprLanguage.Hscript)
f.texture.setParms(dict(colorspace="data", alpha_type="ignore"))
f.output.setNamedInput("surface", f.texture, "color")
f.texture.parm("filename").setExpression(
    'ifs($F==3,"op:/img/not_present/source","op:/img/batch_cops/source")', hou.exprLanguage.Hscript
)
f.rop.parm("picture").set(str(dest / "cop.$F4.exr"))
report = dict(result="running", device=args.device, frames=[])
try:
    hou.hipFile.save(str(dest / "cop-batch.hiplc"))
    f.rop.render(frame_range=(1, 5, 1))
    for frame in range(1, 6):
        path = dest / ("cop.%04d.exr" % frame)
        assert path.is_file()
        reader = OIIO.ImageInput.open(str(path))
        assert reader
        spec = reader.spec()
        pixels = reader.read_image(format=OIIO.FLOAT)
        issues = spec.get_string_attribute("cycles_houdini:source_diagnostics")
        reader.close()
        channels = [spec.channelnames.index(name) for name in ("R", "G", "B")]
        measured = np.asarray(pixels)[30:34, 30:34][:, :, channels].mean(axis=(0, 1))
        expected = (1, 0, 1) if frame == 3 else (0.1 + frame * 0.1, 0.25, 0.5)
        np.testing.assert_allclose(measured, expected, atol=2e-5, rtol=0)
        diagnostics = json.loads(issues) if issues else []
        assert bool(diagnostics) == (frame == 3), (frame, diagnostics)
        report["frames"].append(
            dict(frame=frame, measured=measured.tolist(), diagnostics=diagnostics, bytes=path.stat().st_size)
        )
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    (dest / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", frames=len(report["frames"]))))
