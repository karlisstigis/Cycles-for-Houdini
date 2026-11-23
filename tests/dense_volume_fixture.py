# SPDX-License-Identifier: Apache-2.0
"""Owned native dense fields and independent image checks shared by IPR tests."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
from dataclasses import dataclass
import math
import os
from pathlib import Path

import hou
import numpy as np
import cycles_houdini as ch


@dataclass(frozen=True)
class Case:
    mode: int
    density: float | None
    codes: frozenset = frozenset()
    binding: int = 0


IGNORED = frozenset({"volume.field_ignored"})
MISSING = IGNORED | {"volume.density_unavailable"}
CONFLICT = frozenset({"volume.conflicting_materials"})
BASE = Case(0, 0.35)
CASES = (
    BASE,
    Case(1, 1),
    BASE,
    Case(2, 0.35, IGNORED),
    BASE,
    Case(4, 0.35, IGNORED),
    BASE,
    Case(8, None, MISSING),
    BASE,
    Case(16, None, frozenset({"volume.density_unavailable"})),
    BASE,
    Case(64, None, MISSING),
    BASE,
    Case(32, 0.35, IGNORED),
    BASE,
    Case(96, 0.7, IGNORED),
    BASE,
    Case(128, 0),
    BASE,
    Case(32, 0.35, IGNORED | CONFLICT, 1),
    Case(96, 1.4, IGNORED | CONFLICT, 1),
    Case(32, 0.7, IGNORED | CONFLICT, 2),
    Case(96, 0.7, IGNORED | CONFLICT, 2),
    Case(0, 0.35, CONFLICT, 1),
    Case(0, 0.7, CONFLICT, 2),
    Case(0, 0.35, CONFLICT, 3),
    BASE,
)


def fixture(device, *, samples=8192, scale=1):
    dest = artifacts_root() / "stage2/volumes/dense-recovery"
    dest.mkdir(parents=True, exist_ok=True)
    for mode in sorted({case.mode for case in CASES}):
        ch._native.write_dense_volume_fixture(str(dest / f"fields-{mode}.bgeo.sc"), mode)

    obj = hou.node("/obj").createNode("geo", "dense_recovery", run_init_scripts=False)
    source = obj.createNode("file", "typed_dense_fields")
    source.addSpareParmTuple(hou.IntParmTemplate("mode", "Test field state", 1))
    source.parm("file").setExpression(
        repr(str(dest)) + " + '/fields-' + str(hou.pwd().evalParm('mode')) + '.bgeo.sc'", hou.exprLanguage.Python
    )

    absorb = ch.create_material(name="dense_recovery_absorption", shader="absorption_volume")
    absorb.node("volume_shader").parmTuple("color").set((0, 0, 0))
    other = ch.create_material(name="dense_recovery_double_absorption", shader="absorption_volume")
    other.node("volume_shader").parmTuple("color").set((0, 0, 0))
    other.node("density_scale").parm("value2").set(2)
    obj.parm("shop_materialpath").set(absorb.path())

    assignment = obj.createNode("attribwrangle", "density_material_bindings")
    assignment.setInput(0, source)
    assignment.addSpareParmTuple(hou.IntParmTemplate("binding", "Test material state", 1))
    assignment.setParms(
        {
            "class": 1,
            "snippet": """
int mode = chi("binding");
s@shop_materialpath = "";
if (mode == 1) s@shop_materialpath = @primnum == 0 ? "%s" : "%s";
if (mode == 2) s@shop_materialpath = @primnum == 0 ? "%s" : "%s";
if (mode == 3 && @primnum != 0) s@shop_materialpath = "%s";
"""
            % (absorb.path(), other.path(), other.path(), absorb.path(), other.path()),
        }
    )

    emitter = ch.create_material(name="dense_recovery_sentinel", shader="emission")
    emitter.node("surface_shader").parmTuple("color").set((0.1, 0.7, 0.2))
    emitter.node("surface_shader").parm("strength").set(1)
    box = obj.createNode("box", "unaffected_mesh")
    box.parmTuple("size").set((0.3, 0.5, 0.2))
    box.parm("tx").set(1.5)
    binding = obj.createNode("attribwrangle", "sentinel_material")
    binding.setInput(0, box)
    binding.setParms({"class": 1, "snippet": 's@shop_materialpath="' + emitter.path() + '";'})
    merge = obj.createNode("merge", "OUT")
    merge.setInput(0, assignment)
    merge.setInput(1, binding)
    merge.setDisplayFlag(True)
    merge.setRenderFlag(True)

    camera = hou.node("/obj").createNode("cam", "dense_recovery_camera")
    camera.setParms(dict(tz=5, projection=1, orthowidth=4))
    rop = hou.node("/out").createNode("cycles_ipr", "dense_recovery")
    # The default this test was written for (before the ROP took Blender's defaults).
    rop.setParms(dict(pixel_filter="box", filter_width=1.0))
    set_rop_devices(rop, device)
    rop.setParms(
        dict(
            source_mode="objects",
            vobject=obj.path(),
            camera=camera.path(),
            resx=80 * scale,
            resy=48 * scale,
            samples=samples,
            ipr_samples=samples,
            adaptive_sampling=0,
            ipr_adaptive_sampling=0,
            use_denoise=0,
            ipr_use_denoise=0,
            allowmotionblur=0,
            use_material_assignments=1,
        )
    )
    rop.parmTuple("background").set((1, 1, 1))
    return rop, source, assignment, dest


def check(frame, status, case, *, geometry_extra=0, object_extra=0):
    plane = frame["planes"]["combined"]
    image = np.frombuffer(plane["pixels"], np.float32).reshape(frame["height"], frame["width"], plane["channels"])[
        :, :, :3
    ]
    scale = frame["width"] // 80
    expected = 1 if case.density is None else math.exp(-case.density)
    observed = float(image[21 * scale : 27 * scale, 37 * scale : 43 * scale, 0].mean())
    assert np.isfinite(image).all() and status["state"] != "failed" and not status["motion_warnings"], status
    assert {d["code"] for d in status["source_diagnostics"]} == case.codes, status
    count = 1 if case.density is None else 2
    assert (
        status["geometry_resources"] == count + geometry_extra and status["object_resources"] == count + object_extra
    ), status
    assert status["points"] == 0, status
    assert abs(observed - expected) < 0.004, (case, observed, expected)
    # Single precision accumulation introduces about 3e-5 at 8192 samples.
    np.testing.assert_allclose(image[24 * scale, 70 * scale], (0.1, 0.7, 0.2), atol=1e-4)
    return dict(expected=expected, observed=observed)
