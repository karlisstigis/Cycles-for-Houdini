# SPDX-License-Identifier: Apache-2.0
"""Bit-sensitive native height inputs: retained images must match fresh renders."""

import argparse, json, os, traceback
from pathlib import Path
import numpy as np
import cycles_houdini as ch
from hash_displacement_fixture import Fixture, CASES, BITS, plane

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
device = json.loads(device) if device.startswith("[") else device
f = Fixture(device)
path = f.dest / "report.json"
report = dict(result="running", device=device, scope=__doc__, checks=[], failures=[])
oracle = {}
try:
    for case in CASES:
        f.configure(case)
        try:
            with ch.Session(
                device=device,
                width=f.width,
                height=f.height,
                samples=4,
                threads=2,
                execution="worker",
                use_texture_cache=False,
                collect_diagnostics=True,
            ) as retained:
                for label, bits in (
                    ("positive_zero", 0),
                    ("negative_zero", 0x80000000),
                    ("repair", 0),
                    ("positive_subnormal", 1),
                    ("negative_subnormal", 0x80000001),
                    ("repair_again", 0),
                ):
                    f.edit(bits)
                    actual, status = f.render(retained)
                    with ch.Session(
                        device=device,
                        width=f.width,
                        height=f.height,
                        samples=4,
                        threads=2,
                        execution="worker",
                        use_texture_cache=False,
                    ) as fresh:
                        reference, fresh_status = f.render(fresh)
                    assert fresh.status()["owned_geometry_bytes"] == fresh.status()["device_allocation_bytes"] == 0
                    assert not status["source_diagnostics"] and not fresh_status["source_diagnostics"], (
                        status,
                        fresh_status,
                    )
                    measured = f.check(actual)
                    expected = f.check(reference)
                    mask = f.mask(reference) & f.mask(actual)
                    a, b = plane(actual, "position")[mask], plane(reference, "position")[mask]
                    error = float(np.max(np.abs(a - b)))
                    report["checks"].append(
                        dict(
                            case=case,
                            label=label,
                            bits=bits,
                            retained_height=measured,
                            fresh_height=expected,
                            max_position_error=error,
                            status=status,
                        )
                    )
                    path.write_text(json.dumps(report, indent=2))
                    np.testing.assert_allclose(a, b, rtol=0, atol=2e-5, err_msg=case + ":" + label)
                    for name in ("combined", "normal", "depth"):
                        np.testing.assert_allclose(
                            plane(actual, name),
                            plane(reference, name),
                            rtol=0,
                            atol=2e-5,
                            err_msg=case + ":" + label + ":" + name,
                        )
                    if case == "scalar":
                        oracle[bits] = expected
                    else:
                        assert abs(expected - oracle[bits]) < 2e-5, (
                            case,
                            label,
                            "authored field/parameter changed before hashing",
                            expected,
                            oracle[bits],
                        )
                before = status["displacement_rebuilds"]
                f.source.parm("unused_bits").set(-2147483648)
                actual, status = f.render(retained)
                assert status["displacement_rebuilds"] == before and not status["source_diagnostics"], status
                report["checks"].append(dict(case=case, label="unused_field_retains_height", status=status))
            closed = retained.status()
            assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0, closed
        except BaseException:
            report["failures"].append(dict(case=case, error=traceback.format_exc()))
    assert abs(oracle[0] - oracle[0x80000000]) > 0.001, oracle
    assert not report["failures"], report["failures"]
    report.update(result="passed", oracle=oracle)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
finally:
    path.write_text(json.dumps(report, indent=2))
assert report["result"] == "passed", report.get("error")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
