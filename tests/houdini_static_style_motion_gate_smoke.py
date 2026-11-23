# SPDX-License-Identifier: Apache-2.0
"""Static packed geometry without style sources avoids material shutter probes."""

import argparse, json, time, traceback
import hou, numpy as np
import cycles_houdini as ch
from light_shader_fixture import Fixture

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
f = Fixture(p.parse_args().device)
f.add_heavy_packed()
f.rop.setParms(
    dict(
        use_material_assignments=1,
        allowmotionblur=1,
        geometry_motion="transform",
        motion_samples=3,
        samples=1,
        ipr_samples=1,
    )
)
f.light.parm("light_intensity").set(0)
mat = ch.create_material(name="unmodified_packed_material", shader="emission")
mat.node("surface_shader").parmTuple("color").set((0.2, 0.4, 0.6))
mat.node("surface_shader").parm("strength").set(1)
for obj in (f.obj, f.heavy):
    obj.parm("shop_materialpath").set(mat.path())
entry = f.session()
dest = f.dest.parent / "static-style-motion-gate"
dest.mkdir(exist_ok=True)
report = dict(result="running", device=f.device, checks=[])
try:
    for label, action in (
        ("Initial static packed scene", lambda: None),
        ("Unchanged static scene", lambda: None),
        ("Camera transform", lambda: f.camera.parm("tx").set(0.3)),
        ("Packed object transform", lambda: f.heavy.parm("tx").set(0.5)),
        ("Another unchanged capture", lambda: None),
    ):
        action()
        start = time.monotonic()
        image = f.update()
        status = entry.session.status()
        elapsed = time.monotonic() - start
        report["checks"].append(
            dict(
                label=label,
                wall_seconds=elapsed,
                status=status,
                heavy_cooks=f.heavy.node("million_triangle_prototype").cookCount(),
            )
        )
        np.testing.assert_allclose(f.pixels(image)[32, 32, :3], (0.2, 0.4, 0.6), atol=3e-5)
        assert not status["error"] and not status["source_diagnostics"] and not status["motion_warnings"], status
        assert (
            status["geometry_resources"] == 2 and status["object_resources"] == 13 and status["packed_instances"] == 12
        ), status
        assert report["checks"][-1]["heavy_cooks"] == 1
        assert status["geometry_conversions"] == report["checks"][0]["status"]["geometry_conversions"], status
    assert all(
        row["status"]["material_motion_cache_hits"] == 0
        and row["status"]["material_motion_samples"] == 0
        and row["status"]["material_motion_cooks"] == 0
        for row in report["checks"]
    ), [
        (row["label"], row["status"]["material_motion_cache_hits"], row["status"]["material_motion_samples"])
        for row in report["checks"]
    ]
    entry.session.close(wait=True)
    closed = entry.session.status()
    assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0
    report.update(result="passed", closed=closed)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    ch.registry.close_all()
    (dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
