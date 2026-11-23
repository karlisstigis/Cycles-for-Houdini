# SPDX-License-Identifier: Apache-2.0
"""Compare same-device linear pass buffers before and after the Huang RLS fix."""

import argparse
import json
from pathlib import Path

import numpy as np

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--directory", type=Path, required=True)
parser.add_argument("--baseline", required=True)
parser.add_argument("--fixed", required=True)
parser.add_argument("--report", type=Path, required=True)
args = parser.parse_args()
before = json.loads((args.directory / (args.baseline + ".json")).read_text())
after = json.loads((args.directory / (args.fixed + ".json")).read_text())
assert before["result"] == after["result"] == "passed"
checks = []
changed_hair = []
for run in before["runs"]:
    name = run["name"]
    old = np.load(args.directory / (args.baseline + "-" + name + ".npz"))
    new = np.load(args.directory / (args.fixed + "-" + name + ".npz"))
    assert set(old.files) == set(new.files)
    delta = {plane: float(np.max(np.abs(old[plane] - new[plane]))) for plane in old.files}
    changed = run["rls"] and run["material"] in ("huang", "mixed_huang")
    if changed:
        # The repaired RLS render must agree with the old native render, rather than the old bug.
        reference_name = name.rsplit("-", 1)[0] + "-0"
        reference = np.load(args.directory / (args.baseline + "-" + reference_name + ".npz"))
        for plane in reference.files:
            assert np.allclose(reference[plane], new[plane], rtol=1e-5, atol=1e-5), (name, plane, delta)
        changed_hair.append(max(delta.values()))
    else:
        for plane in old.files:
            assert np.allclose(old[plane], new[plane], rtol=1e-5, atol=1e-5), (name, plane, delta)
    checks.append(dict(name=name, expected_change=changed, max_abs=delta))
assert changed_hair and max(changed_hair) > 1e-4, "Fixture did not expose the previous Huang bug"
for kind in ("chiang", "diffuse", "glossy", "glass", "principled", "subsurface"):
    off = np.load(args.directory / (args.fixed + "-" + kind + "-s17-tree1-clamp0-0.npz"))
    on = np.load(args.directory / (args.fixed + "-" + kind + "-s17-tree1-clamp0-1.npz"))
    assert not np.array_equal(off["combined"], on["combined"]), (kind, "RLS did not change samples")
args.report.write_text(
    json.dumps(
        dict(
            result="passed",
            baseline=before["host"],
            fixed=after["host"],
            device=after["device"],
            checks=checks,
            scope="Same-device before/after transport and all captured planes; native-off unchanged; non-Huang RLS unchanged and still active; repaired pure/mixed Huang matches pre-fix native sampling.",
        ),
        indent=2,
    )
)
print(args.report)
