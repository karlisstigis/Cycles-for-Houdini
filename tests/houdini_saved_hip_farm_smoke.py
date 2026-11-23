# SPDX-License-Identifier: Apache-2.0
"""Fresh hython processes: saved HIP, relative assets, frame ranges and failure codes."""

from support.paths import artifacts_root
import argparse
import json
import os
from pathlib import Path
import subprocess
import time

import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import background_render as bg, device_selection

args = argparse.ArgumentParser()
args.add_argument("--device", default="CPU")
device = args.parse_args().device
root = artifacts_root() / "stage3/saved-hip-farm"
destination = root / str(time.time_ns())
project = destination / "Project With Spaces"
project.mkdir(parents=True)
cwd = destination / "Different Working Directory"
cwd.mkdir()
geo = hou.node("/obj").createNode("geo", "farm_subject", run_init_scripts=False)
box = geo.createNode("box")
box.geometry().saveToFile(str(project / "shape.bgeo.sc"))
source = geo.createNode("file")
source.parm("file").set("$HIP/shape.bgeo.sc")
source.setRenderFlag(True)
material = ch.create_material(name="farm_emission", shader="emission")
material.node("surface_shader").parmTuple("color").set((0.2, 0.4, 0.8))
material.node("surface_shader").parm("strength").set(1)
geo.parm("shop_materialpath").setExpression(
    f'ifs($F==3,"/mat/missing_farm_material","{material.path()}")', hou.exprLanguage.Hscript
)
camera = hou.node("/obj").createNode("cam", "farm_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
rop = hou.node("/out").createNode("cycles_ipr", "farm_render")
rop.setParms(
    dict(
        vobject=geo.path(),
        camera=camera.path(),
        resx=32,
        resy=24,
        samples=4,
        use_denoise=0,
        render_threads=2,
        allowmotionblur=0,
        trange=1,
        persistent_data=1,
        picture="$HIP/renders/$HIPNAME.$F4.exr",
    )
)
device_selection.set_devices(rop, [device])
upstream = hou.node("/out").createNode("geometry", "prepare_render_geometry")
upstream.setParms(dict(soppath=box.path(), sopoutput="$HIP/shape.bgeo.sc"))
rop.setInput(0, upstream)
for parm in rop.parmTuple("f"):
    parm.deleteAllKeyframes()
rop.parmTuple("f").set((1, 5, 1))
scene = project / "farm_artist.hiplc"
hou.hipFile.save(str(scene), save_to_recent_files=False)
script = Path(ch.__file__).resolve().parents[2] / "scripts/render_hip.py"
assert script.is_file(), script
executable = Path(hou.getenv("HFS")) / "bin/hython.exe"
checks = []


def render(name, options=(), failure=False):
    report = destination / (name + ".json")
    command = [str(executable), str(script), str(scene), "--rop", "/out/farm_render", "--report", str(report), *options]
    result = subprocess.run(command, cwd=cwd, env=bg._environment(), capture_output=True, text=True, timeout=90)
    (destination / (name + ".stdout.log")).write_text(result.stdout, encoding="utf-8")
    (destination / (name + ".stderr.log")).write_text(result.stderr, encoding="utf-8")
    assert (result.returncode != 0) == failure, (command, result.returncode, result.stderr)
    value = json.loads(report.read_text())
    assert value["result"] == ("failed" if failure else "passed"), value
    if not failure:
        assert "ALF_PROGRESS 100%" in result.stdout, result.stdout
    checks.append(dict(name=name, exit_code=result.returncode, report=value))
    return value


(project / "shape.bgeo.sc").unlink()
normal = render("saved-range")
assert (project / "shape.bgeo.sc").is_file(), "Connected geometry ROP did not execute before Cycles"
assert normal["summary"]["completed_frames"] == 5 and normal["summary"]["created_sessions"] == 1, normal
for frame in range(1, 6):
    image = oiio.ImageBuf(str(project / f"renders/farm_artist.{frame:04d}.exr"))
    pixels = image.get_pixels()
    assert pixels is not None, image.geterror()
    names = list(image.spec().channelnames)
    pixel = pixels[12, 16, [names.index(c) for c in ("R", "G", "B")]]
    diagnostics = json.loads(image.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
    if frame == 3:
        assert diagnostics and pixel[0] > 0.5 and pixel[1] < 0.1 and pixel[2] > 0.5, (pixel, diagnostics)
    else:
        np.testing.assert_allclose(pixel, (0.2, 0.4, 0.8), atol=0.002, rtol=0)
        assert not diagnostics, diagnostics
# Earlier-frame diagnostics survive a later clean frame and permissive skips.
issues = normal["summary"]["source_diagnostics"]
assert any(issue["frame"] == 3 and issue["source"] for issue in issues), issues
rop.parm("output_existing").set("skip")
hou.hipFile.save(str(scene), save_to_recent_files=False)
skipped = render("validated-skip")
assert skipped["summary"]["skipped_frames"] == 5 and skipped["summary"]["created_sessions"] == 0, skipped
assert any(issue["frame"] == 3 and issue["existing_output"] for issue in skipped["summary"]["source_diagnostics"]), (
    skipped
)
strict_skip = render("strict-skip", ["--fail-on-missing-inputs"], failure=True)
assert strict_skip["summary"]["skipped_frames"] == 2 and strict_skip["summary"]["error"], strict_skip
rop.parm("output_existing").set("overwrite")
hou.hipFile.save(str(scene), save_to_recent_files=False)
strict = render("strict-inputs", ["--fail-on-missing-inputs", "--output", "$HIP/strict.$F4.exr"], failure=True)
assert all((project / f"strict.{frame:04d}.exr").is_file() for frame in (1, 2)), strict
assert not any((project / f"strict.{frame:04d}.exr").exists() for frame in (3, 4, 5)), strict
assert any(issue["frame"] == 3 for issue in strict["summary"]["source_diagnostics"]), strict
subset = render("stepped-range", ["--start", "1", "--end", "5", "--step", "2", "--output", "$HIP/range.$F4.exr"])
assert subset["summary"]["completed_frames"] == 3, subset
assert [f for f in range(1, 6) if (project / f"range.{f:04d}.exr").is_file()] == [1, 3, 5]
render("missing-rop", ["--rop", "/out/missing"], failure=True)
blocked = project / "blocked.exr"
blocked.mkdir()
render("output-failure", ["--output", str(blocked)], failure=True)
assert not (project / "cycles_background").exists()
report = dict(result="passed", device=device, checks=checks, scope=__doc__)
(destination / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
(root / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", checks=len(checks))))
