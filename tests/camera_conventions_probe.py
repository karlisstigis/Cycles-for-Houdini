# SPDX-License-Identifier: Apache-2.0
"""Public host camera semantics, measured before accepting renderer conventions."""

from support.paths import artifacts_root
import json
import os
from pathlib import Path
import hou
import cycles_houdini as ch
from cycles_houdini import camera_properties

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage2/camera"
destination.mkdir(parents=True, exist_ok=True)
camera = hou.node("/obj").createNode("cam", "soho_camera_conventions")
camera.parm("orthowidth").set(4)
rop = hou.node("/out").createNode("cycles_ipr", "camera_conventions")
rop.parm("camera").set(camera.path())
rop.parm("soho_program").set(str(Path(__file__).with_name("soho_camera_probe.py")))
reports = []
for projection, z in ((0, 5), (1, 5), (1, 6), (2, 5), (3, 5), (4, 5)):
    camera.parm("projection").set(projection)
    camera.parm("tz").set(z)
    path = destination / f"{projection}-{z}.json"
    os.environ["CH_CAMERA_PROBE_REPORT"] = str(path)
    rop.render(frame_range=(hou.frame(), hou.frame()))
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(hom_token=camera.evalParm("projection"), menu=camera.parm("projection").menuItems(), z=z)
    if projection < 2:
        assert record["converted"]["orthographic"] == bool(projection), record
        if projection:
            assert abs(record["source"]["ortho_scale"] - 4 * 64 / 96) < 1e-9, record
            assert abs(record["converted"]["ortho_scale"] - 4 * 64 / 96) < 1e-6, record
    else:
        assert "Invalid requested-frame camera projection" in record["error"], record
        with ch.Session(device="CPU", width=8, height=8, samples=1) as session:
            try:
                session.apply(dict(camera_path=camera.path()))
            except RuntimeError as error:
                assert "Invalid requested-frame camera projection" in str(error), str(error)
            else:
                raise AssertionError("Unsupported native camera projection was silently rendered")
            assert session.status()["generation"] == 0
    reports.append(record)
assert reports[1]["converted"]["ortho_scale"] == reports[2]["converted"]["ortho_scale"]
camera_properties.install(camera)
camera.setParms(dict(projection=0, cycles_projection="panorama"))
panoramas = []
for kind, _label in camera_properties.PANORAMA_TYPES:
    camera.parm("cycles_panorama_type").set(kind)
    path = destination / (kind + ".json")
    os.environ["CH_CAMERA_PROBE_REPORT"] = str(path)
    rop.render(frame_range=(hou.frame(), hou.frame()))
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["source"]["projection_code"] == 2, record
    assert record["converted"]["panorama"]["type"] == kind, record
    assert not record["converted"]["orthographic"], record
    with ch.Session(device="CPU", width=8, height=8, samples=1) as session:
        frame = session.wait(session.apply(dict(camera_path=camera.path())))
        assert frame["complete"] and session.status()["generation"] == 1
    panoramas.append(record)
report = dict(result="passed", cameras=reports, panoramas=panoramas)
(destination / "camera-conventions.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report, indent=2))
