# SPDX-License-Identifier: Apache-2.0
"""Independent native OBJ selection, render outputs, updates and retirement."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene, object_scene, registry, passes
from native_object_fixture import fixture

destination = artifacts_root() / "stage3/native-scene"
destination.mkdir(parents=True, exist_ok=True)
hou.setFrame(1)
f = fixture()
passes.add(f.rop, "depth")
reports = []


def sample(frame, x, y=0):
    image = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(96, 128, 4)
    ix, iy = round(64 + 32 * x), round(48 + 32 * y)
    return image[iy - 1 : iy + 2, ix - 1 : ix + 2].mean((0, 1))


def expect(frame, x, color, y=0, alpha=1):
    actual = sample(frame, x, y)
    assert np.allclose(actual, (*color, alpha), atol=0.001, rtol=0), (x, y, actual, color, alpha)
    return actual.tolist()


for device in ("CPU", ch.default_device()):
    set_rop_devices(f.rop, device)
    entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
    checks = []

    def render():
        return entry.session.wait(
            entry.apply(native_scene.capture(f.rop, deleted_sources=entry.observer.deleted_sources), hou.time())
        )

    def record(name):
        checks.append(dict(name=name, status=entry.session.status()))

    try:
        frame = render()
        expect(frame, -1, f.colors["red"])
        expect(frame, 1, f.colors["blue"])
        expect(frame, 0, (0, 0, 0), 1, 0)
        assert entry.session.status()["geometry_resources"] == 2 and entry.session.status()["light_resources"] == 1
        assert len(entry.source_objects) == 2
        keys = entry.session.native_object_keys()
        record("Nested OBJ transforms, scoped root and render SOP rather than display SOP")
        f.rop.parm("forceobject").set(f.hidden.path())
        frame = render()
        expect(frame, 0, f.colors["green"], 1)
        record("Absolute Force Objects includes a hidden object")
        f.rop.parm("excludeobject").set(f.hidden.path())
        frame = render()
        expect(frame, 0, (0, 0, 0), 1, 0)
        record("Exclude Objects overrides Force Objects")
        f.rop.parm("forceobject").set("")
        f.rop.parm("excludeobject").set("")
        converted = entry.session.status()["geometry_conversions"]
        scans = entry.session.status()["partition_scans"]
        f.camera.parm("tx").set(0.1)
        render()
        assert (
            entry.session.status()["geometry_conversions"] == converted
            and entry.session.status()["partition_scans"] == scans
        )
        f.camera.parm("tx").set(0)
        frame = render()
        record("Camera edit retains all selected geometry and partition metadata")
        f.parent.setDisplayFlag(False)
        frame = render()
        expect(frame, -1, (0, 0, 0), alpha=0)
        expect(frame, 1, f.colors["blue"])
        record("Hidden parent subnet excludes its child")
        f.parent.setDisplayFlag(True)
        render()
        f.red.parm("tdisplay").set(1)
        f.red.parm("display").setExpression("$F < 2", hou.exprLanguage.Hscript)
        at = hou.frameToTime(3)
        update = native_scene.capture(f.rop, at)
        assert hou.frame() == 1 and len(update["geometry"]) == 1
        frame = entry.session.wait(entry.apply(update, at))
        expect(frame, -1, (0, 0, 0), alpha=0)
        record("Animated display is evaluated at explicit batch time")
        f.red.parm("display").deleteAllKeyframes()
        f.red.parm("display").set(1)
        f.red.parm("tdisplay").set(0)
        render()
        f.red.setName("renamed_receiver")
        frame = render()
        assert entry.session.native_object_keys() == keys
        expect(frame, -1, f.colors["red"])
        record("OBJ rename retains stable node identities")
        before = entry.session.status()["geometry_conversions"]
        f.parent.parm("tx").set(1.25)
        frame = render()
        expect(frame, -0.75, f.colors["red"])
        assert entry.session.status()["geometry_conversions"] == before
        f.parent.parm("tx").set(1)
        render()
        record("Subnet transform edits update instances without geometry conversion")
        f.red.parm("vm_rendervisibility").set("-primary")
        before = entry.session.status()["geometry_conversions"]
        frame = render()
        expect(frame, -1, (0, 0, 0), alpha=0)
        assert (
            entry.session.status()["geometry_conversions"] == before
            and entry.session.status()["geometry_resources"] == 2
        )
        f.red.parm("vm_rendervisibility").set("*")
        frame = render()
        expect(frame, -1, f.colors["red"])
        record("Ray visibility changes preserve geometry ownership")
        bundle = hou.addNodeBundle("native_cycles_selection")
        bundle.addNode(f.red)
        f.rop.parm("vobject").set("@" + bundle.name())
        frame = render()
        expect(frame, -1, f.colors["red"])
        expect(frame, 1, (0, 0, 0), alpha=0)
        serial = entry.change_serial
        bundle.addNode(f.blue)
        entry.observer.selection_changed()
        assert entry.change_serial > serial
        frame = render()
        expect(frame, 1, f.colors["blue"])
        record("Bundle membership edits trigger observation and update selection")
        bundle.destroy()
        f.rop.parm("vobject").set("*")
        f.blue_display.setRenderFlag(True)
        frame = render()
        depth = np.frombuffer(frame["planes"]["depth"]["pixels"], np.float32).reshape(96, 128)[48, 96]
        assert abs(float(depth) - 4.5) < 0.001, (depth, entry.session.status())
        record("Render flag switches discover and convert the new SOP")
        f.blue_source.setRenderFlag(True)
        render()
        f.rop.parm("source_mode").set("sop")
        f.rop.parm("soppath").set(f.red_source.path())
        f.rop.parm("lightpath").set(f.light.path())
        frame = render()
        expect(frame, -1, f.colors["red"])
        expect(frame, 1, (0, 0, 0), alpha=0)
        assert entry.session.status()["object_resources"] == 1 and entry.session.status()["light_resources"] == 1
        light_path = f.light.path()
        generation = entry.session.status()["generation"]
        last = entry.session.frame_info()
        f.rop.parm("lightpath").set(light_path + "_missing")
        try:
            render()
        except RuntimeError as error:
            assert "Missing light OBJ" in str(error)
        else:
            raise AssertionError("Mistyped explicit light path silently accepted")
        assert entry.session.status()["generation"] == generation and entry.session.frame_info() == last
        f.rop.parm("lightpath").set(light_path)
        render()
        record("Source-mode switch reuses the renderer; invalid paths remain errors")
        f.light.destroy()
        assert light_path in entry.observer.deleted_sources
        frame = render()
        expect(frame, -1, f.colors["red"])
        assert entry.session.status()["light_resources"] == 0 and not entry.source_lights
        assert entry.observer.deleted_sources == {light_path}
        f.camera.parm("tx").set(0.1)
        frame = render()
        assert entry.session.status()["light_resources"] == 0
        f.camera.parm("tx").set(0)
        render()
        f.light = f.scene.createNode("hlight", light_path.rsplit("/", 1)[1])
        f.light.parm("light_intensity").set(100)
        f.light.parmTuple("t").set((-4, 0, 5))
        render()
        assert entry.session.status()["light_resources"] == 1 and not entry.observer.deleted_sources
        f.rop.parm("source_mode").set("objects")
        render()
        record("Observed deletion retires an explicit light; re-created path restores it")
        f.rop.parm("vobject").set("")
        f.rop.parm("lights").set("")
        render()
        status = entry.session.status()
        assert (
            status["geometry_resources"]
            == status["object_resources"]
            == status["light_resources"]
            == status["partition_cache_bytes"]
            == 0
        )
        record("Empty authoritative selection retires all native geometry, lights and partition metadata")
        f.rop.parm("vobject").set("*")
        f.rop.parm("lights").set("*")
        frame = render()
        expect(frame, -1, f.colors["red"])
        expect(frame, 1, f.colors["blue"])
        generation = entry.session.status()["generation"]
        last = entry.session.frame_info()
        f.red.parm("vm_rendervisibility").set("unknown_ray")
        try:
            render()
        except ValueError as error:
            assert "visibility" in str(error)
        else:
            raise AssertionError("Unsupported visibility silently accepted")
        assert entry.session.status()["generation"] == generation and entry.session.frame_info() == last
        f.red.parm("vm_rendervisibility").set("*")
        render()
        record("Unsupported visibility rejects before mutation and recovers")
        assert object_scene.visibility("primary|shadow") == 97
        assert object_scene.visibility("-diffuse&-reflect&-refract") == 241
        assert object_scene.visibility("-(primary|shadow)") == 158
        record("Pinned visibility masks and compound ray-set semantics")
        f.rop.parm("allowmotionblur").set(1)
        generation = entry.session.status()["generation"]
        last = entry.session.frame_info()
        f.red.parm("tdisplay").set(1)
        f.red.parm("display").setExpression("$T >= 0", hou.exprLanguage.Hscript)
        try:
            render()
        except ValueError as error:
            assert "scene membership changes" in str(error)
        else:
            raise AssertionError("Temporal scene membership was frozen silently")
        f.red.parm("display").deleteAllKeyframes()
        f.red.parm("display").set(1)
        f.red.parm("tdisplay").set(0)
        f.red.parm("vm_rendervisibility").setExpression('ifs($T < 0, "-primary", "*")', hou.exprLanguage.Hscript)
        try:
            render()
        except ValueError as error:
            assert "Animated OBJ ray visibility" in str(error)
        else:
            raise AssertionError("Temporal ray visibility was frozen silently")
        f.red.parm("vm_rendervisibility").deleteAllKeyframes()
        f.red.parm("vm_rendervisibility").set("*")
        f.rop.parm("source_mode").set("sop")
        f.rop.parm("soppath").setExpression(
            f'ifs($T < 0, "{f.red_source.path()}", "{f.blue_source.path()}")', hou.exprLanguage.Hscript
        )
        try:
            render()
        except ValueError as error:
            assert "sources change over the shutter" in str(error)
        else:
            raise AssertionError("Temporal SOP source was frozen silently")
        assert entry.session.status()["generation"] == generation and entry.session.frame_info() == last
        f.rop.parm("soppath").deleteAllKeyframes()
        f.rop.parm("soppath").set(f.red_source.path())
        f.rop.parm("source_mode").set("objects")
        f.rop.parm("allowmotionblur").set(0)
        render()
        record("Temporal membership, ray visibility and SOP-source changes reject atomically and recover")
    finally:
        registry.close_all()
    assert entry.session.status()["owned_geometry_bytes"] == entry.session.status()["device_allocation_bytes"] == 0
    reports.append(dict(device=device, checks=checks, closed=entry.session.status()))
report = dict(result="passed", devices=reports)
(destination / "houdini-object-scene-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", checks=[len(report["checks"]) for report in reports])))
