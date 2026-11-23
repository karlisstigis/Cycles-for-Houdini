# SPDX-License-Identifier: Apache-2.0
"""Initial serial-zero capture and conservative camera-only follow-up guards."""

from support.paths import artifacts_root
import json
import os
from pathlib import Path
from copy import deepcopy
from cycles_houdini.registry import Entry


class SubmittedSession:
    def __init__(self):
        self.submissions = []

    def apply(self, update, **kwargs):
        self.submissions.append(deepcopy(update))
        return len(self.submissions)


motion = dict(enabled=False, vectors=False, fps=24.0, shutter_frames=0.5, offset=0.0, samples=3)


def update():
    return dict(geometry=[], motion=deepcopy(motion), camera=dict(transform=[1.0, 0.0, 0.0]))


entry = Entry("camera_guard", (), SubmittedSession())
entry.changed(camera_only=True)
assert not entry.can_capture_camera_only(motion, 0.0)
entry = Entry("camera_guard", (), SubmittedSession())
entry.apply(update(), 0.0)
assert entry.applied_serial == 0 and entry.last_motion == motion
entry.changed(camera_only=True)
assert entry.can_capture_camera_only(motion, 0.0), "First camera move repeated the authoritative scene capture"
assert not entry.can_capture_camera_only(motion, 1 / 24)
for key, value in [
    ("enabled", True),
    ("vectors", True),
    ("fps", 48.0),
    ("shutter_frames", 0.25),
    ("offset", 0.5),
    ("samples", 5),
]:
    changed = dict(motion, **{key: value})
    assert not entry.can_capture_camera_only(changed, 0.0), key
entry.error = "Source edit rejected"
assert not entry.can_capture_camera_only(motion, 0.0)
entry.error = ""
entry.changed()
entry.changed(camera_only=True)
assert not entry.can_capture_camera_only(motion, 0.0), "Camera edit erased a pending geometry change"
entry.apply(update(), 0.0)
entry.changed(camera_only=True)
assert entry.can_capture_camera_only(motion, 0.0)
report = dict(
    result="passed",
    checks=12,
    initial_capture_serial=0,
    scope="Actual Entry.apply/changed state transitions; final pixels, native SOHO and actual viewer captures have separate tests.",
)
dest = artifacts_root() / "stage1/camera-capture"
dest.mkdir(parents=True, exist_ok=True)
(dest / "houdini-camera-capture-guard-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
