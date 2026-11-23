# SPDX-License-Identifier: Apache-2.0
"""Focused packaged CPU check that OpenPGL actually trains a guide."""

import argparse
import json
import os
from pathlib import Path
import re
import uuid

import hou
import cycles_houdini as ch
from cycles_houdini import native_scene
from heavy_renderer_suites import render, scene


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--report", required=True, type=Path)
args = parser.parse_args()
args.report.parent.mkdir(parents=True, exist_ok=True)
logs = args.report.parent / "guiding-worker-logs" / uuid.uuid4().hex
logs.mkdir(parents=True, exist_ok=False)
os.environ["CYCLES_HOUDINI_WORKER_LOG_DIR"] = str(logs)
os.environ["CYCLES_HOUDINI_TRACE_KERNELS"] = "1"
caps = ch.capabilities()
assert caps["path_guiding"] is True, "Packaged runtime lacks OpenPGL path guiding"
assert caps["image_iris"] is True, "Packaged runtime lacks Image Iris"
rop = hou.node("/out").createNode("cycles_ipr", "guiding_control_probe")
assert rop.parm("use_guiding").eval() == 0
assert rop.parm("deterministic_guiding").eval() == 1
rop.parm("use_guiding").set(1)
rop.parm("device_mode").set("gpu")
saved_gpu = native_scene.rendering_configuration(rop, hou.time(), interactive=False)
rop.parm("device_mode").set("cpu")
saved_cpu = native_scene.rendering_configuration(rop, hou.time(), interactive=False)
assert saved_gpu["use_guiding"] and saved_cpu["use_guiding"]
assert saved_gpu["deterministic_guiding"] and saved_cpu["deterministic_guiding"]
image, metadata = render(scene("courtyard"), "CPU", 256, 301, guiding=True)
log_files = sorted(logs.glob("*.log"))
assert log_files, "CPU render created no worker log for this probe invocation"
content = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in log_files)
updates = re.findall(r"Path guiding field valid:\s*(?:1|true)", content, re.IGNORECASE)
assert updates, "CPU render completed without a valid trained OpenPGL field"
report = dict(
    result="passed",
    path_guiding=caps["path_guiding"],
    image_iris=caps["image_iris"],
    rop_choice_survives_gpu_switch=True,
    training_updates=len(updates),
    worker_log_dir=str(logs),
    worker_logs=[str(path) for path in log_files],
    render=metadata,
    mean_rgb=image.mean((0, 1)).tolist(),
)
args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
