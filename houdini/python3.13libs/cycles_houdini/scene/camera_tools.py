# SPDX-License-Identifier: Apache-2.0
"""Viewer camera operations using rendered world positions and native cameras."""

import math
import struct
import hou


def focus_distance(frame, x, y, camera):
    if "position" in frame:
        # Native single-pixel sampling works on progressive compact planes,
        # without expanding/copying full-resolution floats on the UI thread.
        depth, point = frame["depth"], hou.Vector3(frame["position"])
    else:
        x -= frame["x"]
        y -= frame["y"]
        if not (0 <= x < frame["width"] and 0 <= y < frame["height"]):
            raise ValueError("Click inside the rendered region")
        index = y * frame["width"] + x
        planes = frame["planes"]
        if not {"position", "depth"} <= planes.keys():
            raise ValueError("Waiting for focus-picking passes")
        depth = struct.unpack_from("f", planes["depth"]["pixels"], index * 4)[0]
        point = hou.Vector3(struct.unpack_from("fff", planes["position"]["pixels"], index * 12))
    if not math.isfinite(depth) or depth <= 0 or depth >= 1e10:
        raise ValueError("No rendered surface at this pixel")
    transform = camera.worldTransform()
    origin = hou.Vector3(0, 0, 0) * transform
    if camera.parm("cycles_projection") and camera.evalParm("cycles_projection") == "panorama":
        distance = (point - origin).length()
        if not math.isfinite(distance) or distance <= 0:
            raise ValueError("No valid focus distance at this pixel")
        return distance
    forward = (hou.Vector3(0, 0, -1) * transform - origin).normalized()
    distance = (point - origin).dot(forward)
    if not math.isfinite(distance) or distance <= 0:
        raise ValueError("No valid focus plane at this pixel")
    return distance


def set_focus(camera, distance):
    from . import camera_properties

    with hou.undos.group("Cycles Click to Focus"):
        camera_properties.install(camera)
        custom = camera.parm("cycles_focus_distance")
        target = custom
        try:
            if custom.expression() == 'ch("focus")':
                target = camera.parm("focus")
        except hou.OperationFailed:
            pass
        target.set(distance)
        camera.parm("cycles_dof").set(1)
