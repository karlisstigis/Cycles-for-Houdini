# SPDX-License-Identifier: Apache-2.0
"""Light transform updates must change pixels, not merely advance a generation."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
import argparse
import json
import os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene, light_properties, registry

device = argparse.ArgumentParser()
device.add_argument("--device", default="CPU")
device = device.parse_args().device
obj = hou.node("/obj").createNode("geo", "light_update_receiver", run_init_scripts=False)
grid = obj.createNode("grid")
grid.setParms(dict(orient=0, rows=2, cols=2, sizex=8, sizey=8))
blocker = hou.node("/obj").createNode("geo", "light_update_blocker", run_init_scripts=False)
box = blocker.createNode("box")
box.setParms(dict(sizex=0.8, sizey=1.2, sizez=0.4, tz=1))
camera = hou.node("/obj").createNode("cam", "light_update_camera")
camera.setParms(dict(tz=8, projection=1, orthowidth=8))
rop = hou.node("/out").createNode("cycles_ipr", "light_update")
set_rop_devices(rop, device)
rop.setParms(
    dict(
        source_mode="objects",
        vobject=obj.path() + " " + blocker.path(),
        camera=camera.path(),
        resx=128,
        resy=96,
        samples=64,
        ipr_samples=64,
        use_material_assignments=0,
        material_kind="diffuse",
        allowmotionblur=0,
        use_denoise=0,
        ipr_use_denoise=0,
    )
)
rop.parmTuple("basecolor").set((1, 1, 1))
rop.parmTuple("background").set((0, 0, 0))
checks = []
for kind in ("hlight::2.0", light_properties.LIGHT_TYPE):
    light = hou.node("/obj").createNode(kind, "update_light")
    type_parm = "cycles_light_type" if light_properties.is_cycles_light(light) else "light_type"
    light.setParms({type_parm: "grid", "tz": 4, "tx": -2, "light_intensity": 500, "singlesided": 1})
    rop.parm("lights").set(light.path())
    entry = registry.for_rop(rop, native_scene.configuration(rop))

    def pixels():
        frame = entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
        return np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).copy()

    try:
        before = pixels()
        assert before.max() > 0.1
        conversions = entry.session.status()["geometry_conversions"]
        notified = []
        observer = native_scene.SceneObserver(rop, lambda: notified.append(True))
        light.parm("tx").set(2)
        assert notified, "Transform was not observed"
        after = pixels()
        delta = float(np.mean(np.abs(after - before)))
        assert delta > 0.01, (kind, "Light movement did not change rendered pixels", delta)
        assert entry.session.status()["geometry_conversions"] == conversions
        # A cold renderer at the edited position is an independent update oracle.
        with ch.Session(device=device, width=128, height=96, samples=64) as reference:
            frame = reference.wait(reference.apply(native_scene.capture(rop)))
            expected = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32)
        error = float(np.mean(np.abs(after - expected)))
        assert error < 0.002, (kind, "Updated light differs from fresh scene", error)
        checks.append(dict(type=kind, delta=delta, reference_error=error))
        # Rapid edits while capture/conversion are pending must preserve the
        # latest light delta, including an unchanged final refresh capture.
        for x in (-1.0, 0.0, 1.0, -2.0):
            light.parm("tx").set(x)
            entry.apply(native_scene.capture(rop), hou.time())
        burst = pixels()
        error = float(np.mean(np.abs(burst - before)))
        assert error < 0.002, (kind, "Rapid edits retained a stale light", error)
        # Compare shadow-on/off at each light position. Illumination falloff
        # and a visible emitter are not evidence that the shadow itself moved.
        centroids = []
        for x in (-2.0, 2.0):
            light.parm("tx").set(x)
            if kind == light_properties.LIGHT_TYPE:
                light.parm("cycles_light_cast_shadow").set(1)
            else:
                light.parm("shadow_type").set("raytrace")
            shadow = pixels().reshape(96, 128, 4)[:, :, :3].mean(axis=2)
            if kind == light_properties.LIGHT_TYPE:
                light.parm("cycles_light_cast_shadow").set(0)
            else:
                light.parm("shadow_type").set("off")
            lit = pixels().reshape(96, 128, 4)[:, :, :3].mean(axis=2)
            weight = np.maximum(lit - shadow, 0.0)
            # Restrict to the receiver, excluding the blocker silhouette.
            weight[:, 58:70] = 0
            weight[:30] = 0
            weight[66:] = 0
            assert weight.sum() > 1.0, (kind, "No measurable receiver shadow")
            centroids.append(float((weight * np.arange(128)[None, :]).sum() / weight.sum()))
        assert centroids[0] - centroids[1] > 8.0, (kind, "Shadow stayed in place", centroids)
        checks[-1].update(burst_error=error, shadow_centroids=centroids)
        observer.close()
    finally:
        registry.close_all()
        light.destroy()
report = artifacts_root() / "stage3/native-scene/light-update.json"
report.parent.mkdir(parents=True, exist_ok=True)
report.write_text(json.dumps(dict(result="passed", device=device, checks=checks), indent=2))
print(report)
