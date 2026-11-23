# SPDX-License-Identifier: Apache-2.0
"""Native textured/IES light illumination, local recovery, materials, ownership and ordered frames."""

import argparse, json, traceback, copy
import hou, numpy as np, OpenImageIO as oiio, PyOpenColorIO as OCIO
import cycles_houdini as ch
from cycles_houdini import registry, native_scene, menu_actions, final_render, light_properties, object_scene
from light_shader_fixture import Fixture, ies_text, write_image

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
f = Fixture(p.parse_args().device)
entry = f.session()
report = dict(result="running", device=f.device, checks=[])


def render(label, multiplier=1, code=None, *, capture=True, light_x=0):
    frame = f.update(capture)
    status = entry.session.status()
    measured = f.check(frame, multiplier, light_x=light_x)
    assert status["state"] == "complete" and not status["error"] and status["render_device_ids"] == [f.device], status
    codes = {issue["code"] for issue in status["source_diagnostics"]}
    assert codes == (set(code) if isinstance(code, (list, tuple, set)) else {code} if code else set()), (
        label,
        status["source_diagnostics"],
    )
    assert all(issue["source"] == f.light.path() for issue in status["source_diagnostics"]), status[
        "source_diagnostics"
    ]
    report["checks"].append(dict(label=label, rgb=measured, status=status))
    return frame, status


