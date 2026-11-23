# SPDX-License-Identifier: Apache-2.0
"""One explicit tessellation update without changing the automatic-update toggle."""

import hou


def refresh(rop):
    from . import native_scene
    from ..ipr import registry

    entry = registry.get(f"rop:{rop.sessionId()}")
    if entry is None or entry.session.status()["state"] == "closed":
        entry = registry.for_rop(rop, native_scene.configuration(rop))
    entry.tessellation_revision += 1
    entry.camera_refinement_pending = False
    entry.changed()
    return entry.apply(native_scene.capture(rop), hou.time())
