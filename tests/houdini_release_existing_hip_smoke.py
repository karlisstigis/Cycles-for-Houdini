# SPDX-License-Identifier: Apache-2.0
"""Open an existing saved HIP on the release runtime and verify a healthy graph."""

import argparse
import hashlib
import json
from pathlib import Path
import hou
import cycles_houdini as ch

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--device", default="CPU")
parser.add_argument("--hip", type=Path, required=True)
parser.add_argument("--report", type=Path, required=True)
args = parser.parse_args()
before = hashlib.sha256(args.hip.read_bytes()).hexdigest()
hou.hipFile.load(str(args.hip), suppress_save_prompt=True, ignore_load_warnings=False)
material = hou.node("/mat/offline_test")
assert material is not None and material.type().name() == "cycles::material::1"
shader = material.node("surface_shader")
output = material.node("surface_output")
assert shader.type().name() == "cycles::principled_bsdf::1"
assert output.type().name() == "cycles::surface_output::1"
assert output.input(0) == shader, "Authored material connection changed"
assert shader.evalParm("roughness") == 0.314159, "Authored roughness changed"
graphs = [material, shader, output]
connections = sum(len(node.inputConnections()) for node in graphs)
assert hashlib.sha256(args.hip.read_bytes()).hexdigest() == before
args.report.write_text(
    json.dumps(
        dict(
            result="passed",
            hip=str(args.hip),
            sha256=before,
            houdini=hou.applicationVersionString(),
            nodes=[node.path() for node in graphs],
            connections=connections,
            scope="Healthy load of pre-existing fixture; no save or artist modification",
        ),
        indent=2,
    ),
    encoding="utf-8",
)
