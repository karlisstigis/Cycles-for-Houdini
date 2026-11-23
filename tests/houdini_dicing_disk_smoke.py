# SPDX-License-Identifier: Apache-2.0
"""Saved-HIP/SOHO disk renders use final dicing policy and a separate camera."""

import json
import os
from pathlib import Path
import hou
import OpenImageIO as oiio
from cycles_houdini import final_render, object_properties
from shader_bypass_preview_fixture import Fixture

dest = Path(os.environ["CH_DICING_DISK_REPORT"])
dest.parent.mkdir(parents=True, exist_ok=True)
f = Fixture("CPU")
f.output.setNamedInput("surface", f.emission, "emission")
f.emission.parmTuple("color").set((0.2, 0.4, 0.8))
object_properties.install(f.geo)
f.geo.setParms(
    dict(
        cycles_subdivision_override=1,
        cycles_subdivision_ipr=0,
        cycles_subdivision_type="linear",
        cycles_subdivision_adaptive=1,
    )
)
camera = f.scene.createNode("cam", "disk_dicing_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=8))
f.rop.setParms(
    dict(
        dicing_camera=camera.path(),
        dicing_rate=1,
        ipr_dicing_rate=100,
        tessellation_update=0,
        max_subdivisions=12,
        picture=str(dest.parent / "dicing-disk.exr"),
    )
)
path = f.rop.path()
camera_path = camera.path()
hip = dest.parent / "dicing-policy.hiplc"
hou.hipFile.save(str(hip))
hou.hipFile.load(str(hip), suppress_save_prompt=True)
rop = hou.node(path)
counts = []
for selected in (camera_path, ""):
    rop.parm("dicing_camera").set(selected)
    rop.render(frame_range=(1, 1, 1))
    summary = rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 1, summary
    status = summary["last_status"]
    assert not status["error"], status
    assert status["subdivision_control_faces"] == 1, status
    counts.append(status["subdivision_triangles"])
    image = oiio.ImageBuf(rop.evalParm("picture"))
    assert not image.has_error, image.geterror()
    pixel = image.get_pixels()[32, 32, :3]
    assert max(abs(pixel - (0.2, 0.4, 0.8))) < 1e-5, pixel
assert counts[0] < counts[1], counts
dest.write_text(
    json.dumps(
        dict(
            result="passed",
            fixed_camera_triangles=counts[0],
            following_camera_triangles=counts[1],
            saved_hip_reload=True,
            ipr_disabled_final_subdivides=True,
        ),
        indent=2,
    )
)
