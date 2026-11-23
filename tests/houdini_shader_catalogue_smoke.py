# SPDX-License-Identifier: Apache-2.0
"""Independent pinned socket expectations, deterministic catalogue and ownership."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
import json
import math
import os
from pathlib import Path
import subprocess

import cycles_houdini as ch

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/materials"
destination.mkdir(parents=True, exist_ok=True)
catalogue = ch.shader_catalogue()
assert catalogue == ch.shader_catalogue(), "Repeated reflection is not deterministic"
assert catalogue["schema_version"] == 1
lock = json.loads((root / "source.lock.json").read_text(encoding="utf-8-sig"))
assert catalogue["engine_commit"] == lock["cycles"]["commit"]
definitions = {item["engine_type"]: item for item in catalogue["definitions"]}
assert len(definitions) == len(catalogue["definitions"])
assert list(definitions) == sorted(definitions)
assert len({item["id"] for item in definitions.values()}) == len(definitions)
assert not {"mesh", "object", "scene", "shader", "light"} & set(definitions)
for node in definitions.values():
    assert node["id"] == f"cycles/{node['engine_type']}/1"
    assert node["definition_version"] == 1
    for direction in ("inputs", "outputs"):
        sockets = node[direction]
        assert len({socket["id"] for socket in sockets}) == len(sockets)
        for socket in sockets:
            if socket["enum_values"]:
                tokens = [item["token"] for item in socket["enum_values"]]
                if node["engine_type"] == "warning" and socket["id"] == "severity":
                    # Authored diagnostic severity uses its semantic order.
                    assert tokens == ["info", "warning", "error"]
                    assert [item["engine_value"] for item in socket["enum_values"]] == [0, 1, 2]
                else:
                    assert tokens == sorted(tokens)
                assert socket["default"] in {item["token"] for item in socket["enum_values"]}


def socket(node, name, direction="inputs"):
    return next(item for item in definitions[node][direction] if item["id"] == name)


# Expectations read from the pinned definitions, independent of the existing
# convenience materials (whose default emission strength is intentionally 1).
assert socket("emission", "strength")["default"] == 10
assert all(abs(value - 0.8) < 1e-7 for value in socket("emission", "color")["default"])
assert socket("diffuse_bsdf", "roughness")["default"] == 0
assert socket("diffuse_bsdf", "normal")["default_links"] == ["normal"]
assert socket("diffuse_bsdf", "surface_mix_weight")["internal_svm"]
assert socket("value", "value")["type"] == "float"
assert not socket("value", "value")["linkable"]
assert socket("value", "value", "outputs")["linkable"]
assert {item["token"] for item in socket("noise_texture", "dimensions")["enum_values"]} == {"1D", "2D", "3D", "4D"}
assert socket("noise_texture", "dimensions")["default"] == "3D"
assert socket("noise_texture", "type")["default"] == "fBM"
assert socket("noise_texture", "vector")["default_links"] == ["texture_generated"]
assert socket("rgb_ramp", "ramp")["type"] == "array_color" and socket("rgb_ramp", "ramp")["default"] == []
assert socket("rgb_ramp", "ramp_alpha")["type"] == "array_float"
assert socket("rgb_ramp", "interpolate")["default"] is True
assert socket("output", "surface")["type"] == "closure"
assert socket("output", "volume")["type"] == "closure"
assert socket("output", "displacement")["type"] == "vector"
assert all(math.isfinite(value) for value in socket("noise_texture", "vector")["default"])

# The separately linked harness must describe the same locked build.
output = destination / "headless-shader-catalogue.json"
run = subprocess.run(
    [
        str(runtime_executable("ch_harness.exe")),
        "--resources",
        os.environ["CYCLES_HOUDINI_RESOURCES"],
        "--shader-catalogue",
        "--output",
        str(output),
    ],
    capture_output=True,
    text=True,
    timeout=30,
)
assert run.returncode == 0, run.stderr
assert json.loads(output.read_text(encoding="utf-8")) == catalogue
with ch.Session(device="CPU", width=32, height=32, samples=2) as session:
    session.wait(session.calibration())
    frame = session.frame_info()
    status = session.status()
    assert ch.shader_catalogue() == catalogue
    assert session.frame_info() == frame
    assert session.status()["generation"] == status["generation"]
    assert session.status()["geometry_conversions"] == status["geometry_conversions"]
assert session.status()["device_allocation_bytes"] == 0

(destination / "native-shader-catalogue.json").write_text(json.dumps(catalogue, indent=2), encoding="utf-8")
report = dict(
    result="passed",
    definitions=len(definitions),
    input_sockets=sum(len(node["inputs"]) for node in definitions.values()),
    output_sockets=sum(len(node["outputs"]) for node in definitions.values()),
    deterministic=True,
    matched_harness=True,
    read_only_live_session=True,
    scope="Catalogue and independent pinned socket expectations. Native VOP authoring and graph rendering have separate evidence; ramps, textures and broader coordinate semantics need separate certification.",
)
(destination / "houdini-shader-catalogue-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
