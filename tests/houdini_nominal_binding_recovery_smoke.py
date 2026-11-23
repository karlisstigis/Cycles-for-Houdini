# SPDX-License-Identifier: Apache-2.0
"""Sampled shader inputs and packed transforms retain supported basis conversions."""

from support.paths import artifacts_root
import argparse, json, os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene, registry
from displacement_motion_recovery_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
f = Fixture(device)
f.rop.setParms(dict(samples=1024, ipr_samples=1024, adaptive_sampling=0, ipr_adaptive_sampling=0))
entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
checks = []


def apply():
    return entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))


def plane(frame, name):
    p = frame["planes"][name]
    return np.frombuffer(p["pixels"], np.float32).reshape(128, 192, p["channels"])


def compare_identity(frame, disconnect, restore):
    # Removing only an identity node is an independent graph reference. Both
    # captures retain changing fields, geometry, and complete instance motion.
    disconnect()
    try:
        update = native_scene.capture(f.rop)
    finally:
        restore()
    # Four overlapping moving instances need enough samples for this unchanged
    # full-image budget; single-ray position passes are compared statistically.
    with ch.Session(
        device=device,
        width=192,
        height=128,
        samples=1024,
        interactive=False,
        execution="sync",
        passes=["combined", "position"],
    ) as reference:
        expected = reference.wait(reference.apply(update, at_time=hou.time()))
        assert not reference.status()["source_diagnostics"], reference.status()
        error = float(np.abs(plane(frame, "combined") - plane(expected, "combined")).mean())
        if error >= 0.001:
            np.savez(
                f.dest / "identity-reference-probe.npz",
                actual=plane(frame, "combined"),
                expected=plane(expected, "combined"),
            )
            raise AssertionError(("Identity conversion changed the sampled image", error))
        actual = plane(frame, "position")
        target = plane(expected, "position")
        hits = (actual[:, :, 2] > 0.1) & (target[:, :, 2] > 0.1)
        assert hits.sum() > 100
        delta = actual[hits] - target[hits]
        bound = 0.0005 + 5 * delta.std(axis=0) / np.sqrt(hits.sum())
        assert np.all(np.abs(delta.mean(axis=0)) < bound), (delta.mean(axis=0), bound)
        return error


try:
    # Animated UV/Cd/N must retain their full shutter history through an identity
    # Vector Transform, rather than silently falling back to a frozen pose.
    f.configure("deformation")
    f.rop.parm("shutteroffset").set(0.75)
    authored = f.obj.createNode("attribwrangle", "animated_nominal_inputs")
    authored.setInput(0, f.sop)
    authored.parm("snippet").set(
        "v@uv=set(0.25+@Time*12,0,0); v@Cd=set(0.2+@Time*6,0.6,0.3); v@N=normalize(set(@Time*12,0,1));"
    )
    authored.setDisplayFlag(True)
    authored.setRenderFlag(True)
    uv = f.material.createNode("cycles::texture_coordinate::1", "nominal_uv")
    split = f.material.createNode("cycles::separate_xyz::1", "nominal_u")
    split.setNamedInput("vector", uv, "UV")
    f.pending_basis.setNamedInput("vector", uv, "UV")
    split.setNamedInput("vector", f.pending_basis, "vector")
    f.scalar.setNamedInput("height", split, "x")
    color = f.material.createNode("cycles::attribute::1", "nominal_color")
    color.parm("attribute").set("Cd")
    f.source.setNamedInput("color", color, "color")
    # Exclude the static shared material object: its absent UV/Cd is covered by
    # the independent attribute-recovery suite rather than this pose check.
    f.rop.parm("vobject").set(f.obj.path() + " " + f.other.path())
    for broken in (False, True, False, True, False):
        f.edit(broken)
        frame = apply()
        status = entry.session.status()
        assert not status["error"] and status["geometry_resources"] == 2, status
        assert not status["source_diagnostics"] and not status["motion_warnings"], status
        if broken:
            error = compare_identity(
                frame,
                lambda: split.setNamedInput("vector", uv, "UV"),
                lambda: split.setNamedInput("vector", f.pending_basis, "vector"),
            )
            checks.append(
                dict(
                    label="Sampled UV/Cd/N with a supported identity basis under an offset shutter",
                    mean_difference=error,
                    status=status,
                )
            )
    f.source.setInput(f.source.inputIndex("color"), None)
    authored.destroy()
    f.sop.setDisplayFlag(True)
    f.sop.setRenderFlag(True)
    # Packed instances have independently sampled local and OBJ transforms.
    # Both direct and identity-basis graphs must retain local and OBJ motion.
    f.configure("transform")
    f.rop.parm("geometry_motion").set("transform")
    targets = f.obj.createNode("grid", "packed_targets")
    targets.setParms(dict(type=0, orient=0, rows=2, cols=2))
    targets.parmTuple("size").set((1.0, 1.0))
    move = f.obj.createNode("xform", "moving_targets")
    move.setInput(0, targets)
    move.parm("tx").setExpression("$T*48", hou.exprLanguage.Hscript)
    copies = f.obj.createNode("copytopoints", "nominal_instances")
    copies.setInput(0, f.sop)
    copies.setInput(1, move)
    copies.parm("pack").set(1)
    copies.setDisplayFlag(True)
    copies.setRenderFlag(True)
    for offset in (-1.0, 0.75):
        f.rop.parm("shutteroffset").set(offset)
        baseline = None
        for broken in (False, True, False, True, False, True, False):
            f.edit(broken)
            frame = apply()
            status = entry.session.status()
            rgb = plane(frame, "combined")
            assert status["geometry_resources"] == 2 and status["object_resources"] == 5, status
            assert not status["source_diagnostics"] and not status["motion_warnings"], status
            if broken:
                error = compare_identity(
                    frame,
                    lambda: f.attribute_vector.setNamedInput("vector1", f.attribute, "color"),
                    lambda: f.attribute_vector.setNamedInput("vector1", f.pending_basis, "vector"),
                )
                # World-dependent displacement keeps four explicit transform
                # variants. The canonical packed prototype remains shared.
                assert status["mesh_variant_resources"] == 5, status
                checks.append(
                    dict(
                        label="Supported packed basis conversion with sampled local/OBJ transforms",
                        offset=offset,
                        mean_difference=error,
                        status=status,
                    )
                )
            else:
                assert not status["source_diagnostics"], status
                assert status["mesh_variant_resources"] == 2, status
                if baseline is not None:
                    if not np.allclose(rgb, baseline, atol=1e-5):
                        np.savez(f.dest / "packed-repair-probe.npz", before=baseline, after=rgb)
                    assert np.allclose(rgb, baseline, atol=1e-5), (
                        "Packed shader repair changed the original blurred image",
                        offset,
                        float(np.mean(np.abs(rgb - baseline))),
                        float(np.max(np.abs(rgb - baseline))),
                    )
                baseline = rgb.copy()
finally:
    registry.close_all()
report = dict(result="passed", device=device, checks=checks)
path = artifacts_root() / "stage3/materials/recovery/houdini-nominal-binding-recovery-smoke.json"
path.write_text(json.dumps(report, indent=2))
print(json.dumps(report))
