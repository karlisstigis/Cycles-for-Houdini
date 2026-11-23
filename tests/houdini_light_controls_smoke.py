# SPDX-License-Identifier: Apache-2.0
"""Light temperature versus native Blackbody, scalar area sizes and live repairs."""

from support.paths import artifacts_root, artifact_label

import argparse
import json
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
import PyOpenColorIO as OCIO
import cycles_houdini as ch
from cycles_houdini import light_properties, light_viewer_state, menu_actions, native_scene, object_scene, registry
from light_shader_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.add_argument("--working-space", default="default")
args = parser.parse_args()
device = args.device
if device == "optix4090":
    device = next(d["id"] for d in ch.devices() if d["backend"] == "OPTIX" and "4090" in d["name"])
if args.working_space != "default":
    original = OCIO.GetCurrentConfig()
    config = OCIO.Config.CreateFromStream(original.serialize())
    config.setWorkingDir(original.getWorkingDir())
    target = config.getColorSpace(args.working_space)
    assert target, args.working_space
    config.setRole(OCIO.ROLE_SCENE_LINEAR, target.getName())
    OCIO.SetCurrentConfig(config)
    ch.synchronize_color_configuration()

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "light-controls-20261026"
destination.mkdir(parents=True, exist_ok=True)
report_path = destination / (
    "controls-" + artifact_label(args.device) + "-" + artifact_label(args.working_space) + ".json"
)
report = dict(result="running", device=device, working_space=ch.color_policy()["working_space"], checks=[])
f = Fixture(device)
f.rop.setParms(dict(samples=64, ipr_samples=64))
entry = f.session()


def record(label, **details):
    report["checks"].append(dict(label=label, **details))


def render():
    frame = f.update()
    status = entry.session.status()
    assert status["state"] == "complete" and not status["error"], status
    assert not status["source_diagnostics"], status["source_diagnostics"]
    pixels = f.pixels(frame).copy()
    assert np.isfinite(pixels).all()
    return pixels, status


