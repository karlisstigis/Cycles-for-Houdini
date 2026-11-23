# SPDX-License-Identifier: Apache-2.0
"""Measure public SOHO camera values inside an actual renderer invocation."""

import json
import os
from pathlib import Path
import hou
import soho
from cycles_houdini import _native, soho_adapter

now = soho.getDefaultedFloat("state:time", [hou.time()])[0]
path = soho.getDefaultedString("camera", ["/obj/cam1"])[0]
if not soho.initialize(now, path):
    soho.error("Camera probe could not initialize SOHO")
camera = next(iter(soho.objectList("objlist:camera")))
value = camera.evaluate({"projection": soho.SohoParm("projection", "string", ["perspective"], False)}, now)
record = dict(camera=path, raw_projection=value["projection"].Value[0])
try:
    record["source"] = soho_adapter._camera(camera, now, 96, 64)
    record["converted"] = _native.resolve_camera_motion(record["source"], [], 0.0, path, False, 96, 64)
except RuntimeError as error:
    record["error"] = str(error)
Path(os.environ["CH_CAMERA_PROBE_REPORT"]).write_text(json.dumps(record, indent=2), encoding="utf-8")
