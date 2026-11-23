# SPDX-License-Identifier: Apache-2.0
"""Independent curve positions/fields and frozen background-read ownership."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import gc
import json
import traceback

import hou
import numpy as np
import cycles_houdini as ch
from parametric_curve_fixture import Fixture, expected, raw_arrays

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
f = Fixture()
report = dict(result="running", device=device, checks=[])
try:
    for name, node in f.attributes.items():
        primitive = node.geometry().prims()[0]
        for divisions in (1, 4, 16, 64):
            lease = ch._native.preserve_geometry(node.path(), hou.time(), True)
            raw = ch._native.convert_preserved_curves(lease, divisions, True, ["signal", "custom"])
            actual = raw_arrays(raw)
            reference = expected(primitive, divisions)
            assert not raw["source_diagnostics"], raw["source_diagnostics"]
            assert actual["first_key"].tolist() == reference["first_key"]
            errors = {}
            for key, target in (("positions", "positions"), ("colors", "Cd"), ("velocities", "v")):
                np.testing.assert_allclose(actual[key], reference[target], atol=5e-6, rtol=0)
                errors[key] = float(np.abs(actual[key] - reference[target]).max())
            np.testing.assert_allclose(actual["radii"], reference["width"] / 2, atol=2e-6, rtol=0)
            np.testing.assert_allclose(actual["uv"][0], reference["uv"][0, :2], atol=2e-6, rtol=0)
            for key in ("signal", "custom"):
                width = 1 if key == "signal" else 3
                np.testing.assert_allclose(
                    actual["attributes"][key][:, :width].squeeze(), reference[key].squeeze(), atol=5e-6, rtol=0
                )
            # A second public host evaluator checks normalized domain handling.
            hom = np.array([primitive.positionAtInterior(float(u), 0) for u in reference["parameters"]])
            np.testing.assert_allclose(actual["positions"], hom, atol=5e-6, rtol=0)
            if primitive.isClosed():
                np.testing.assert_allclose(actual["positions"][0], actual["positions"][-1], atol=5e-6, rtol=0)
            if name.startswith("rational"):
                np.testing.assert_allclose(np.linalg.norm(actual["positions"][:, :2], axis=1), 0.8, atol=5e-6, rtol=0)
            report["checks"].append(dict(name=name, divisions=divisions, keys=len(actual["positions"]), errors=errors))
            del lease
    for name, node in f.invalid_sources.items():
        lease = ch._native.preserve_geometry(node.path(), hou.time(), True)
        raw = ch._native.convert_preserved_curves(lease, 16)
        assert not raw["positions"] and not raw["first_key"]
        assert {d["code"] for d in raw["source_diagnostics"]} == {"geometry.curve_unavailable"}
        report.setdefault("invalid_sources", []).append(dict(name=name, diagnostics=raw["source_diagnostics"]))
        del lease
    node = f.attributes["multi_bezier"]
    lease = ch._native.preserve_geometry(node.path(), hou.time(), True)
    baseline = ch._native.convert_preserved_curves(lease, 64, True, ["signal", "custom"])

    def convert():
        for _ in range(30):
            actual = ch._native.convert_preserved_curves(lease, 64, True, ["signal", "custom"])
            assert actual == baseline
        return 30

    with ThreadPoolExecutor(max_workers=2) as pool:
        readers = [pool.submit(convert) for _ in range(2)]
        for index in range(12):
            node.parm("snippet").set(node.evalParm("snippet") + "\n@P.z+=" + str(0.01 * index) + ";")
            node.cook(force=True)
        f.obj.destroy()
        report["background_conversions"] = sum(reader.result() for reader in readers)
    del lease
    gc.collect()
    assert ch._native.geometry_lease_count() == 0
    report.update(result="passed", closed_leases=ch._native.geometry_lease_count())
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    (f.dest / "contracts.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
