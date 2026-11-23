# SPDX-License-Identifier: Apache-2.0
"""Owned frames survive renderer closure and convert without host access."""

from support.paths import artifacts_root
import argparse
import json
import os
from pathlib import Path
import threading
import time
import cycles_houdini as ch
from cycles_houdini.display_queue import DisplayQueue

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
with ch.Session(device=device, width=96, height=72, samples=4, interactive=False) as session:
    session.wait(session.calibration())
    settings = dict(exposure=0.75, gamma=1.4)
    expected = session.display_image(**settings)
    expected.setdefault("render_revision", expected["generation"])
    owned = session.display_frame()
assert session.status()["device_allocation_bytes"] == 0
main_thread = threading.get_ident()
conversion_threads = []


def convert(frame, options):
    conversion_threads.append(threading.get_ident())
    return ch._native.display_image(frame, options)


queue = DisplayQueue(convert)
try:
    queue.request(("closed-renderer",), *owned, settings)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        ready = queue.take()
        if ready is not None:
            break
        time.sleep(0.005)
    else:
        raise TimeoutError("Background conversion stalled")
    _, image, error = ready
    assert error is None and image == expected
    assert conversion_threads and main_thread not in conversion_threads
finally:
    queue.close(wait=True)
assert not queue.thread.is_alive()
report = dict(
    result="passed",
    device=device,
    checks=[
        "Exact RGBA, metadata, exposure and gamma match after renderer closure",
        "Immutable frame conversion executes on a separate thread",
        "Display thread joins cleanly",
    ],
)
destination = artifacts_root() / "stage1/display-queue"
destination.mkdir(parents=True, exist_ok=True)
(destination / "houdini-display-queue-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
