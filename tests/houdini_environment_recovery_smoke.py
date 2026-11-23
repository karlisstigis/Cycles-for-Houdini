# SPDX-License-Identifier: Apache-2.0
"""Optional native environment controls retain usable lighting and local diagnostics."""

import argparse
import json
import traceback

import hou
import numpy as np
import OpenImageIO as oiio

from cycles_houdini import final_render, native_scene, registry, world
from environment_recovery_fixture import Fixture

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
f = Fixture(p.parse_args().device)
entry = f.session()
report = dict(result="running", device=f.device, checks=[])
notifications = []
observer = native_scene.SceneObserver(f.rop, lambda: notifications.append(True))


def render(name, expected=None, settings=(), **kwargs):
    frame = f.render(kwargs.pop("at", None))
    status = entry.session.status()
    planes = f.check(frame, status, expected, settings, **kwargs)
    report["checks"].append(
        dict(
            name=name,
            diagnostics=status["source_diagnostics"],
            generation=frame["generation"],
            radiance=planes["combined"][2, 2, :3].tolist(),
            geometry_conversions=status["geometry_conversions"],
            background_builds=entry.session.diagnostics()["background_builds"],
        )
    )
    (f.dest / "report.json").write_text(json.dumps(report, indent=2))
    return planes, status