try:
    baseline, status = render()
    native_definition = object_scene.light_definitions(f.light, hou.time(), [hou.time()])
    pid = status["renderer_pid"]
    conversions = status["geometry_conversions"]
    f.check(dict(planes={"combined": dict(pixels=baseline.tobytes(), channels=4)}, width=64, height=64))
    assert not f.light.evalParm("cycles_light_use_temperature")
    f.light.parm("cycles_light_temperature").set(2700)
    off, _ = render()
    assert object_scene.light_definitions(f.light, hou.time(), [hou.time()]) == native_definition
    # GPU accumulation order may vary even for an identical light definition.
    np.testing.assert_allclose(off, baseline, atol=2e-7, rtol=2e-6)
    record("Temperature disabled retains the exact light definition and native image within floating-point tolerance")

    material = menu_actions.create_light_material()
    emission = material.node("emission")
    emission.parm("strength").set(1)
    blackbody = material.createNode("cycles::blackbody::1", "temperature_reference")
    f.light.parm("cycles_light_material").set(material.path())
    f.light.parmTuple("cycles_light_color").set((0.7, 0.8, 0.9))
    for kelvin in (800, 2700, 6500, 12000, 20000):
        f.light.parm("cycles_light_use_temperature").set(0)
        blackbody.parm("temperature").set(kelvin)
        emission.setNamedInput("color", blackbody, "color")
        reference, _ = render()
        emission.setInput(emission.inputNames().index("color"), None)
        emission.parmTuple("color").set((1, 1, 1))
        f.light.setParms(dict(cycles_light_use_temperature=1, cycles_light_temperature=kelvin))
        actual, _ = render()
        np.testing.assert_allclose(actual, reference, atol=2e-5, rtol=3e-5)
        color = light_properties.emission_color(f.light)
        np.testing.assert_allclose(f.light.parmTuple("cycles_light_color").eval(), (0.7, 0.8, 0.9), rtol=1e-6)
        np.testing.assert_allclose(f.light.parmTuple("light_color").eval(), color, rtol=1e-6)
        f.light.parm("light_exposure").set(1)
        doubled, _ = render()
        np.testing.assert_allclose(doubled[..., :3], actual[..., :3] * 2, atol=2e-5, rtol=3e-5)
        f.light.parm("light_exposure").set(0)
        record(
            "Temperature matches rendered native Blackbody and preserves tint/exposure",
            kelvin=kelvin,
            color=list(color),
            max_absolute_error=float(np.max(np.abs(actual - reference))),
        )

    # Exact runtime checks are also needed when expressions bypass UI limits.
    for kelvin in (float("nan"), float("inf"), 799, 20001):
        try:
            ch._native.blackbody_temperature_color(kelvin)
        except RuntimeError as error:
            assert "Temperature" in str(error)
        else:
            raise AssertionError("Invalid temperature accepted")
    f.light.parm("cycles_light_temperature").setExpression("799", hou.exprLanguage.Hscript)
    assert f.light.evalParm("cycles_light_temperature") == 800
    render()
    # Houdini clamps expressions to strict parameter limits, like Blender.
    f.light.parm("cycles_light_temperature").deleteAllKeyframes()
    f.light.parm("cycles_light_temperature").set(2700)
    render()
    record("Native invalid values are rejected; the UI clamps out-of-range expressions and allows repair")

    f.light.setParms(dict(cycles_light_use_temperature=0, cycles_light_material=""))
    f.light.parmTuple("cycles_light_color").set((1, 1, 1))
    state = object.__new__(light_viewer_state.LightState)
    state.active = True
    state.node = f.light
    for shape in ("square", "disk", "rectangle", "ellipse"):
        f.light.setParms(dict(cycles_light_type="grid", cycles_light_area_shape=shape, areasizex=2, areasizey=0.75))
        f.light.updateParmStates()
        uniform = shape in ("square", "disk")
        assert f.light.parm("areasizex").parmTemplate().numComponents() == 1
        assert not f.light.parm("areasizex").isHidden()
        assert f.light.parm("areasizey").isHidden() == uniform
        size = (2, 2 if uniform else 0.75)
        assert light_properties.area_size(f.light) == size
        assert f.light.parmTuple("areasize").eval() == size
        definition = object_scene.light_definitions(f.light, hou.time(), [hou.time()])[0]
        assert tuple(definition["size"]) == size
        _, status = render()
        state.onHandleToState(
            dict(
                node=f.light,
                handle=light_viewer_state.ELLIPSE if shape in ("disk", "ellipse") else light_viewer_state.RECTANGLE,
                parms={"sizex": 2, "sizey": 3},
                mod_parms=["sizey"],
            )
        )
        expected = (3, 3) if uniform else (2, 3)
        assert light_properties.area_size(f.light) == expected
        assert f.light.parmTuple("areasize").eval() == expected
        record(
            "Area UI, renderer, native light and resize handle agree",
            shape=shape,
            size=list(size),
            resized=list(expected),
        )

    # Edit while the worker is actually tracing, then Stop and restart it.
    f.light.setParms(
        dict(
            cycles_light_type="grid",
            cycles_light_area_shape="disk",
            cycles_light_use_temperature=1,
            cycles_light_temperature=2700,
        )
    )
    f.rop.parm("ipr_samples").set(1000000)
    generation = entry.apply(native_scene.capture(f.rop), hou.time())
    until = time.monotonic() + 30
    while time.monotonic() < until:
        status = entry.session.status()
        if (
            status["committed_generation"] == generation
            and status.get("backend_state") == "rendering"
            and status.get("current_sample", 0) > 0
        ):
            break
        time.sleep(0.02)
    else:
        raise AssertionError(("No active IPR render", status))
    f.light.setParms(dict(cycles_light_temperature=6500, areasizex=1.25))
    f.rop.parm("ipr_samples").set(64)
    changed, status = render()
    assert status["renderer_pid"] == pid and status["geometry_conversions"] == conversions
    entry.session.cancel()
    restarted, _ = render()
    np.testing.assert_allclose(restarted, changed, atol=2e-5, rtol=3e-5)
    record("Temperature and scalar size edits during active IPR, Stop and restart", renderer_pid=pid)

    # The optional native light tab uses the same temperature semantics.
    f.light.parm("light_enable").set(0)
    native = f.scene.createNode("hlight::2.0", "native_temperature")
    light_properties.install(native)
    native.setParms(
        dict(
            tz=3,
            light_type="point",
            light_intensity=100,
            light_exposure=0,
            cycles_light_use_temperature=1,
            cycles_light_temperature=2700,
        )
    )
    frame = f.update()
    f.check(frame, light_properties.emission_color(native))
    record("Native Houdini light optional tab also supports temperature")
    native.destroy()
    f.light.parm("light_enable").set(1)
    render()

    # Persist the actual single-component parameters, then render after reload.
    scene_path = destination / (
        "controls-" + artifact_label(args.device) + "-" + artifact_label(args.working_space) + ".hiplc"
    )
    light_path = f.light.path()
    rop_path = f.rop.path()
    hou.hipFile.save(str(scene_path))
    registry.close_all()
    hou.hipFile.load(str(scene_path), suppress_save_prompt=True)
    loaded = hou.node(light_path)
    assert loaded.evalParm("areasizex") == 1.25 and loaded.evalParm("cycles_light_temperature") == 6500
    assert light_properties.area_size(loaded) == (1.25, 1.25)
    record("Scalar dimensions and temperature survive HIP reload")
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]), report=str(report_path))))
