# SPDX-License-Identifier: Apache-2.0
from support.paths import artifacts_root
import argparse, json, os
from pathlib import Path
import hou
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene, registry
from shader_source_recovery_fixture import Fixture, CASES

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
f = Fixture(device)
entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
events = []
try:
    for case in CASES:
        f.configure(case)
        baseline = None
        for broken in (False, True, False):
            f.parm.set("unavailable_layer" if broken else f.good)
            frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
            status = entry.session.status()
            pixels = f.check(frame, status, broken, baseline if not broken else None)
            if baseline is None:
                baseline = pixels
            conversions = status["geometry_conversions"]
            entry.session.wait(entry.apply(dict(camera_path=f.rop.evalParm("camera")), hou.time()))
            assert entry.session.status()["geometry_conversions"] == conversions
            assert entry.session.status()["source_diagnostics"] == status["source_diagnostics"]
            events.append(dict(case=case, broken=broken, diagnostics=status["source_diagnostics"]))
    # Pointiness is a supported intrinsic; authored attribute inputs remain independent.
    f.configure("attribute")
    geometry = f.material.createNode("cycles::geometry::1", "geometry_inputs")
    for socket in ("pointiness",):
        f.surface.setNamedInput("color", geometry, socket)
        frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
        import numpy as np

        pixels = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(64, 128, 4)
        np.testing.assert_allclose(pixels[32, 96, :3], (0.5, 0.5, 0.5), atol=1e-5, rtol=0)
        np.testing.assert_allclose(pixels[32, 32, :3], (0.2, 0.6, 0.3), atol=1e-5, rtol=0)
        assert not entry.session.status()["source_diagnostics"], entry.session.status()
        f.surface.setNamedInput("color", f.source, "color")
        f.check(entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time())), entry.session.status(), False)
        events.append(dict(check="Supported Geometry " + socket + " and attribute shader changes"))
    # Direct graph root uses the same Geometry node's valid Normal output as displacement.
    f.surface.setNamedInput("color", geometry, "pointiness")
    f.material.node("surface_output").setNamedInput("displacement", geometry, "normal")
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
    import numpy as np

    position = np.frombuffer(frame["planes"]["position"]["pixels"], np.float32).reshape(64, 128, 3)
    assert abs(position[32, 96, 2] - 1) < 1e-5, position[32, 96]
    assert not entry.session.status()["source_diagnostics"], entry.session.status()
    events.append(
        dict(check="Pointiness retains independent displacement from another output of the same Geometry node")
    )
    f.configure("attribute")
    # Disconnected unsupported attribute demands are irrelevant.
    unused = f.material.createNode("cycles::attribute::1", "unused")
    unused.parm("attribute").set("irrelevant")
    f.check(entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time())), entry.session.status(), False)
    # Ordered output includes failures and repairs, retaining warnings as metadata.
    f.configure("shared_height")
    for n, broken in enumerate((False, True, False, True, False), 1):
        hou.setFrame(n)
        f.parm.set("unavailable_layer" if broken else f.good)
        entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
        path = f.dest / f"recovery.{n:04d}.exr"
        entry.session.write_exr(path)
        reader = oiio.ImageInput.open(str(path))
        assert reader
        warnings = json.loads(reader.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        reader.close()
        assert bool(warnings) == broken
    with ch.Session(
        device=device,
        width=128,
        height=64,
        samples=32,
        execution="sync",
        interactive=False,
        passes=["combined", "position", "normal"],
    ) as final:
        for case in CASES:
            f.configure(case)
            for broken in (True, False):
                f.parm.set("unavailable_layer" if broken else f.good)
                frame = final.wait(final.apply(native_scene.capture(f.rop), at_time=hou.time()))
                f.check(frame, final.status(), broken)
    f.configure("attribute")
    hou.hipFile.save(str(f.dest / "shader-source-recovery.hiplc"))
finally:
    registry.close_all()
report = dict(result="passed", device=device, events=events, sequence_frames=5)
path = artifacts_root() / "stage3/materials/recovery/houdini-shader-source-recovery-smoke.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(report, indent=2))
print(json.dumps(report))
