# SPDX-License-Identifier: Apache-2.0
"""A bad sky light shader retains the supported light and its photometric power."""

import argparse, json, traceback
import hou, numpy as np
from cycles_houdini import menu_actions, native_scene, registry
from light_shader_fixture import Fixture

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
args = p.parse_args()
f = Fixture(args.device)
f.dest = f.dest.parent / "sky-light-recovery"
f.dest.mkdir(exist_ok=True)
material = menu_actions.create_light_material()
sky = material.createNode("cycles::sky_texture::1", "sky")
sky.parm("sky_type").set("hosek_wilkie")
sky.parmTuple("sun_direction").set((0, 0, 1))
direction = material.createNode("cycles::combine_xyz::1", "direction")
direction.setParms(dict(x=0.6, y=0, z=0.8))
sky.setNamedInput("vector", direction, "vector")
material.node("emission").setNamedInput("color", sky, "color")
f.light.parm("cycles_light_material").set(material.path())
entry = f.session()
report = dict(result="running", device=args.device, checks=[])


def render(label, warning=False, **photometry):
    frame = f.update()
    status = entry.session.status()
    assert status["state"] == "complete" and not status["error"] and not status.get("update_rejected"), status
    assert status["object_resources"] == status["geometry_resources"] == status["light_resources"] == 1, status
    assert bool(status["source_diagnostics"]) == warning, status
    if warning:
        assert all(
            issue["code"] == "light.shader_unavailable" and "Sky Texture" in issue["message"]
            for issue in status["source_diagnostics"]
        ), status
        f.check(frame, **photometry)
    report["checks"].append(dict(label=label, status=status))
    return f.pixels(frame).copy()


try:
    baseline = render("Healthy analytic sky emission")
    before = entry.session.status()
    pid = before["renderer_pid"]
    conversions = before["geometry_conversions"]
    sky.parmTuple("sun_direction")[2].setExpression("0", hou.exprLanguage.Hscript)
    render("Bad sky shader uses supported point power and white modulation", True)
    render("Warm warning retains photometric fallback", True)
    f.light.parm("tx").set(0.5)
    f.camera.parm("tx").set(0.05)
    render("Light and camera continue updating through the shader fault", True, light_x=0.5, camera_x=0.05)
    assert entry.session.status()["geometry_conversions"] == conversions
    assert entry.session.status()["renderer_pid"] == pid
    f.light.parm("tx").set(0)
    f.camera.parm("tx").set(0)
    sky.parmTuple("sun_direction")[2].setExpression("1", hou.exprLanguage.Hscript)
    repaired = render("Repair restores the original sky emission")
    np.testing.assert_allclose(repaired, baseline, atol=2e-5, rtol=0)
    bad = native_scene.capture(f.rop)
    next(n for n in bad["lights"][0]["graph"]["nodes"] if n["definition"] == "cycles/sky_texture/1")["parameters"][
        "sky_type"
    ] = "malformed_owned_enum"
    committed = entry.session.status()["committed_generation"]
    try:
        entry.session.wait(entry.apply(bad, hou.time()), timeout=60)
    except RuntimeError as error:
        assert "Unknown shader enum token" in str(error), str(error)
    else:
        raise AssertionError("Malformed internal sky enum was accepted")
    status = entry.session.status()
    assert status["update_rejected"] and status["committed_generation"] == committed, status
    report["checks"].append(dict(label="Malformed owned enum stays an explicit rejected transaction", status=status))
    np.testing.assert_allclose(render("Repair after strict rejection"), baseline, atol=2e-5, rtol=0)
    entry.session.close(wait=True)
    status = entry.session.status()
    assert status["state"] == "closed" and status["owned_geometry_bytes"] == status["device_allocation_bytes"] == 0, (
        status
    )
    report.update(result="passed", closed=status)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (f.dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
