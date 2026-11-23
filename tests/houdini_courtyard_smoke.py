# SPDX-License-Identifier: Apache-2.0
"""Exercise unpacked meshes, hair, points, VDBs and genuine instances in one SOP."""

from support.paths import artifacts_root
import json
import os
from pathlib import Path
import runpy

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
generator = runpy.run_path(str(root / "examples/sop-fx-courtyard/build_scene.py"), run_name="courtyard_generator")
# This standalone workload must not overwrite the showcase producer's HIP and
# OCIO files: later consumer fixtures verify and stage those exact receipts.
generator["build"].__globals__["DESTINATION"] = artifacts_root() / "examples/courtyard-smoke-fixture"
rop, scene = generator["build"]()
render = generator["render"](rop, 320, 180, 16, "CPU")
status = render["status"]
assert status["packed_instances"] == 54 and status["geometry_resources"] == 5, status
assert status["points"] == 360 and status["curves"] == 265, status
assert status["displacement_rebuilds"] == 1 and status["mesh_variant_resources"] == 2, status
report = dict(
    result="passed",
    scene=scene,
    render=render,
    scope="Combined native SOP workload and retirement; analytic feature correctness is established in separate regressions",
)
destination = artifacts_root() / "examples/sop-fx-courtyard/houdini-courtyard-smoke.json"
destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(
    json.dumps(
        dict(
            result="passed",
            packed_instances=status["packed_instances"],
            points=status["points"],
            curves=status["curves"],
        )
    )
)
