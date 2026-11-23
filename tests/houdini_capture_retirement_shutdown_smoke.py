# SPDX-License-Identifier: Apache-2.0
"""Worker capture shutdown remains valid after Python executor workers stop."""

from support.paths import artifacts_root
import argparse
import json
import os
from pathlib import Path
import traceback

from cycles_houdini.queued import WorkerSession


parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
path = artifacts_root() / "stage1/capture-retirement-shutdown/report.json"
path.parent.mkdir(parents=True, exist_ok=True)
report = dict(result="running", scope=__doc__, device=device, checks=[])
session = None
try:
    for initialized in (False, True):
        session = WorkerSession(
            device=device,
            width=80,
            height=60,
            samples=2,
            threads=2,
            execution="worker",
            use_texture_cache=False,
            denoising=dict(enabled=False),
        )
        if initialized:
            session.wait(session.calibration(), timeout=60)
            assert session.status()["render_device_ids"] == [device]
        # This is the real executor shutdown state, not a mocked exception.
        session._conversion.shutdown(wait=True)
        session.shutdown()
        status = session.status()
        retirement = session.diagnostics()["capture"]["retirement"]
        assert (
            status["state"] == "closed" and status["owned_geometry_bytes"] == status["device_allocation_bytes"] == 0
        ), status
        assert status["scene_lifecycle"]["retired_scenes"] == status["scene_lifecycle"]["retired_snapshots"] == 0, (
            status
        )
        assert retirement["on_host_thread"], retirement
        try:
            session.apply({})
        except RuntimeError as error:
            assert "closed" in str(error), str(error)
        else:
            raise AssertionError("A shut down session accepted a scene")
        session.shutdown()
        report["checks"].append(dict(initialized=initialized, status=status, retirement=retirement))
        session = None
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    if session is not None:
        session.shutdown()
    path.write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
