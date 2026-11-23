# SPDX-License-Identifier: Apache-2.0
from support.paths import artifacts_root
import argparse, json, os
from pathlib import Path
import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene, registry
from displacement_motion_recovery_fixture import Fixture, CASES

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
f = Fixture(device)
entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
events = []


def reference_update(case):
    if case == "transform":
        # Remove the mathematically identity conversion, retaining all motion.
        f.attribute_vector.setNamedInput("vector1", f.attribute, "color")
    else:
        # A separately authored constant valid UV is the exact held-field
        # reference here. Keep every geometry/object motion sample enabled.
        f.recovery_switch.parm("input").deleteAllKeyframes()
        f.recovery_switch.parm("input").set(0)
    return native_scene.capture(f.rop)


try:
    for case in CASES:
        f.configure(case)
        baseline = None
        for broken in (False, True, False):
            f.edit(broken)
            frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
            status = entry.session.status()
            pixels = f.check(frame, status, broken, baseline)
            if baseline is None:
                baseline = pixels
            conversions = status["geometry_conversions"]
            camera = hou.node(f.rop.evalParm("camera"))
            camera.parm("tx").set(0.1)
            entry.session.wait(entry.apply(dict(camera_path=f.rop.evalParm("camera")), hou.time()))
            assert entry.session.status()["geometry_conversions"] == conversions
            assert entry.session.status()["source_diagnostics"] == status["source_diagnostics"]
            camera.parm("tx").set(0)
            entry.session.wait(entry.apply(dict(camera_path=camera.path()), hou.time()))
            assert entry.session.status()["geometry_conversions"] == conversions
            events.append(dict(case=case, broken=broken, diagnostics=status["source_diagnostics"]))
        # A valid held coordinate or bypassed identity reference retains motion.
        f.edit(True)
        recovered = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
        update = reference_update(case)
        with ch.Session(
            device=device,
            width=192,
            height=128,
            samples=128,
            execution="sync",
            interactive=False,
            passes=["combined", "position"],
        ) as reference:
            clean = reference.wait(reference.apply(update, at_time=hou.time()))
            assert not reference.status()["source_diagnostics"], reference.status()
            # Compare independently captured beauty statistically and depth
            # exactly; temporal rays can differ between independent sessions.
            a = np.frombuffer(recovered["planes"]["combined"]["pixels"], np.float32).reshape(128, 192, 4)
            b = np.frombuffer(clean["planes"]["combined"]["pixels"], np.float32).reshape(128, 192, 4)
            assert np.mean(np.abs(a - b)) < 0.001 and abs(a.sum() - b.sum()) < b.sum() * 0.02, (
                case,
                "reference beauty differs",
            )
            a = np.frombuffer(recovered["planes"]["position"]["pixels"], np.float32).reshape(128, 192, 3)
            b = np.frombuffer(clean["planes"]["position"]["pixels"], np.float32).reshape(128, 192, 3)
            # The unfiltered position pass can hit or miss the moving plane at
            # different times in independent sessions. Inspect common hits over
            # the affected object, not a tiny fully covered static-pose window.
            actual = a[54:74, :110]
            expected = b[54:74, :110]
            interior = (actual[:, :, 2] > 0.1) & (expected[:, :, 2] > 0.1)
            assert interior.sum() > 100, (case, "Not enough common reference hits", int(interior.sum()))
            delta = actual[interior] - expected[interior]
            # Retained motion samples subpixel rays independently in these two
            # sessions. Depth is constant on this plane; XY must agree within
            # a measured sampling bound, rather than bitwise jitter equality.
            error = np.abs(delta.mean(axis=0))
            bound = 0.0005 + 5 * delta.std(axis=0) / np.sqrt(interior.sum())
            assert np.all(error < bound) and np.allclose(delta[:, 2], 0, atol=1e-5), (
                case,
                "reference interior position differs",
                error,
                bound,
            )
    # Geometry and camera have equal translation. Holding a broken UV field
    # must preserve their cancellation rather than invent camera-only blur.
    f.configure("combined")
    f.edit(True)
    camera = hou.node(f.rop.evalParm("camera"))
    camera.parm("tx").setExpression("$T*48", hou.exprLanguage.Hscript)
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
    p = frame["planes"]["combined"]
    rgb = np.frombuffer(p["pixels"], np.float32).reshape(128, 192, 4)
    assert 18 <= (rgb[64, :110, 1] > 0.005).sum() <= 21, "Valid geometry and camera motion did not cancel"
    assert not any(d["code"].startswith("motion.displacement_") for d in entry.session.status()["source_diagnostics"])
    camera.parm("tx").deleteAllKeyframes()
    camera.parm("tx").set(0)
    # Turn blur off/on while a fallback is active, preserving owned samples.
    f.configure("combined")
    f.edit(True)
    entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
    entry.session.wait(entry.apply(dict(motion_blur=False), hou.time()))
    assert not entry.session.status()["source_diagnostics"]
    frame = entry.session.wait(entry.apply(dict(motion_blur=True), hou.time()))
    f.check(frame, entry.session.status(), True)
    f.edit(False)
    frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
    f.check(frame, entry.session.status(), False)
    # Offset shutters preserve supported basis conversions and held UV fields.
    for offset in (-1.0, 0.75):
        f.rop.parm("shutteroffset").set(offset)
        for case in CASES:
            f.configure(case)
            baseline = None
            for broken in (False, True, False):
                f.edit(broken)
                frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
                pixels = f.check(frame, entry.session.status(), broken, baseline)
                if broken:
                    assert all(
                        "requested-frame" in d["action"]
                        for d in entry.session.status()["source_diagnostics"]
                        if d["code"].startswith("motion.")
                    )
                if baseline is None:
                    baseline = pixels
                events.append(
                    dict(
                        case=case,
                        broken=broken,
                        offset=offset,
                        diagnostics=entry.session.status()["source_diagnostics"],
                    )
                )
            f.edit(True)
            recovered = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
            update = reference_update(case)
            with ch.Session(
                device=device,
                width=192,
                height=128,
                samples=128,
                execution="sync",
                interactive=False,
                passes=["combined", "position"],
            ) as reference:
                clean = reference.wait(reference.apply(update, at_time=hou.time()))
                assert not reference.status()["source_diagnostics"], reference.status()
                a = np.frombuffer(recovered["planes"]["combined"]["pixels"], np.float32)
                b = np.frombuffer(clean["planes"]["combined"]["pixels"], np.float32)
                assert np.mean(np.abs(a - b)) < 0.001 and abs(a.sum() - b.sum()) < b.sum() * 0.02, (
                    case,
                    offset,
                    "nominal reference differs",
                )
    f.rop.parm("shutteroffset").set(0)
    # Shader-only basis edits retain the nominal pose and shutter samples. Check
    # bounded host/device allocations after warm-up without geometry recapture.
    f.rop.parm("shutteroffset").set(0.75)
    f.configure("combined")
    f.shader_only_recovery()
    entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
    conversions = entry.session.status()["geometry_conversions"]
    warm = []
    for iteration in range(20):
        broken = bool(iteration % 2)
        f.edit(broken)
        frame = entry.session.wait(entry.apply(dict(material_resolver=native_scene.resolve_materials), hou.time()))
        status = entry.session.status()
        f.check(frame, status, broken)
        assert status["geometry_conversions"] == conversions and status["motion_cooks"] == 0, status
        if iteration >= 8:
            warm.append((broken, status["owned_geometry_bytes"], status["device_allocation_bytes"]))
    for phase in (False, True):
        values = [v for v in warm if v[0] == phase]
        assert len({v[1] for v in values}) == 1, warm
        # Engine allocations can retire between status reads. A smaller sample
        # is fine; repeated edits must not raise the warmed allocation ceiling.
        assert max(v[2] for v in values[3:]) <= max(v[2] for v in values[:3]), warm
    events.append(
        dict(check="20 supported shader-only basis edits retain shutter poses and bounded allocations", warm=warm)
    )
    f.rop.parm("shutteroffset").set(0)
    # Final uses the same recovery policy. Keep the nominal frame fixed for the
    # analytic coverage checks, then write a genuine five-frame broken/repair sequence.
    with ch.Session(
        device=device,
        width=192,
        height=128,
        samples=128,
        execution="sync",
        interactive=False,
        passes=["combined", "position"],
    ) as final:
        for offset in (0.0, -1.0, 0.75):
            f.rop.parm("shutteroffset").set(offset)
            for case in CASES:
                f.configure(case)
                for broken in (True, False):
                    f.edit(broken)
                    frame = final.wait(final.apply(native_scene.capture(f.rop), at_time=hou.time()))
                    f.check(frame, final.status(), broken)
    f.configure("combined")
    f.shader_only_recovery()
    for n, broken in enumerate((False, True, False, True, False), 1):
        hou.setFrame(n)
        f.edit(True)
        f.attribute.parm("attribute").set("missing_height" if broken else "Cd")
        frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
        assert (
            entry.session.status()["object_resources"] == 3
            and np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).max() > 0.1
        )
        path = f.dest / f"recovery.{n:04d}.exr"
        entry.session.write_exr(path)
        reader = oiio.ImageInput.open(str(path))
        assert reader
        warnings = json.loads(reader.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        reader.close()
        assert bool(warnings) == broken
    # Exercise Houdini's real ordered ROP/SOHO batch path as well as the worker
    # output service. Missing displacement fields recover on odd frames; valid
    # Cd is restored on even frames. Every frame carries its own diagnostics.
    hou.setFrame(1)
    f.configure("combined")
    f.edit(True)
    f.shader_only_recovery()
    f.attribute.parm("attribute").setExpression(
        "'missing_height' if int(hou.frame()+.5)%2 else 'Cd'", hou.exprLanguage.Python
    )
    f.rop.parm("picture").set(str(f.dest / "native.$F4.exr"))
    f.rop.render(frame_range=(1, 5, 1))
    for n in range(1, 6):
        reader = oiio.ImageInput.open(str(f.dest / f"native.{n:04d}.exr"))
        assert reader
        warnings = json.loads(reader.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        reader.close()
        assert bool(warnings) == bool(n % 2), (n, warnings)
    f.attribute.parm("attribute").deleteAllKeyframes()
    f.attribute.parm("attribute").set("Cd")
    hou.setFrame(1)
    f.configure("combined")
    hou.hipFile.save(str(f.dest / "displacement-motion-recovery.hiplc"))
finally:
    registry.close_all()
report = dict(
    result="passed", device=device, events=events, sequence_frames=5, native_batch_frames=5, final_recovery_states=24
)
path = artifacts_root() / "stage3/materials/recovery/houdini-displacement-motion-recovery-smoke.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(report, indent=2))
print(json.dumps(report))
