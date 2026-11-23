# SPDX-License-Identifier: Apache-2.0
"""Native hlight controls compared with independent Lambertian light integrals."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import math
import os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene, object_scene, registry

destination = artifacts_root() / "stage3/native-scene"
destination.mkdir(parents=True, exist_ok=True)
hou.setFrame(1)
scene = hou.node("/obj").createNode("subnet", "native_lights")
obj = scene.createNode("geo", "lambertian_receiver", run_init_scripts=False)
source = obj.createNode("python", "quad")
source.parm("python").set("""import hou
g=hou.pwd().geometry();face=g.createPolygon()
for p in ((-4,-4,0),(4,-4,0),(4,4,0),(-4,4,0)):
    point=g.createPoint();point.setPosition(p);face.addVertex(point)
""")
source.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam", "native_lights_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)
light = scene.createNode("hlight", "native_light")
light.parmTuple("t").set((0, 0, 5))
light.parm("light_intensity").set(100)
rop = hou.node("/out").createNode("cycles_ipr", "native_lights_rop")
for name, value in dict(
    source_mode="objects",
    objectroot=scene.path(),
    camera=camera.path(),
    resx=96,
    resy=64,
    samples=1024,
    material_kind="diffuse",
    use_material_assignments=0,
    roughness=0,
).items():
    rop.parm(name).set(value)
rop.parmTuple("basecolor").set((1, 1, 1))
rop.parmTuple("background").set((0, 0, 0))
black = ch.create_material(name="native_black_occluder", shader="diffuse_bsdf")
black.node("surface_shader").parmTuple("color").set((0, 0, 0))
reports = []


def mean(frame):
    image = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(64, 96, 4)
    assert np.isfinite(image).all()
    return image[30:34, 46:50, :3].mean((0, 1))


def rectangle_integral(width, height, distance=5):
    # Independent midpoint integration of cos(theta_light)*cos(theta_receiver)/r^2.
    n = 512
    x = (np.arange(n) + 0.5) * width / n - width / 2
    y = (np.arange(n) + 0.5) * height / n - height / 2
    return float(np.sum(distance**2 / (distance**2 + y[:, None] ** 2 + x[None, :] ** 2) ** 2) * width * height / n**2)


for device in ("CPU", ch.default_device()):
    rop.parm("vobject").set("*")
    rop.parm("lights").set("*")
    set_rop_devices(rop, device)
    entry = registry.for_rop(rop, native_scene.configuration(rop))
    checks = []

    def render():
        return entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))

    def record(name, **kwargs):
        checks.append(dict(name=name, status=entry.session.status(), **kwargs))

    try:
        first = mean(render())
        expected = 100 / (4 * math.pi**2 * 25)
        assert np.max(np.abs(first - expected)) < 0.001, (device, first, expected)
        record(
            "Native point power agrees with independent Lambertian irradiance",
            measured=first.tolist(),
            expected=expected,
        )
        light.parm("normalizearea").set(0)
        assert np.allclose(mean(render()), first, atol=0.001, rtol=0)
        light.parm("normalizearea").set(1)
        converted = entry.session.status()["geometry_conversions"]
        light.parm("light_exposure").set(1)
        second = mean(render())
        assert np.allclose(second / first, 2, rtol=0.01)
        assert entry.session.status()["geometry_conversions"] == converted
        light.parm("light_exposure").set(0)
        light.parmTuple("light_color").set((1, 0, 0))
        red = mean(render())
        assert red[0] > 0.05 and red[1] == red[2] == 0
        light.parmTuple("light_color").set((1, 1, 1))
        record("Exposure doubles radiance and RGB colors retain geometry", before=first.tolist(), after=second.tolist())
        light.parm("light_enable").set(0)
        assert np.max(mean(render())) == 0 and entry.session.status()["light_resources"] == 0
        light.parm("light_enable").set(1)
        render()
        record("Enable retires and restores the native light")
        rop.parm("lights").set("")
        assert np.max(mean(render())) == 0
        rop.parm("forcelights").set(light.path())
        render()
        assert entry.session.status()["light_resources"] == 1
        rop.parm("excludelights").set(light.path())
        assert np.max(mean(render())) == 0
        rop.parm("lights").set("*")
        rop.parm("forcelights").set("")
        rop.parm("excludelights").set("")
        record("Light masks, force and exclusion precedence")
        light.parm("light_type").set("grid")
        light.parm("singlesided").set(1)
        light.parmTuple("areasize").set((1, 2))
        grid = mean(render())
        expected = 100 / (2 * math.pi**2) * rectangle_integral(1, 2)
        assert np.max(np.abs(grid - expected)) < expected * 0.025, (device, grid, expected)
        record(
            "Rectangular area geometry and normalized power match independent integral",
            measured=grid.tolist(),
            expected=expected,
        )
        light.parmTuple("s").set((2, 3, 1))
        scaled = mean(render())
        # Light 2.0's public canonical transform strips inactive OBJ scale.
        # Its Area Size, rather than the hidden s tuple, defines the rectangle.
        transform = light.worldTransformAtTime(hou.time())
        assert all(abs(transform.at(axis, axis) - 1) < 1e-5 for axis in range(3))
        assert np.max(np.abs(scaled - expected)) < expected * 0.025, (device, scaled, expected)
        light.parmTuple("s").set((1, 1, 1))
        render()
        record(
            "Houdini canonical light transform preserves Area Size despite inactive OBJ scale",
            measured=scaled.tolist(),
            expected=expected,
        )
        light.parm("normalizearea").set(0)
        radiance = mean(render())
        assert np.allclose(radiance / grid, 2, rtol=0.01), (radiance, grid)
        light.parmTuple("areasize").set((2, 2))
        larger = mean(render())
        expected = 100 / math.pi**2 * rectangle_integral(2, 2)
        assert np.max(np.abs(larger - expected)) < expected * 0.025, (device, larger, expected)
        record(
            "Unnormalized intensity preserves radiance as rectangle area changes",
            measured=larger.tolist(),
            expected=expected,
        )
        light.parm("normalizearea").set(1)
        light.parm("singlesided").set(0)
        two = mean(render())
        assert entry.session.status()["light_resources"] == 2
        assert np.allclose(two / (larger / 4), 0.5, rtol=0.015), (two, larger)
        light.parm("singlesided").set(1)
        light.parm("reverse").set(1)
        assert np.max(mean(render())) == 0
        light.parm("reverse").set(0)
        record("Two-sided power split and reverse direction have explicit ownership")
        light.setParms(dict(light_type="disk", singlesided=1, normalizearea=1))
        light.parmTuple("areasize").set((2, 2))
        disk = mean(render())
        # A radius-one disk has integral pi*R^2/(d^2+R^2).
        expected = 100 / (math.pi**3) * (math.pi / 26)
        assert np.max(np.abs(disk - expected)) < expected * 0.025, (device, disk, expected)
        record(
            "Native disk irradiance agrees with an independent circular integral",
            measured=disk.tolist(),
            expected=expected,
        )
        light.setParms(dict(light_type="sphere", light_contribprimary=1))
        light.parmTuple("areasize").set((1, 1))
        sphere = render()
        image = np.frombuffer(sphere["planes"]["combined"]["pixels"], np.float32).reshape(64, 96, 4)
        # Camera and emitter coincide here; put the visible sphere below it.
        light.parm("tz").set(3)
        sphere = render()
        image = np.frombuffer(sphere["planes"]["combined"]["pixels"], np.float32).reshape(64, 96, 4)
        visible = image[32, :, 0] > 1
        assert 22 <= np.count_nonzero(visible) <= 26, (device, np.where(visible)[0])
        light.parm("light_contribprimary").set(0)
        hidden = render()
        hidden_image = np.frombuffer(hidden["planes"]["combined"]["pixels"], np.float32).reshape(64, 96, 4)
        assert hidden_image[32, 48, 0] < 1 and hidden_image[32, 48, 0] > 0
        record("Native sphere uses Area Size as diameter, illuminates and respects Render Light Geometry")
        light.setParms(dict(light_type="point", tz=5, coneenable=0, light_contribprimary=0))
        reference = render()
        reference_pixels = np.frombuffer(reference["planes"]["combined"]["pixels"], np.float32).reshape(64, 96, 4)[
            :, :, :3
        ]
        x = (np.arange(96) + 0.5) * 4 / 96 - 2
        y = (np.arange(64) + 0.5) * 4 / 96 - 4 / 3
        direction = np.stack(np.broadcast_arrays(x[None, :], y[:, None], np.full((64, 96), -5)), axis=-1)
        direction /= np.linalg.norm(direction, axis=-1, keepdims=True)
        light.setParms(dict(coneenable=1, coneangle=20, conedelta=10))
        for roll in (0.5, 1.0, 2.0):
            light.parm("coneroll").set(roll)
            coned = render()
            coned_pixels = np.frombuffer(coned["planes"]["combined"]["pixels"], np.float32).reshape(64, 96, 4)[:, :, :3]
            t = np.clip(
                (-direction[:, :, 2] - math.cos(math.radians(20)))
                / (math.cos(math.radians(10)) - math.cos(math.radians(20))),
                0,
                1,
            )
            weight = t * t * (3 - 2 * t)
            weight = weight**roll if roll >= 1 else 1 - (1 - weight) ** (1 / roll)
            measured = coned_pixels[:, :, 0] / reference_pixels[:, :, 0]
            assert np.max(np.abs(measured - weight)) < 0.035, (device, roll, float(np.max(np.abs(measured - weight))))
        light.parm("ry").set(15)
        rotated = render()
        rotated_pixels = np.frombuffer(rotated["planes"]["combined"]["pixels"], np.float32).reshape(64, 96, 4)[:, :, :3]
        axis = np.array([-math.sin(math.radians(15)), 0, -math.cos(math.radians(15))])
        t = np.clip(
            (direction @ axis - math.cos(math.radians(20))) / (math.cos(math.radians(10)) - math.cos(math.radians(20))),
            0,
            1,
        )
        weight = (t * t * (3 - 2 * t)) ** 2
        assert np.max(np.abs(rotated_pixels[:, :, 0] / reference_pixels[:, :, 0] - weight)) < 0.035
        light.setParms(dict(ry=0, coneenable=0))
        render()
        record("Native spot Cone Angle, Delta, Rolloff and rotation agree with independent angular coverage")
        light.parm("light_type").set("distant")
        sun = mean(render())
        expected = 100 / math.pi
        assert np.max(np.abs(sun - expected)) < 0.02, (device, sun, expected)
        light.parm("light_type").set("sun")
        light.parm("vm_envangle").set(5)
        finite = mean(render())
        assert np.max(np.abs(finite / expected - 1)) < 0.01, (finite, expected)
        record(
            "Distant direction and finite sun angle have correct irradiation", distant=sun.tolist(), sun=finite.tolist()
        )
        # Place an opaque slab between the area/point and receiver, outside
        # the camera ray, to check the actual cast-shadow control.
        blocker = scene.createNode("geo", "shadow_blocker", run_init_scripts=False)
        blocker.parm("shop_materialpath").set(black.path())
        rop.parm("use_material_assignments").set(1)
        box = blocker.createNode("box")
        box.parmTuple("size").set((2, 2, 0.1))
        box.setRenderFlag(True)
        blocker.parmTuple("t").set((0, 0, 2.5))
        camera.parm("tx").set(3)
        light.parm("light_type").set("point")
        light.parmTuple("t").set((3, 0, 5))
        blocker.parm("tx").set(3)
        # Camera remains above the blocker: hide it from primary rays while
        # retaining its independently tested shadow visibility.
        blocker.addSpareParmTuple(
            hou.StringParmTemplate("vm_rendervisibility", "Render Visibility", 1, default_value=("-primary",))
        )
        shadow = mean(render())
        light.parm("shadow_type").set("off")
        no_shadow = mean(render())
        assert np.max(shadow) < 1e-5 and np.min(no_shadow) > 0.08, (shadow, no_shadow)
        light.parm("shadow_type").set("raytrace")
        blocker.destroy()
        rop.parm("use_material_assignments").set(0)
        camera.parm("tx").set(0)
        light.parmTuple("t").set((0, 0, 5))
        render()
        record("Shadow rays survive primary invisibility; Shadow Type Off removes actual occlusion")
        converted = entry.session.status()["geometry_conversions"]
        reference = mean(render())
        for kind in ("tube", "geo"):
            light.parm("light_type").set(kind)
            frame = render()
            status = entry.session.status()
            assert (
                np.max(mean(frame)) < 1e-5 and status["geometry_resources"] == 1 and status["light_resources"] == 0
            ), status
            assert any(
                d["code"] == "light.unavailable" and d["source"] == light.path() for d in status["source_diagnostics"]
            ), status
            light.parm("light_type").set("point")
            assert np.allclose(mean(render()), reference, atol=0.001, rtol=0)
            assert not entry.session.status()["source_diagnostics"]
        light.parm("atten_type").set("half")
        assert np.allclose(mean(render()), reference, atol=0.001, rtol=0)
        assert any(d["code"] == "light.unsupported_setting" for d in entry.session.status()["source_diagnostics"])
        light.parm("atten_type").set("physical")
        render()
        assert not entry.session.status()["source_diagnostics"]
        assert entry.session.status()["geometry_conversions"] == converted
        record(
            "Unsupported emitter shapes omit only the light; attenuation retains supported illumination; repairs clear diagnostics"
        )
        rop.parm("allowmotionblur").set(1)
        light.parm("light_exposure").setExpression("$T", hou.exprLanguage.Hscript)
        animated = mean(render())
        assert np.allclose(animated, reference * 2 ** hou.time(), atol=0.001, rtol=0)
        # Lights do not blur over the shutter (as in Blender), so a moving light reports nothing.
        assert not entry.session.status()["source_diagnostics"], entry.session.status()["source_diagnostics"]
        # +-2.5 units over the shutter would change the centre irradiance by about 12% if it blurred.
        light.parm("tx").setExpression("$T * 240", hou.exprLanguage.Hscript)
        assert np.allclose(mean(render()), reference * 2 ** hou.time(), atol=0.001, rtol=0)
        assert not entry.session.status()["source_diagnostics"], entry.session.status()["source_diagnostics"]
        light.parm("tx").deleteAllKeyframes()
        light.parm("tx").set(0)
        rop.parm("allowmotionblur").set(0)
        light.parm("light_exposure").deleteAllKeyframes()
        light.parm("light_exposure").set(0)
        render()
        assert not entry.session.status()["source_diagnostics"]
        record("Animated and moving lights under motion blur use requested-frame energy without a diagnostic")
        rop.parm("vobject").set("")
        rop.parm("lights").set("")
        render()
        assert (
            entry.session.status()["object_resources"]
            == entry.session.status()["light_resources"]
            == entry.session.status()["geometry_resources"]
            == 0
        )
        record("Light type changes and final deletion retire resources")
    finally:
        registry.close_all()
    assert entry.session.status()["owned_geometry_bytes"] == entry.session.status()["device_allocation_bytes"] == 0
    reports.append(dict(device=device, checks=checks, closed=entry.session.status()))
report = dict(result="passed", devices=reports)
(destination / "houdini-object-lights-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", checks=[len(report["checks"]) for report in reports])))
