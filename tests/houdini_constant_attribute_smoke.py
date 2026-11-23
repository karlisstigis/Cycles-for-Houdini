# SPDX-License-Identifier: Apache-2.0
"""Packed/unpacked scalar and vector float hashes, interpolation, motion and live repairs."""

from subdivision_test_setup import configure_render, set_subdivision
import argparse, json, traceback
import hou, numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import final_render
from constant_attribute_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
f = Fixture(device)
report = dict(result="running", device=device, checks=[])
try:
    with ch.Session(
        device=device,
        width=f.width,
        height=f.height,
        samples=1,
        execution="worker",
        threads=2,
        use_texture_cache=False,
        collect_diagnostics=True,
    ) as s:

        def render(label, reference=None, uniform=True, warning=False, check=True):
            frame = s.wait(s.apply(f.update(), reuse_if_unchanged=True))
            status = s.status()
            assert (
                status["state"] == "complete" and not status["error"] and bool(status["source_diagnostics"]) == warning
            ), status
            values = f.groups(frame, uniform, reference, warning) if check else []
            report["checks"].append(dict(label=label, values=values, status=status))
            return frame, values

        for field in ("rand", "counter", "seed2", "seed3"):
            for mode in ("raw", "noise"):
                f.shade(mode, field)
                f.copies.parm("pack").set(1)
                _, reference = render("Packed " + field + " " + mode)
                if field == "rand" and mode == "noise":
                    uniform_color = reference[0]
                f.copies.parm("pack").set(0)
                render("Unpacked " + field + " " + mode, reference)
        f.shade("math")
        f.copies.parm("pack").set(1)
        _, reference = render("Packed math")
        f.copies.parm("pack").set(0)
        render("Unpacked math", reference)
        f.shade()
        _, reference = render("Unpacked hash before subdivision")
        for scheme in ("linear", "catmull_clark"):
            configure_render(f.rop, dict(subdivision=dict(type=scheme, space="object", dicing_rate=0.1, max_level=2)))
            render("Uniform field through subdivision " + scheme, reference)
        set_subdivision(f.rop, type="none")
        for owner in ("primitive", "vertex"):
            f.fields.parm("class").set(f.fields.parm("class").menuItems().index(owner))
            f.fields.parm("snippet").set("f@face_seed=.1234567;")
            f.shade(field="face_seed")
            render("Uniform " + owner + " hash", reference=[uniform_color] * 4)
        f.fields.parm("class").set(f.fields.parm("class").menuItems().index("point"))
        f.fields.parm("snippet").set("f@rand=.4+.07*@P.x+.11*@P.y;")
        f.shade("raw")
        frame, _ = render("Varying fields retain interpolation", uniform=False, check=False)
        f.varying(frame)
        f.fields.parm("snippet").set("v@seed3=set(.4+.07*@P.x+.11*@P.y,.7182818,.3141592);")
        f.shade(field="seed3")
        split = f.material.createNode("cycles::separate_xyz::1", "constant_vector_component")
        split.setNamedInput("vector", f.rand, "vector")
        f.scale.setNamedInput("value1", split, "y")
        f.noise.parm("dimensions").set("1D")
        _, values = render("Constant component of a varying vector hashes uniformly")
        np.testing.assert_allclose(values, np.broadcast_to(values[0], (4, 3)), rtol=0, atol=0)
        f.fields.parm("snippet").set("")
        f.scale.setNamedInput("value1", f.rand, "fac")
        f.shade("bump")
        f.bump.parm("strength").set(0)
        frame, _ = render("Unperturbed normal reference")
        baseline = frame["planes"]["combined"]["pixels"]
        f.bump.parm("strength").set(1)
        frame, _ = render("Constant hash has no bump derivatives")
        np.testing.assert_allclose(
            np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32),
            np.frombuffer(baseline, np.float32),
            rtol=0,
            atol=2e-6,
        )
        f.shade()
        f.shape.parm("input").set(2)
        f.copies.parm("pack").set(1)
        _, hair = render("Packed uniform hair hash")
        f.copies.parm("pack").set(0)
        render("Unpacked uniform hair hash", hair)
        f.shape.parm("input").set(0)
        for packed in (0, 1):
            f.copies.parm("pack").set(packed)
            for offset in (0.037, 0.071, 0):
                f.configure(offset=offset)
                render("Live hash value edit " + str((packed, offset)))
            f.configure(broken=True)
            render("Broken field remains visible " + str(packed), warning=True)
            f.configure()
            render("Field repair restores uniform hash " + str(packed))
        f.copies.parm("pack").set(0)
        f.prototype_fields.parm("snippet").set("@P.z=.1*@Time;")
        configure_render(f.rop, dict(allowmotionblur=1, geometry_motion="deformation", motion_samples=3))
        render("Constant field over deforming shutter samples")
        configure_render(f.rop, dict(allowmotionblur=0))
        f.prototype_fields.parm("snippet").set("")
        f.camera.parm("tx").set(0.05)
        render("Camera move preserves uniform copies")
        f.obj.parm("tx").set(0.03)
        render("Object move preserves uniform copies")
        s.close()
        assert s.status()["device_allocation_bytes"] == s.status()["owned_geometry_bytes"] == 0
        report["closed"] = s.status()
    f.camera.parm("tx").set(0)
    f.obj.parm("tx").set(0)
    f.configure(animation=True)
    f.rop.parm("picture").set(str(f.dest / "constant-attributes.$F4.exr"))
    configure_render(f.rop, dict(persistent_data=1))
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1, summary
    frames = []
    for number in range(1, 6):
        reader = oiio.ImageInput.open(str(f.dest / f"constant-attributes.{number:04d}.exr"))
        assert reader
        spec = reader.spec()
        assert not json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        data = np.asarray(reader.read_image(format=oiio.FLOAT))
        reader.close()
        assert np.isfinite(data).all()
        names = list(spec.channelnames)
        combined = (
            np.stack([data[:, :, names.index("Combined." + c)] for c in "RGBA"], axis=2)
            if "Combined.R" in names
            else data[:, :, :4]
        )
        opaque = combined[:, :, 3] > 0.999
        colors = np.unique(combined[:, :, :3][opaque], axis=0)
        assert len(colors) == 4, (number, colors)
        frames.append(colors.tolist())
    assert all(frames[i] != frames[i + 1] for i in range(4))
    report.update(result="passed", batch_frames=5, batch_summary=summary, batch_colors=frames, scope=__doc__)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    (f.dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]), batch_frames=report.get("batch_frames"))))
