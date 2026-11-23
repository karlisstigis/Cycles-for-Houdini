# SPDX-License-Identifier: Apache-2.0
"""Time conversion of an owned image, separately from rendering and UI waits."""

from support.paths import artifacts_root
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import time

import cycles_houdini as ch

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
with ch.Session(device=device, width=960, height=540, samples=1, interactive=False) as session:
    session.wait(session.calibration(), timeout=120)
    owned = session.display_frame()[0]
assert session.status()["device_allocation_bytes"] == 0

cases = []
for name, settings in [
    ("view", {}),
    ("raw", dict(raw=True)),
    ("alpha", dict(channels="alpha")),
    ("gamma", dict(gamma=1.4)),
    ("exposure", dict(exposure=1.0)),
    ("data", {"pass": "normal"}),
]:
    expected = ch._native.display_image(owned, settings)
    timings = []
    for index in range(31):
        began = time.perf_counter()
        actual = ch._native.display_image(owned, dict(settings, collect_diagnostics=True))
        elapsed = time.perf_counter() - began
        profile = actual.pop("diagnostics")
        assert actual == expected, name
        assert all(v >= 0 for v in profile.values()), profile
        if index:
            timings.append(dict(wall_seconds=elapsed, **profile))
    cases.append(
        dict(
            name=name,
            settings=settings,
            rgba_sha256=hashlib.sha256(expected["rgba"]).hexdigest(),
            median_seconds={key: statistics.median(row[key] for row in timings) for key in timings[0]},
            timings=timings,
        )
    )
report = dict(
    result="passed",
    device=device,
    width=960,
    height=540,
    cases=cases,
    scope="Thirty warm conversions per case of the same owned calibration frame after renderer shutdown. No concurrent rendering or UI latency claim.",
)
path = artifacts_root() / "stage1/display-profile/houdini-display-profile-smoke.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", report=str(path))))
