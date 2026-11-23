# SPDX-License-Identifier: Apache-2.0
"""Dedicated light graphs, native create/open callbacks, live edits and recovery."""

from support.paths import artifacts_root, artifact_label

import argparse
import json
import os
import re
from pathlib import Path
import time
import traceback

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import light_materials, materials, native_scene, object_scene, registry
from cycles_houdini.material_previews import PreviewService
from light_shader_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
args = parser.parse_args()
device = args.device
if device == "optix4090":
    device = next(d["id"] for d in ch.devices() if d["backend"] == "OPTIX" and "4090" in d["name"])
destination = artifacts_root() / "light-material-20261026"
destination.mkdir(parents=True, exist_ok=True)
report_path = destination / ("material-" + re.sub(r"[^A-Za-z0-9_.-]", "_", args.device) + ".json")
report = dict(result="running", device=device, checks=[])
f = Fixture(device)
f.rop.setParms(dict(samples=64, ipr_samples=64))
entry = f.session()
preview = None


def record(name, **details):
    report["checks"].append(dict(name=name, **details))


def render(multiplier=1, warning=None):
    frame = f.update()
    status = entry.session.status()
    f.check(frame, multiplier)
    assert status["render_device_ids"] == [device], status
    assert {d["code"] for d in status["source_diagnostics"]} == ({warning} if warning else set()), status
    assert not status["error"], status
    return f.pixels(frame).copy(), status


try:
    baseline, status = render()
    pid = status["renderer_pid"]
    conversions = status["geometry_conversions"]
    selector = f.light.parm("cycles_light_material")
    f.light.updateParmStates()
    assert selector.parmTemplate().joinsWithNext()
    assert f.light.parm("cycles_light_material_create").parmTemplate().joinsWithNext()
    assert not f.light.parm("cycles_light_material_create").isHidden()
    assert f.light.parm("cycles_light_material_open").isHidden()
    f.light.parm("cycles_light_material_create").pressButton()
    material = hou.node(selector.eval())
    assert material.type().name() == materials.LIGHT_BUILDER and material.name() == f.light.name()
    assert material.parent() == hou.node("/mat")
    emission = material.node("emission")
    output = material.node("light_output")
    assert len(material.children()) == 2 and emission.evalParm("strength") == 1
    assert tuple(emission.parmTuple("color").eval()) == (1, 1, 1)
    assert output.type().name() == materials.LIGHT_OUTPUT and output.inputNames() == ("surface",)
    assert output.inputs() == (emission,)
    assert not material.parm("cycles_displacement_method")
    f.light.updateParmStates()
    assert f.light.parm("cycles_light_material_create").isHidden()
    assert not f.light.parm("cycles_light_material_open").isHidden()
    created, _ = render()
    np.testing.assert_allclose(created, baseline, atol=3e-7, rtol=3e-6)
    record(
        "Native + creates and assigns a same-name unity light graph; button switches to Open without changing brightness",
        material=material.path(),
    )

    before = tuple(n.path() for n in hou.node("/mat").children())
    definition = object_scene.light_definitions(f.light, hou.time(), [hou.time()])
    f.light.parm("cycles_light_material_open").pressButton()
    assert tuple(n.path() for n in hou.node("/mat").children()) == before
    assert object_scene.light_definitions(f.light, hou.time(), [hou.time()]) == definition
    record("Open callback preserves assignment, material count and the render definition")

    # Another light with the same basename must not overwrite a material.
    other_parent = hou.node("/obj").createNode("subnet", "light_material_collision")
    other = other_parent.createNode("cycles::light::1", f.light.name())
    other.parm("light_enable").set(0)
    another = light_materials.create_or_focus(other)
    assert another != material and another.name().startswith(other.name())
    assert output.inputs() == (emission,)
    other_parent.destroy()
    another.destroy()
    record("Name collisions use Houdini unique names and preserve existing materials")

    f.rop.parm("ipr_samples").set(1000000)
    generation = entry.apply(native_scene.capture(f.rop), hou.time())
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        active = entry.session.status()
        if (
            active["committed_generation"] == generation
            and active.get("backend_state") == "rendering"
            and active.get("current_sample", 0) > 0
        ):
            break
        time.sleep(0.02)
    else:
        raise AssertionError(("IPR never started", active))
    emission.parmTuple("color").set((0.2, 0.4, 0.8))
    f.rop.parm("ipr_samples").set(64)
    edited, status = render((0.2, 0.4, 0.8))
    assert status["renderer_pid"] == pid and status["geometry_conversions"] == conversions
    output.setInput(0, None)
    render(1, "light.shader_unavailable")
    output.setNamedInput("surface", emission, "emission")
    render((0.2, 0.4, 0.8))
    entry.session.cancel()
    restarted, _ = render((0.2, 0.4, 0.8))
    np.testing.assert_allclose(restarted, edited, atol=3e-7, rtol=3e-6)
    record("Shader edits during tracing, output disconnection/repair and Stop/restart retain the worker and geometry")

    # Flatten a typed shader subnetwork within the new material domain.
    subnet = materials.create_subnet(material, "light_group")
    terminal = materials.create_connector(subnet, "shader", kind="output", type="bsdf")
    nested = subnet.createNode("cycles::emission::1", "nested_emission")
    nested.parm("strength").set(1)
    nested.parmTuple("color").set((0.3, 0.5, 0.7))
    terminal.setInput(0, nested, 0)
    output.setNamedInput("surface", subnet, "shader")
    render((0.3, 0.5, 0.7))
    record("Light material subnetwork flattens to supported emission shading")
    output.setNamedInput("surface", emission, "emission")
    subnet.destroy()

    # An invalid assignment remains a usable light and Open never creates a replacement.
    selector.set("/mat/missing_light_material")
    count = len(hou.node("/mat").children())
    try:
        light_materials.create_or_focus(f.light)
    except ValueError as error:
        assert "does not exist" in str(error)
    else:
        raise AssertionError("Missing assigned material was not reported")
    assert len(hou.node("/mat").children()) == count
    render(1, "light.shader_unavailable")
    selector.set(material.path())
    render((0.2, 0.4, 0.8))
    record("Missing material retains base illumination, Open reports it, and assignment repairs")

    # Light builders participate in the existing native thumbnail service.
    entry.session.close(wait=True)
    preview = PreviewService(device=device, samples=4, busy=lambda: False)
    preview.request(material, 32)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        preview.tick(force=True)
        if preview.completed and preview.active is None and not preview.pending:
            break
        time.sleep(0.01)
    else:
        raise AssertionError(preview.diagnostics())
    assert material.sessionId() not in preview.errors, preview.errors
    assert ch._native.thumbnail_pixels(material.sessionId())
    record("Dedicated light material receives a native rendered thumbnail")
    preview.close(wait=True)
    preview = None

    light_path = f.light.path()
    material_path = material.path()
    path = destination / ("light-material-" + artifact_label(args.device) + ".hiplc")
    hou.hipFile.save(str(path), save_to_recent_files=False)
    registry.close_all()
    hou.hipFile.load(str(path), suppress_save_prompt=True)
    loaded = hou.node(material_path)
    assert loaded.type().name() == materials.LIGHT_BUILDER
    assert loaded.node("light_output").inputs() == (loaded.node("emission"),)
    assert hou.node(light_path).evalParm("cycles_light_material") == material_path
    assert materials.material_graph(loaded)["surface"]
    record("Light material type, connections and assignment survive HIP reload")
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    if preview:
        preview.close(wait=True)
    registry.close_all()
    report_path.write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]), report=str(report_path))))
