# SPDX-License-Identifier: Apache-2.0
"""Real selected-device direct/worker status conversion and owned-frame contract."""

from support.paths import artifacts_root
import argparse
import hashlib
import json
from pathlib import Path

import cycles_houdini as ch

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU", help="CPU or an exact device ID from the verified runtime")
parser.add_argument("--report", type=Path, default=artifacts_root() / "tests/status-conversion/report.json")
args = parser.parse_args()
schema = json.loads((Path(__file__).parent / "fixtures/python_status_schema.json").read_text())


def check(value, expected):
    for key, kind in expected.items():
        assert key in value, ("missing", key)
        assert type(value[key]).__name__ == kind, (key, type(value[key]).__name__, kind)


try:
    ch._native.status(None)
except RuntimeError as error:
    assert str(error) == "Invalid or closed Cycles session handle", str(error)
else:
    raise AssertionError("Invalid native status handle was accepted")

selected = next(device for device in ch.devices() if device["id"] == args.device)
report = {"result": "running", "device": selected, "modes": {}, "invalid_handle_error_preserved": True}
args.report.parent.mkdir(parents=True, exist_ok=True)
for mode in ("sync", "worker"):
    session = ch.Session(
        device=args.device, width=32, height=24, samples=2, execution=mode, collect_diagnostics=True, threads=2
    )
    try:
        generation = session.calibration()
        frame = session.wait(generation, timeout=90)
        raw = frame["planes"]["combined"]["pixels"]
        assert type(raw) is bytes and len(raw) == 32 * 24 * 4 * 4
        digest = hashlib.sha256(raw).hexdigest()
        for _ in range(128):
            caps = ch._native.capabilities()
            check(caps, schema["capabilities"])
            assert caps["image_iris"] is True and caps["gaussian_splats"] is True
            status = session.status()
            check(status, schema["status"])
            assert isinstance(status["rendering"], dict)
            assert status["state"] != "failed", status
            assert status["generation"] == generation
            assert status["render_device_ids"] == [args.device], status
            assert type(status["motion_warnings"]) is list
            for issue in status["source_diagnostics"]:
                assert set(issue) == {"code", "source", "resource", "message", "action", "severity", "attribute"}
                assert all(type(item) is str for item in issue.values())
        report["modes"][mode] = {"polls": 128, "status": status, "capabilities": caps, "frame_sha256": digest}
    finally:
        session.close()
    assert hashlib.sha256(raw).hexdigest() == digest
    assert frame["planes"]["combined"]["pixels"] == raw
    report["modes"][mode]["frame_owned_after_close"] = True
report["result"] = "passed"
args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps({"result": "passed", "report": str(args.report)}))