try:
    base, status = render("Baseline native image illuminates a Lambertian surface")
    conversions = status["geometry_conversions"]
    builds = entry.session.diagnostics()["background_builds"]
    pid = status["renderer_pid"]
    # This is intentionally first: the old whole-world fallback fails numerically.
    f.env.parm("env_portalenable").set(1)
    render("Portal request retains environment radiance and illumination", settings=("env_portalenable",))
    f.env.parm("env_portalenable").set(0)
    render("Removing portal request clears its diagnostic")
    for parm, value in (
        ("env_filteramount", 0.5),
        ("shadowmask", "unrelated_object"),
        ("shadow_intensity", 0.25),
        ("shadow_transparent", 0),
    ):
        f.env.parm(parm).set(value)
        render("Unsupported active " + parm + " retains the environment", settings=(parm,))
        f.env.parm(parm).revertToDefaults()
        render("Repairing " + parm + " clears its diagnostic")
    for mode in ("occlusion", "background"):
        f.env.parm("env_mode").set(mode)
        render("Native " + mode + " mode retains supported direct environment", settings=("env_mode",))
        f.env.parm("env_mode").set("direct")
        render("Restoring direct mode clears the warning for " + mode)
    f.env.parm("light_contrib").set(1)
    render("Contribution list is ignored while supported bindings remain", settings=("light_contrib",))
    f.env.parm("light_contrib").set(0)
    render("Removing contribution overrides clears the warning")
    assert entry.session.status()["geometry_conversions"] == conversions
    assert entry.session.diagnostics()["background_builds"] == builds

    # Inactive native controls must not be evaluated or reported as active effects.
    f.env.setParms(dict(env_mode="background", env_filteramount=0.5, env_portalenable=1, env_domaxdist=1))
    render("Disabled direct/occlusion controls remain quiet in background mode", settings=("env_mode",))
    f.env.setParms(dict(env_mode="occlusion", env_angle=45, env_doadaptive=1))
    render("Unsupported occlusion sampling is covered by its mode warning", settings=("env_mode",))
    f.env.setParms(dict(env_mode="direct", env_filteramount=0, env_portalenable=0))
    render("Dormant occlusion settings do not produce diagnostics in direct mode")
    f.env.setParms(dict(env_map="", env_filteramount=0.5))
    render("Glossy filtering without an image is inactive and quiet", expected=(1, 1, 1))
    f.env.setParms(dict(env_map=str(f.image), env_filteramount=0))
    f.env.setParms(dict(shadow_type="off", shadowmask="unrelated", shadow_intensity=0.2, shadow_transparent=0))
    render("Shadow overrides are inactive when shadows are off")
    f.env.setParms(dict(shadow_type="raytrace", cycles_light_cast_shadow=0))
    render("Cycles shadow opt-out keeps unused native shadow settings quiet")
    f.env.setParms(dict(cycles_light_cast_shadow=1, shadowmask="*", shadow_intensity=1, shadow_transparent=1))
    render("Restoring supported shadow settings clears all warnings")

    f.env.setParms(
        dict(
            env_portalenable=1,
            env_filteramount=0.5,
            shadowmask="unrelated",
            shadow_intensity=0.2,
            shadow_transparent=0,
            light_contrib=1,
        )
    )
    settings = (
        "env_portalenable",
        "env_filteramount",
        "shadowmask",
        "shadow_intensity",
        "shadow_transparent",
        "light_contrib",
    )
    render("Concurrent optional problems are distinct and do not discard lighting", settings=settings)
    render("Diagnostics persist once each across unchanged capture hits", settings=settings)
    f.rop.parm("allowmotionblur").set(1)
    render("Shutter samples do not duplicate nominal optional diagnostics", settings=settings)
    f.env.parm("env_filteramount").setExpression(".5+$T", hou.exprLanguage.Hscript)
    render("Animation in ignored controls does not invent world motion", settings=settings)
    f.env.parm("env_filteramount").deleteAllKeyframes()
    f.env.setParms(
        dict(
            env_portalenable=0,
            env_filteramount=0,
            shadowmask="*",
            shadow_intensity=1,
            shadow_transparent=1,
            light_contrib=0,
        )
    )
    render("Repairing all active controls clears warnings under motion blur")

    # Overflow and invalid source values are known source problems, not internal faults.
    for parm, value in (("light_exposure", 2048), ("light_intensity", -1), ("light_intensity", 1e38)):
        if value == 1e38:
            f.env.parm("light_exposure").set(8)
        f.env.parm(parm).set(value)
        render("Unusable environment power uses configured background: " + parm, f.fallback, unavailable=True)
        f.env.setParms(dict(light_exposure=0, light_intensity=1))
        render("Repairing environment power restores illumination")

    # The generated sky is a different image source, not an optional modifier.
    f.env.parm("skymap_enable").set(1)
    render("Unadapted native sky source uses configured fallback, not a dormant file", f.fallback, unavailable=True)
    f.env.parm("skymap_enable").set(0)
    render("Returning to supported image source repairs the world")
    f.rop.parm("allowmotionblur").set(0)
    f.env.parm("env_portalenable").set(1)
    render("Portal warning is accepted before a camera-only capture", settings=("env_portalenable",))
    before = entry.session.status()["geometry_conversions"]
    f.camera.parm("tx").set(0.125)
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop, camera_only=True), hou.time()))
    f.check(frame, entry.session.status(), settings=("env_portalenable",))
    assert entry.session.status()["geometry_conversions"] == before
    render("Camera-only updates retain warnings and usable geometry", settings=("env_portalenable",))
    f.camera.parm("tx").set(0)
    f.env.parm("light_intensity").set(2)
    render("Supported intensity still updates with a portal warning", f.radiance * 2, settings=("env_portalenable",))
    f.env.parm("light_intensity").set(1)
    f.env.parm("env_portalenable").set(0)
    render("Final repair restores independent illumination")
    assert entry.session.status()["geometry_conversions"] == conversions
    assert entry.session.status()["renderer_pid"] == pid

    f.env.parm("env_portalenable").set(1)
    f.geo.parm("tx").set(0.25)
    moved, _ = render("Object transforms still update during an environment warning", settings=("env_portalenable",))
    assert np.mean(np.abs(moved["position"] - base["position"])) > 0.001
    f.geo.parm("tx").set(0)
    lamp = f.scene.createNode("hlight", "supported_light")
    lamp.setParms(dict(light_type="point", light_intensity=100, tx=-1.5, ty=0.5, tz=2))
    lit, _ = render(
        "Independent analytic light survives an environment warning", settings=("env_portalenable",), surface=False
    )
    lamp.parm("tx").set(1.5)
    relit, _ = render(
        "Moving another light updates usable geometry and lighting", settings=("env_portalenable",), surface=False
    )
    assert np.mean(np.abs(lit["combined"] - relit["combined"])) > 0.0001
    f.env.parm("env_portalenable").set(0)
    missing = f.dest / "missing-environment.exr"
    assert not missing.exists()
    f.env.parm("env_map").set(str(missing))
    retained, _ = render(
        "Missing world image preserves other lights and geometry", f.fallback, unavailable=True, surface=False
    )
    assert retained["combined"][45:55, 75:85, :3].max() > 0.1
    f.env.parm("env_map").set(str(f.image))
    lamp.destroy()
    render("Source repair and light removal restore the nominal scene")
    assert entry.session.status()["geometry_conversions"] == conversions

    # Explicit evaluation time, and no warnings after source deletion.
    f.env.parm("env_portalenable").setExpression("$F==2", hou.exprLanguage.Hscript)
    render(
        "Requested-time optional settings differ from the visible timeline",
        settings=("env_portalenable",),
        at=hou.frameToTime(2),
    )
    render("Nominal healthy frame has no stale optional warning")
    assert hou.frame() == 1
    f.env.parm("env_portalenable").deleteAllKeyframes()
    f.env.parm("env_portalenable").set(0)
    f.env.parm("shadow_intensity").setExpression("if($F==1 || $F==3,.25,1)", hou.exprLanguage.Hscript)
    assert [f.env.parm("shadow_intensity").evalAtFrame(n) for n in range(1, 6)] == [0.25, 1, 0.25, 1, 1]
    f.env.parm("light_intensity").setExpression("1+.25*($F-1)", hou.exprLanguage.Hscript)
    # The default this test was written for (before the ROP took Blender's defaults).
    f.rop.parm("persistent_data").set(1)
    f.rop.parm("picture").set(str(f.dest / "recovery.$F4.exr"))
    f.rop.render(frame_range=(1, 5, 1))
    batch = []
    for number in range(1, 6):
        reader = oiio.ImageInput.open(str(f.dest / ("recovery.%04d.exr" % number)))
        assert reader
        spec = reader.spec()
        pixels = np.asarray(reader.read_image(), np.float32)
        reader.close()
        rgb = [list(spec.channelnames).index(c) for c in ("R", "G", "B")]
        expected = f.radiance * (1 + 0.25 * (number - 1))
        assert np.isfinite(pixels).all() and np.allclose(pixels[2, 2, rgb], expected, atol=0.0002, rtol=0)
        assert np.allclose(pixels[45:55, 75:85][:, :, rgb].mean((0, 1)), expected, atol=0.025, rtol=0)
        issues = json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert bool(issues) == (number in (1, 3)), issues
        assert all(i["code"] == "world.unsupported_setting" and "shadow_intensity" in i["message"] for i in issues)
        batch.append(dict(frame=number, radiance=pixels[2, 2, rgb].tolist(), diagnostics=issues))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"]
    report.update(result="passed", batch=batch, batch_summary=summary)
    f.env.parm("shadow_intensity").deleteAllKeyframes()
    f.env.parm("shadow_intensity").set(1)
    f.env.parm("light_intensity").deleteAllKeyframes()
    f.env.parm("light_intensity").set(1)
    hou.hipFile.save(str(f.dest / "environment-recovery.hiplc"))
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    observer.close()
    registry.close_all()
    (f.dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result="passed", checks=len(report["checks"]), batch_frames=5)))
