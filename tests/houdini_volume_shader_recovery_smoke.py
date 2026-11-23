# SPDX-License-Identifier: Apache-2.0
from support.paths import artifacts_root
import argparse, json, os
from pathlib import Path
import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene, registry
from volume_shader_recovery_fixture import fixture, check

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
rop, edit, dest = fixture(device)
entry = registry.for_rop(rop, native_scene.configuration(rop))
events = []
try:
    for mode in (0, 1, 0, 2, 0, 3, 0, 4, 0, 5, 0, 6, 0):
        edit.parm("mode").set(mode)
        frame = entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
        state = entry.session.status()
        check(frame, state, mode)
        count = state["geometry_conversions"]
        entry.session.wait(entry.apply(dict(camera_path=rop.evalParm("camera")), hou.time()))
        assert entry.session.status()["geometry_conversions"] == count
        assert entry.session.status()["source_diagnostics"] == state["source_diagnostics"]
        events.append(dict(mode=mode, diagnostics=state["source_diagnostics"]))
    # Change only shader demand: retained volumes must switch and recover without recapture.
    field = hou.node("/mat/volume_absorption/required_field")
    before = entry.session.status()["geometry_conversions"]
    for name in ("missing_shader_edit", "extinction"):
        field.parm("attribute").set(name)
        frame = entry.session.wait(entry.apply(dict(material_resolver=native_scene.resolve_materials), hou.time()))
        state = entry.session.status()
        assert state["geometry_conversions"] == before
        assert sum(d["code"] == "volume.shader_field_unavailable" for d in state["source_diagnostics"]) == (
            2 if name != "extinction" else 0
        ), state
        if name == "extinction":
            check(frame, state, 0)
    events.append(dict(check="Shader-only demand changes recover both bindings without geometry conversion"))
    shader_counts = []
    for repeat in range(4):
        for name in ("missing_shader_edit", "extinction"):
            field.parm("attribute").set(name)
            entry.session.wait(entry.apply(dict(material_resolver=native_scene.resolve_materials), hou.time()))
            state = entry.session.status()
            assert state["geometry_conversions"] == before
            shader_counts.append(state["engine_shader_resources"])
    assert len(set(shader_counts)) == 1, shader_counts
    assert not entry.session.status()["source_diagnostics"]
    events.append(dict(check="Repeated shader fallback and repair reuse shader slots", shader_counts=shader_counts))
    # A disconnected missing-field node must not create a requirement.
    unused = field.parent().createNode("cycles::attribute::1", "unused")
    unused.parm("attribute").set("never_provided")
    check(entry.session.wait(entry.apply(native_scene.capture(rop), hou.time())), entry.session.status(), 0)
    unused.destroy()
    for n, mode in enumerate((0, 1, 0, 2, 0), 1):
        edit.parm("mode").set(mode)
        entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
        path = dest / f"recovery.{n:04d}.exr"
        entry.session.write_exr(path)
        reader = oiio.ImageInput.open(str(path))
        assert reader
        issues = json.loads(reader.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        reader.close()
        assert bool(issues) == (mode != 0), issues
    with ch.Session(device=device, width=80, height=48, samples=512, execution="sync", interactive=False) as final:
        for mode in (0, 1, 2, 3, 6, 0):
            edit.parm("mode").set(mode)
            frame = final.wait(final.apply(native_scene.capture(rop), at_time=hou.time()))
            check(frame, final.status(), mode)
    hou.hipFile.save(str(dest / "volume-shader-recovery.hiplc"))
finally:
    registry.close_all()
report = dict(result="passed", device=device, events=events, sequence_frames=5)
path = artifacts_root() / "stage3/materials/recovery/houdini-volume-shader-recovery-smoke.json"
path.write_text(json.dumps(report, indent=2))
print(json.dumps(report))
