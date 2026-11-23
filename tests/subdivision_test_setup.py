# SPDX-License-Identifier: Apache-2.0
"""Author explicit object subdivision in shared rendering fixtures."""

import hou
import pass_fixtures
from cycles_houdini import native_scene, object_properties, object_scene


def set_subdivision(rop, **settings):
    now = hou.time()
    if object_scene.enabled(rop, now):
        objects = object_scene.selected(rop, now)
    else:
        sop = rop.node(rop.evalParm("soppath"))
        objects = [native_scene.object_for_sop(sop)] if sop else []
    # Fixture rates are absolute object targets; scene multipliers are unity.
    rop.setParms(dict(dicing_rate=1, ipr_dicing_rate=1))
    if "max_level" in settings:
        rop.parm("max_subdivisions").set(settings["max_level"])
    for obj in objects:
        object_properties.install(obj)
        values = dict(cycles_subdivision_adaptive=1)
        # The default this test was written for (before the object tab took Blender's defaults):
        # Keep Corners, which the engine references for these fixtures also use.
        values["cycles_subdivision_boundary"] = "edge_and_corner"
        if "type" in settings:
            values["cycles_subdivision_override"] = settings["type"] != "none"
            if settings["type"] != "none":
                values["cycles_subdivision_type"] = settings["type"]
        for key in ("space", "boundary", "fvar"):
            if key in settings:
                values["cycles_subdivision_" + key] = settings[key]
        obj.setParms(values)
        if "dicing_rate" in settings:
            name = (
                "cycles_subdivision_edge_length"
                if obj.evalParm("cycles_subdivision_space") == "object"
                else "cycles_subdivision_pixel_size"
            )
            obj.parm(name).set(settings["dicing_rate"])


def configure_render(rop, parameters):
    """Set ROP controls and an optional explicit object-subdivision fixture."""
    parameters = dict(parameters)
    subdivision = parameters.pop("subdivision", None)
    if "subdivision_limit" in parameters:
        parameters.setdefault("limit_subdivision", True)
    pass_fixtures.set_parameters(rop, parameters)
    if subdivision is not None:
        set_subdivision(rop, **subdivision)
