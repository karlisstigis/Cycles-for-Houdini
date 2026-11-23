# SPDX-License-Identifier: Apache-2.0
"""Real child pin with a changed registration and ordinary prefs packages/HDAs."""

import argparse
import json
import os
from pathlib import Path
import traceback

import hou
from cycles_houdini import background_render, _native
from cycles_houdini.device_selection import set_devices

parser = argparse.ArgumentParser()
parser.add_argument("--report", required=True)
parser.add_argument("--registration", required=True)
parser.add_argument("--replacement", help="New registration; default is an invalid-host trap")
args = parser.parse_args()
report_path = Path(args.report).resolve()
report_path.parent.mkdir(parents=True, exist_ok=True)
registration = Path(args.registration).resolve()
prefs = Path(hou.getenv("HOUDINI_USER_PREF_DIR")).resolve()
assert registration.parent == prefs / "packages", (registration, prefs)
assert report_path.parents[1] in prefs.parents or prefs.is_relative_to(report_path.parent), (
    "Use isolated test preferences beside the report"
)
original = registration.read_bytes()
marker = prefs / "packages" / "cycles_pin_unrelated_fixture.json"
hda = prefs / "otls" / "cycles_pin_unrelated_fixture.hda"
assert not marker.exists() and not hda.exists(), "Fixture paths must be unused"
job = None
result = dict(result="running")
child_report = report_path.with_name(report_path.stem + "-child.json")
host = Path(_native.__file__).resolve().parents[1]
try:
    marker.write_text(json.dumps({"env": [{"CYCLES_PIN_UNRELATED_FIXTURE": "retained"}]}))
    hda.parent.mkdir(parents=True, exist_ok=True)
    geo = hou.node("/obj").createNode("geo", "background_pin_fixture", run_init_scripts=False)
    asset = geo.createNode("subnet", "pref_asset")
    asset = asset.createDigitalAsset(
        name="cycles_pin_pref_fixture", hda_file_name=str(hda), min_num_inputs=0, max_num_inputs=0
    )
    asset.destroy()
    grid = geo.createNode("grid")
    grid.setParms(dict(rows=2, cols=2))
    probe = geo.createNode("python", "verify_child")
    probe.setInput(0, grid)
    expected = dict(host=host.as_posix(), release=hou.getenv("CYCLES_HOUDINI_RELEASE_ID"))
    code = """import os,json,hou
from pathlib import Path
import cycles_houdini
from cycles_houdini import _native
expected = EXPECTED
actual = dict(python=cycles_houdini.__file__, native=_native.__file__,
    resources=hou.getenv('CYCLES_HOUDINI_RESOURCES'),release=hou.getenv('CYCLES_HOUDINI_RELEASE_ID'),
    marker=hou.getenv('CYCLES_PIN_UNRELATED_FIXTURE'),
    hda=hou.nodeType(hou.sopNodeTypeCategory(),'cycles_pin_pref_fixture') is not None,
    preferences=hou.getenv('HOUDINI_USER_PREF_DIR'))
assert Path(actual['python']).resolve().is_relative_to(Path(expected['host']))
assert Path(actual['native']).resolve().is_relative_to(Path(expected['host']))
assert Path(actual['resources']).resolve() == Path(expected['host'])/'resources'
assert actual['release'] == expected['release']
assert actual['marker'] == 'retained' and actual['hda'], actual
Path(REPORT).write_text(json.dumps(actual,indent=2))
""".replace("EXPECTED", repr(expected)).replace("REPORT", repr(child_report.as_posix()))
    probe.parm("python").set(
        "import os\nif os.getpid() != "
        + str(os.getpid())
        + ":\n"
        + "\n".join("    " + line for line in code.splitlines())
    )
    probe.setRenderFlag(True)
    cam = hou.node("/obj").createNode("cam", "background_pin_camera")
    cam.parm("tz").set(5)
    rop = hou.node("/out").createNode("cycles_ipr", "background_pin_render")
    set_devices(rop, "CPU")
    rop.setParms(
        dict(
            source_mode="sop",
            soppath=probe.path(),
            camera=cam.path(),
            resx=16,
            resy=16,
            samples=1,
            adaptive_sampling=0,
            use_denoise=0,
            viewer_interactive=0,
            render_threads=2,
            allowmotionblur=0,
            picture=str(report_path.with_suffix(".exr")),
        )
    )
    hou.hipFile.save(str(report_path.with_suffix(".hiplc")), save_to_recent_files=False)
    replacement = (
        Path(args.replacement).read_bytes()
        if args.replacement
        else json.dumps(
            dict(
                enable="houdini_version == '" + hou.applicationVersionString() + "'",
                load_package_once=True,
                hpath=[str(report_path.parent / "invalid_upgraded_host")],
                env=[
                    {"CYCLES_HOUDINI_HOST_ROOT": str(report_path.parent / "invalid_upgraded_host")},
                    {"CYCLES_HOUDINI_RELEASE_ID": "upgraded_registration_trap"},
                ],
            )
        ).encode()
    )
    registration.write_bytes(replacement)
    hou.putenv("CYCLES_HOUDINI_BACKGROUND_ROOT", str(report_path.parent / "jobs"))
    job = background_render.launch(rop)
    job.wait(timeout=120)
    status = job.status()
    assert status["state"] == "completed", status
    child = json.loads(child_report.read_text())
    assert Path(child["preferences"]).resolve() == prefs
    result.update(
        result="passed",
        job=str(job.directory),
        status=status,
        child=child,
        changed_registration=str(registration),
        original_host=str(host),
    )
except BaseException:
    result.update(result="failed", error=traceback.format_exc())
    if job is not None:
        result["job"] = str(job.directory)
    raise
finally:
    registration.write_bytes(original)
    marker.unlink(missing_ok=True)
    hda.unlink(missing_ok=True)
    report_path.write_text(json.dumps(result, indent=2))
