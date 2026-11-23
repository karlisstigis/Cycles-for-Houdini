# SPDX-License-Identifier: Apache-2.0
"""Independent world-medium attenuation, local recovery, resource retention and five final frames."""

import argparse, json, traceback
import hou, numpy as np
import cycles_houdini as ch
from cycles_houdini import registry, final_render
from world_volume_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
f = Fixture(parser.parse_args().device)
report = dict(result="running", device=f.device, checks=[])
entry = f.session()


def render(label, density, background=None, warning=None, emission=None):
    frame = f.update(entry)
    status = entry.session.status()
    measured = f.check(frame, density, background=background, emission=emission)
    assert status["state"] == "complete" and not status["error"] and status["render_device_ids"] == [f.device], status
    issues = status["source_diagnostics"]
    if warning:
        assert any(d["code"] == warning for d in issues), issues
        assert all(d["source"] == f.world.path() for d in issues), issues
    else:
        assert not issues, issues
    report["checks"].append(dict(label=label, measured=measured, status=status))
    return status


try:
    first = render("unbounded absorption matches exp(-density * distance)", 0.4, background=(0, 0, 0))
    conversions = first["geometry_conversions"]
    pid = first["renderer_pid"]
    f.medium.parm("density").set(0.2)
    edited = render("density edit changes attenuation without geometry conversion", 0.2)
    assert edited["renderer_pid"] == pid and edited["geometry_conversions"] == conversions
    # The ROP's world volume sampling reaches the engine; attenuation is the
    # same for every method (equiangular is the Blender world default).
    assert f.rop.evalParm("world_volume_sampling") == "equiangular"
    for method in ("distance", "multiple_importance", "equiangular"):
        f.rop.parm("world_volume_sampling").set(method)
        sampled = render(method + " world volume sampling keeps the same attenuation", 0.2)
        assert sampled["renderer_pid"] == pid and sampled["geometry_conversions"] == conversions
    position = f.world.createNode("cycles::geometry::1", "world_position")
    separate = f.world.createNode("cycles::separate_xyz::1", "position_components")
    separate.setNamedInput("vector", position, "position")
    gradient = f.world.createNode("cycles::math::1", "linear_density")
    gradient.setParms(dict(math_type="multiply_add", value2=0.1, value3=0.05))
    gradient.setNamedInput("value1", separate, "z")
    f.medium.setNamedInput("density", gradient, "value")
    # rho(z)=.1*z+.05, integrated from z=3 to zero, has optical depth
    # .1*(3**2)/2+.05*3=.6, matching uniform density .2 over three units.
    render("world-space procedural density matches independent line integral", 0.2)
    f.medium.setInput(f.medium.inputNames().index("density"), None)
    f.medium.parm("density").set(0.2)
    f.output.setInput(0, None)
    render("volume-only world output remains valid", 0.2)
    f.output.setNamedInput("surface", f.background, "background")
    f.medium.parm("density").set(0)
    render("zero density retains the independent background and exact surface emission", 0, background=(0.1, 0.2, 0.4))
    principled = f.world.createNode("cycles::principled_volume::1", "world_principled")
    assert all(principled.evalParm(p) == "" for p in ("density_attribute", "color_attribute", "temperature_attribute"))
    principled.parmTuple("color").set((0, 0, 0))
    principled.parmTuple("emission_color").set((0.1, 0.2, 0.4))
    principled.setParms(dict(density=0.4, emission_strength=0.3))
    f.output.setNamedInput("volume", principled, "volume")
    render("principled world emission and absorption match radiative transfer", 0.4, emission=(0.03, 0.06, 0.12))
    principled.parm("emission_strength").set(0)
    principled.setParms(dict(density=0.4, color_attribute="missing_color_grid"))
    f.output.setNamedInput("volume", principled, "volume")
    render(
        "unavailable world grid omits medium and retains background",
        0,
        background=(0.1, 0.2, 0.4),
        warning="world.volume_source_unavailable",
    )
    principled.parm("color_attribute").set("")
    principled.parm("density").set(0)
    render("repair removes world-grid warning", 0, background=(0.1, 0.2, 0.4))
    # A wrong contributing closure is a source problem, while malformed graph
    # definitions/socket ownership stay strict below.
    f.output.setNamedInput("volume", f.background, "background")
    render(
        "background closure on medium socket is omitted locally",
        0,
        background=(0.1, 0.2, 0.4),
        warning="world.volume_source_unavailable",
    )
    f.output.setNamedInput("volume", f.medium, "volume")
    f.medium.parm("density").set(0.4)
    f.output.setNamedInput("surface", f.medium, "volume")
    render(
        "volume closure on background socket uses configured fallback",
        0,
        background=(0.025, 0.05, 0.1),
        warning="world.unavailable",
    )
    f.output.setNamedInput("surface", f.background, "background")
    render("repair both output branches", 0.4)
    graph = ch.material_graph(f.world)
    graph["schema_version"] = 999
    before = entry.session.status()["committed_generation"]
    frame_before = entry.session.frame_info()["generation"]
    try:
        entry.session.apply(dict(world=dict(graph=graph)))
        entry.session.wait(timeout=10)
    except (ValueError, RuntimeError):
        pass
    else:
        raise AssertionError("Malformed graph schema was accepted")
    assert entry.session.status()["committed_generation"] == before and entry.session.status()["update_rejected"]
    assert entry.session.frame_info()["generation"] == frame_before
    render("valid update recovers after strict malformed-schema rejection", 0.4)
    # Nominal-time world animation is allowed; final frames must all be written.
    f.medium.parm("density").setExpression("$F * .1", hou.exprLanguage.Hscript)
    # The default this test was written for (before the ROP took Blender's defaults).
    f.rop.parm("persistent_data").set(1)
    f.rop.parm("picture").set(str(f.dest / "world-volume.$F4.exr"))
    f.rop.render(frame_range=(1, 5, 1))
    frames = f.rop.cachedUserData(final_render._summary_key)
    assert frames["completed_frames"] == 5 and frames["created_sessions"] == 1 and not frames["session_alive"], frames
    assert all((f.dest / f"world-volume.{i:04d}.exr").is_file() for i in range(1, 6))
    import OpenImageIO as oiio

    for index in range(1, 6):
        image = oiio.ImageBuf(str(f.dest / f"world-volume.{index:04d}.exr"))
        pixels = image.get_pixels()
        rgb = [list(image.spec().channelnames).index(c) for c in ("R", "G", "B")]
        np.testing.assert_allclose(
            pixels[20:28, 44:52][:, :, rgb].mean(axis=(0, 1)), f.color * np.exp(-3 * 0.1 * index), atol=0.008, rtol=0
        )
    # A recoverable world field problem in the middle of another five-frame
    # sequence must write every image, report the omission, then repair itself.
    f.output.setNamedInput("volume", principled, "volume")
    principled.parm("density").setExpression("$F * .1", hou.exprLanguage.Hscript)
    principled.parm("color_attribute").setExpression('ifs($F == 3, "unavailable_grid", "")', hou.exprLanguage.Hscript)
    f.rop.parm("picture").set(str(f.dest / "world-volume-recovery.$F4.exr"))
    f.rop.render(frame_range=(1, 5, 1))
    recovery = []
    for index in range(1, 6):
        image = oiio.ImageBuf(str(f.dest / f"world-volume-recovery.{index:04d}.exr"))
        pixels = image.get_pixels()
        rgb = [list(image.spec().channelnames).index(c) for c in ("R", "G", "B")]
        np.testing.assert_allclose(
            pixels[20:28, 44:52][:, :, rgb].mean(axis=(0, 1)),
            f.color * np.exp(-3 * 0.1 * index) if index != 3 else f.color,
            atol=0.008,
            rtol=0,
        )
        issues = json.loads(image.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert bool(issues) == (index == 3), issues
        if issues:
            assert all(i["code"] == "world.volume_source_unavailable" for i in issues), issues
        recovery.append(dict(frame=index, diagnostics=issues))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    report.update(result="passed", final_frames=frames, recovery_sequence=recovery, recovery_summary=summary)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (f.dest / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]), report=str(f.dest / "report.json"))))