try:
    _, initial = render("Unmodulated point light matches independent radiometric Lambertian reference")
    assert initial["geometry_resources"] == 1 and initial["points"] == initial["curves"] == 0, initial
    f.rop.parm("forceobject").set(f.parent.path())
    assert f.parent not in object_scene.selected(f.rop, hou.time())
    f.rop.parm("forceobject").set("")
    f.light.parm("cycles_light_ies").set(str(f.profile))
    _, uniform = render("Unit IES photometry matches the independent point reference")
    conversions = uniform["geometry_conversions"]
    pid = uniform["renderer_pid"]
    f.profile.write_text(ies_text(2), encoding="ascii")
    render("Same-path IES change refreshes retained shader without host scene changes", 2, capture=False)
    f.light.setParms(dict(cycles_light_texture=str(f.texture), cycles_light_texture_colorspace="data"))
    render("IES intensity and color texture multiply independent light color and power", (0.5, 1, 1.5))
    f.profile.unlink()
    render(
        "Missing IES omits only photometry, retaining valid color texture", (0.25, 0.5, 0.75), "light.ies_unavailable"
    )
    f.profile.write_text("not an IES file", encoding="ascii")
    render("Invalid profile remains a usable textured light", (0.25, 0.5, 0.75), "light.ies_unavailable", capture=False)
    f.profile.write_text(ies_text(3), encoding="ascii")
    render("File-only IES repair restores photometry and clears warnings", (0.75, 1.5, 2.25), capture=False)
    f.texture.unlink()
    render(
        "Missing color texture retains valid IES and native light without magenta illumination",
        3,
        "light.texture_unavailable",
    )
    write_image(f.texture, (0.5, 0.25, 0.125))
    render("File-only color texture repair restores modulation", (1.5, 0.75, 0.375), capture=False)
    f.light.parm("cycles_light_texture_colorspace").set("Nonexistent color space")
    render("Invalid OCIO input interpretation omits only the color modulation", 3, "light.texture_unavailable")
    f.light.parm("cycles_light_texture_colorspace").set("data")
    render("Color interpretation repair", (1.5, 0.75, 0.375))
    f.profile.unlink()
    f.texture.unlink()
    render(
        "Both failed resources omit only their modulation", 1, ["light.ies_unavailable", "light.texture_unavailable"]
    )
    f.profile.write_text(ies_text(3), encoding="ascii")
    render("Independent IES repair while color texture is still missing", 3, "light.texture_unavailable", capture=False)
    write_image(f.texture, (0.5, 0.25, 0.125))
    render("Repair remaining color source", (1.5, 0.75, 0.375), capture=False)
    for label, content in [
        ("negative angle count", ies_text().replace("1 -1 1 5 1 1", "1 -1 1 -5 1 1")),
        ("excessive angle count", ies_text().replace("1 -1 1 5 1 1", "1 -1 1 2147483647 1 1")),
        ("nonfinite candela", ies_text().replace("14.151", "nan 14.151", 1)),
        ("unsupported tilt", ies_text().replace("TILT=NONE", "TILT=INCLUDE")),
        ("duplicate angles", ies_text().replace("0 45 90 135 180", "0 45 45 135 180")),
        (
            "fractional integer token before a large multiplier",
            ies_text().replace("1 -1 1 5 1 1", "1.0 -1 1000000000 5 1 1"),
        ),
        ("exponent angle-count token", ies_text().replace("1 -1 1 5 1 1", "1 -1 1 5e0 1 1")),
    ]:
        f.profile.write_text(content, encoding="ascii")
        render("Recover " + label, (0.5, 0.25, 0.125), "light.ies_unavailable")
    f.light.parm("cycles_light_texture").set("")
    f.profile.write_text(ies_text(values=[1, 0, 0, 0, 0]), encoding="ascii")
    render("Directional IES axis points along local -Z")
    f.parent.parm("ry").set(90)
    frame = f.update()
    dark = f.pixels(frame)[31:33, 31:33, :3].mean((0, 1))
    assert np.max(dark) < 0.004, dark
    report["checks"].append(
        dict(
            label="Parent rotation rotates IES photometry, not only the glyph",
            rgb=dark.tolist(),
            status=entry.session.status(),
        )
    )
    f.parent.parm("ry").set(0)
    f.profile.write_text(ies_text(1), encoding="ascii")
    render("Parent/profile repair retains the same geometry")
    # Exercise core domain recovery directly; native authoring already catches
    # this source problem before it reaches the worker.
    raw = copy.deepcopy(native_scene.capture(f.rop)["lights"][0])
    raw["graph"] = dict(
        schema_version=1,
        nodes=[dict(id="unsupported", definition="cycles/diffuse_bsdf/1", parameters={})],
        links=[],
        surface=dict(node="unsupported", socket="BSDF"),
    )
    frame = entry.session.wait(entry.apply(dict(lights=[raw]), hou.time()))
    status = entry.session.status()
    f.check(frame)
    assert {d["code"] for d in status["source_diagnostics"]} == {"light.shader_unavailable"}, status
    assert status["source_diagnostics"][0]["source"] == f.light.path()
    report["checks"].append(
        dict(label="Core light-domain fallback retains unit modulation and native power", status=status)
    )
    render("Native source repair restores emission after core domain fallback")
    f.light.parm("light_enable").set(0)
    native = f.scene.createNode("hlight::2.0", "native_cone")
    light_properties.install(native)
    native.setParms(
        dict(
            tz=3,
            light_intensity=100,
            light_exposure=0,
            coneenable=1,
            coneangle=120,
            conedelta=10,
            coneroll=0.5,
            cycles_light_ies=str(f.profile),
            cycles_light_texture=str(f.texture),
            cycles_light_texture_colorspace="data",
        )
    )
    frame = f.update()
    status = entry.session.status()
    f.check(frame, (0.5, 0.25, 0.125))
    assert not status["source_diagnostics"], status
    report["checks"].append(
        dict(label="Native cone multiplies valid IES/color without changing radiometric power", status=status)
    )
    native.destroy()
    f.light.parm("light_enable").set(1)
    render("Native cone removal restores the original point light")
    material = menu_actions.create_light_material()
    source = material.node("emission")
    source.parmTuple("color").set((0.2, 0.4, 0.8))
    source.parm("strength").set(2)
    f.light.setParms(
        dict(
            cycles_light_material=material.path(),
            cycles_light_texture="dormant-missing.exr",
            cycles_light_ies="dormant-missing.ies",
        )
    )
    render("Emission Material overrides dormant shortcut paths", (0.4, 0.8, 1.6))
    source.parm("strength").set(3)
    render("Emission Material edit updates retained light", (0.6, 1.2, 2.4))
    image = material.createNode("cycles::image_texture::1", "image")
    image.setParms(dict(filename=str(f.texture), colorspace="sRGB Encoded Rec.709 (sRGB)"))
    coordinates = material.createNode("cycles::texture_coordinate::1", "coordinates")
    image.setNamedInput("vector", coordinates, "normal")
    source.setNamedInput("color", image, "color")
    source.parm("strength").set(1)
    expected = (
        OCIO.GetCurrentConfig()
        .getProcessor("sRGB Encoded Rec.709 (sRGB)", OCIO.ROLE_SCENE_LINEAR)
        .getDefaultCPUProcessor()
        .applyRGB([0.5, 0.25, 0.125])
    )
    render("Authored emission texture uses Houdini active OCIO interpretation", expected)
    source.setInput(source.inputNames().index("color"), None)
    name = material.name()
    material.destroy()
    render("Deleted assigned emission material retains native light color and power", 1, "light.shader_unavailable")
    material = ch.create_light_material(name=name)
    material.node("emission").parmTuple("color").set((0.2, 0.3, 0.4))
    material.node("emission").parm("strength").set(1)
    render("Recreated emission material repairs its assignment", (0.2, 0.3, 0.4))
    unsupported = ch.create_material(name="unsupported_light_bsdf", shader="principled_bsdf")
    f.light.parm("cycles_light_material").set(unsupported.path())
    render("Unsupported BSDF assignment retains usable native analytic light", 1, "light.shader_unavailable")
    f.light.setParms(dict(cycles_light_material="", cycles_light_ies=str(f.profile), cycles_light_texture=""))
    _, repaired = render("Shader assignment repair clears warnings")
    assert repaired["renderer_pid"] == pid and repaired["geometry_conversions"] == conversions
    definition = native_scene.capture(f.rop)["lights"][0]
    bad = copy.deepcopy(definition)
    bad["graph"]["schema_version"] = 999
    committed = entry.session.status()["committed_generation"]
    frame_generation = entry.session.frame_info()["generation"]
    try:
        entry.session.wait(entry.session.apply(dict(lights=[bad])), timeout=10)
    except (ValueError, RuntimeError):
        pass
    else:
        raise AssertionError("Malformed light graph schema was accepted")
    assert (
        entry.session.status()["committed_generation"] == committed
        and entry.session.frame_info()["generation"] == frame_generation
    )
    render("Strict graph rejection preserves prior frame and permits a repaired update")
    # Five native final frames retain one renderer and write a recoverable fault.
    for index in range(1, 6):
        (f.dest / f"frame.{index:04d}.ies").write_text("broken" if index == 3 else ies_text(index), encoding="ascii")
    f.light.parm("cycles_light_ies").set(str(f.dest / "frame.$F4.ies"))
    f.rop.parm("picture").set(str(f.dest / "frame.$F4.exr"))
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    batch = []
    for index in range(1, 6):
        image = oiio.ImageBuf(str(f.dest / f"frame.{index:04d}.exr"))
        pixels = image.get_pixels()
        rgb = [list(image.spec().channelnames).index(c) for c in ("R", "G", "B")]
        np.testing.assert_allclose(
            pixels[31:33, 31:33][:, :, rgb].mean((0, 1)), f.expected(1 if index == 3 else index), atol=0.008, rtol=0
        )
        issues = json.loads(image.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert bool(issues) == (index == 3), issues
        batch.append(dict(frame=index, diagnostics=issues))
    report.update(result="passed", batch=batch, final=summary)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (f.dest / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]), report=str(f.dest / "report.json"))))
