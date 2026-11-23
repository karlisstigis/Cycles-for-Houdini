# SPDX-License-Identifier: Apache-2.0
"""Dense packed material preparation phases, kept separate from device lighting."""

import argparse
import json
import time
import traceback
import cycles_houdini as ch
from stylesheet_storage_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
f = Fixture(device)
report = dict(result="running", device=device, checks=[])
try:
    with ch.Session(
        device=device,
        width=f.width,
        height=f.height,
        samples=1,
        threads=2,
        execution="worker",
        use_texture_cache=False,
        collect_diagnostics=True,
    ) as session:
        for label, mode, offset in (("uniform", "uniform", 0), ("split", "colors", 0), ("edit", "colors", 0.03)):
            f.styles(mode, offset)
            began = time.perf_counter()
            frame = session.wait(session.apply(f.update(), reuse_if_unchanged=True), timeout=240)
            elapsed = time.perf_counter() - began
            status = session.status()
            assert not status["error"] and not status["source_diagnostics"], status
            f.check(frame, offset)
            assert f.prototype.cookCount() == 1
            report["checks"].append(
                dict(label=label, status=status, complete_seconds=elapsed, diagnostics=session.diagnostics())
            )
            (f.dest / "preparation-report.json").write_text(json.dumps(report, indent=2))
    assert session.status()["device_allocation_bytes"] == session.status()["owned_geometry_bytes"] == 0
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
finally:
    (f.dest / "preparation-report.json").write_text(json.dumps(report, indent=2))
assert report["result"] == "passed", report.get("error")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
