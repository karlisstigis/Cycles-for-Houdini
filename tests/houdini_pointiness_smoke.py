# SPDX-License-Identifier: Apache-2.0
"""Independent original-polygon Pointiness images, demand, displacement, motion and recovery."""

from subdivision_test_setup import configure_render, set_subdivision
import pass_fixtures as pass_fixture

import argparse, hashlib, json, traceback
import hou, numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import final_render
from pointiness_fixture import Fixture

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
        samples=4,
        execution="worker",
        threads=2,
        use_texture_cache=False,
        collect_diagnostics=True,
    ) as s:

        def render(label, expected=None, warning=False, demand=None):
            frame = s.wait(s.apply(f.update(), reuse_if_unchanged=True))
            status = s.status()
            owned = f.owned(s)
            image = f.planes(frame)["combined"]
            assert (
                status["state"] == "complete" and not status["error"] and bool(status["source_diagnostics"]) == warning
            ), status
            assert np.isfinite(image).all() and (image[:, :, 3] > 0.999).sum() > 40
            if expected is not None:
                f.gray(frame, expected, magenta=warning)
                if not warning:
                    values = image[image[:, :, 3] > 0.999, :3]
                    np.testing.assert_allclose(values, np.full_like(values, expected), rtol=0, atol=3e-5)
            if demand is not None:
                assert owned["pointiness_vertices"] == demand, (label, owned)
            report["checks"].append(dict(label=label, status=status, owned=owned))
            return frame

        def reference(label):
            f.shade()
            a = f.planes(render(label + " intrinsic", demand=len(f.positions)))
            f.shade("reference")
            b = f.planes(render(label + " independent NumPy reference", demand=0))
            np.testing.assert_allclose(a["combined"], b["combined"], rtol=0, atol=2e-6)
            f.shade()
            return a

        for kind, value, options in (("plane", 0.5, {}), ("octa", 0.75, {}), ("octa", 0.25, dict(reverse=True))):
            f.set_shape(kind, **options)
            render("Analytic " + kind + str(options), value, demand=len(f.positions))
        for options in (
            dict(deform=0.25),
            dict(deform=-0.35),
            dict(deform=0.25, reverse=True),
            dict(deform=0.25, duplicate=True),
        ):
            f.set_shape("plane", **options)
            reference("Original quads " + str(options))
        for normal, warning in (
            (None, False),
            ("inverted", False),
            ("bad_type", True),
            ("bad_value", True),
            (None, False),
        ):
            f.set_shape(normals=normal)
            render("Geometric curvature ignores authored N " + str(normal), 0.75, warning, demand=6)
        f.set_shape(user_value=0.37)
        f.shade("names")
        frame = render("Intrinsic and ordinary pointiness field coexist", demand=6)
        image = f.planes(frame)["combined"]
        values = image[image[:, :, 3] > 0.999, :3]
        np.testing.assert_allclose(values, np.broadcast_to((0.75, 0.37, 0.2), values.shape), rtol=0, atol=3e-5)
        f.set_shape(user_value=float("nan"))
        f.shade()
        render("Unused malformed pointiness attribute is quiet", 0.75, demand=6)
        f.set_shape()
        pass_fixture.parm(f.output, "input").set(1)
        for mode, count, value in (("pointiness", 6, 0.75), ("constant", 0, 0.3), ("pointiness", 6, 0.75)):
            f.shade(mode)
            render("Packed prototype demand " + mode, value, demand=count)
            assert s.status()["geometry_resources"] == 1 and s.status()["object_resources"] == 4, s.status()
        warm = []
        for cycle in range(12):
            active = cycle % 2 == 0
            f.shade("pointiness" if active else "constant")
            render("Repeated demand retirement " + str(cycle), 0.75 if active else 0.3, demand=6 if active else 0)
            status = s.status()
            warm.append(
                (status["owned_geometry_bytes"], status["device_allocation_bytes"], status["engine_shader_resources"])
            )
        allocation_bounds = []
        for parity in (0, 1):
            states = warm[parity::2]
            limit = max(value[1] for value in states[:2])
            # Host geometry/shader ownership must be identical. Device buffers
            # may retire after a complete image; shrinking is valid, growth
            # beyond the warmed allocation limit is not.
            assert len({(value[0], value[2]) for value in states[2:]}) == 1, warm
            assert all(value[1] <= limit for value in states[2:]), warm
            allocation_bounds.append(
                dict(pointiness=parity == 0, warmed_limit=limit, measured=[value[1] for value in states[2:]])
            )
        report["warm_resources"] = warm
        report["warm_allocation_bounds"] = allocation_bounds
        pass_fixture.parm(f.output, "input").set(0)
        f.shade("constant")
        writer = f.mat.createNode("cycles::aov_output::1", "curvature_aov")
        pass_fixture.parm(writer, "name").set("Curvature")
        writer.setNamedInput("value", f.geometry, "pointiness")
        render("Unregistered Pointiness AOV stays inactive", 0.3, demand=0)
        pass_fixture.parm(f.rop, "aov_count").set(1)
        configure_render(f.rop, dict(aov_name1="Curvature", aov_type1="value"))
        frame = render("AOV-only Pointiness demand", 0.3, demand=6)
        planes = f.planes(frame)
        mask = planes["combined"][:, :, 3] > 0.999
        np.testing.assert_allclose(planes["Curvature"][mask], 0.75, rtol=0, atol=3e-5)
        pass_fixture.parm(f.rop, "aov_count").set(0)
        render("Removing AOV releases intrinsic demand", 0.3, demand=0)
        writer.destroy()
        pass_fixture.parm(f.output, "input").set(2)
        f.shade()
        frame = render("Mesh survives unsupported Pointiness on hair and points", 0.75, True, demand=6)
        image = f.planes(frame)["combined"]
        assert np.any(np.all(np.isclose(image[:, :, :3], (1, 0, 1), atol=1e-5), axis=2))
        assert all("Pointiness" in d["message"] for d in s.status()["source_diagnostics"]), s.status()[
            "source_diagnostics"
        ]
        f.shade("constant")
        render("Repair mixed components by changing the shader", 0.3, demand=0)
        assert s.status()["geometry_resources"] == 3, s.status()
        pass_fixture.parm(f.output, "input").set(0)
        f.set_shape(normals="radial", seams=True, user_value=0.37)
        f.shade()
        f.displace()
        displaced = f.planes(render("Pointiness drives true displacement across seams", 0.75, demand=6))
        f.shade("user")
        f.set_shape(normals="radial", seams=True, user_value=float("nan"))
        broken = f.planes(
            render("Broken named color preserves independent intrinsic displacement", warning=True, demand=6)
        )
        np.testing.assert_allclose(broken["position"], displaced["position"], rtol=0, atol=2e-6)
        f.set_shape(normals="radial", seams=True, user_value=0.37)
        f.shade()
        repaired = f.planes(render("Named color repair retains intrinsic displacement", 0.75, demand=6))
        np.testing.assert_allclose(repaired["position"], displaced["position"], rtol=0, atol=2e-6)
        f.displace(False)
        f.shade("constant")
        geo = hou.Geometry()
        geo.merge(f.stash.geometry())
        for p in geo.points():
            p.setPosition(p.position() * 1.05)
        pass_fixture.parm(f.stash, "stash").set(geo)
        expanded = f.planes(render("Independent explicitly expanded displacement reference", 0.3, demand=0))
        np.testing.assert_allclose(displaced["position"], expanded["position"], rtol=0, atol=3e-6)
        for scheme in ("linear", "catmull_clark"):
            f.set_shape()
            f.shade()
            configure_render(f.rop, dict(subdivision=dict(type=scheme, space="object", dicing_rate=0.12, max_level=2)))
            render("Intrinsic precedes subdivision " + scheme, 0.75, demand=6)
        set_subdivision(f.rop, type="none")
        f.set_shape("plane")
        pass_fixture.parm(f.wave, "snippet").set("@P.z=.25*sin(@Time*5)*(@P.x*@P.x-.7*@P.y*@P.y+.2*@P.x*@P.y);")
        pass_fixture.parm(f.camera, "shutter").set(0.5)
        configure_render(f.rop, dict(allowmotionblur=1, geometry_motion="deformation", motion_samples=3))
        hou.setFrame(4)
        f.refresh_reference()
        for packed in (0, 1):
            pass_fixture.parm(f.output, "input").set(packed)
            for offset in (0, -0.5, 0.5):
                pass_fixture.parm(f.rop, "shutteroffset").set(offset)
                reference("Requested-frame curvature over moving geometry " + str((packed, offset)))
        configure_render(f.rop, dict(allowmotionblur=0, shutteroffset=0))
        pass_fixture.parm(f.wave, "snippet").set("")
        pass_fixture.parm(f.output, "input").set(0)
        f.set_shape()
        f.shade()
        render("Restore static mesh", 0.75, demand=6)
        conversions = s.status()["geometry_conversions"]
        for label, edit in (
            ("Camera move", lambda: pass_fixture.parm(f.camera, "tx").set(0.05)),
            ("Object move", lambda: pass_fixture.parm(f.obj, "tx").set(0.03)),
        ):
            edit()
            render(label, 0.75, demand=6)
            assert s.status()["geometry_conversions"] == conversions, s.status()
        s.close()
        assert s.status()["device_allocation_bytes"] == s.status()["owned_geometry_bytes"] == 0
        report["closed"] = s.status()
    pass_fixture.parm(f.camera, "tx").set(0)
    pass_fixture.parm(f.obj, "tx").set(0)
    hou.setFrame(1)
    f.set_shape()
    pass_fixture.parm(f.wave, "snippet").set(
        '@P*=1+.03*@Frame;v@N=normalize(@P);if(@Frame==3 && @ptnum==0)v@N.x=atof("nan");'
    )
    pass_fixture.parm(f.rop, "picture").set(str(f.dest / "pointiness.$F4.exr"))
    configure_render(f.rop, dict(persistent_data=1))
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    hashes = []
    for number in range(1, 6):
        reader = oiio.ImageInput.open(str(f.dest / f"pointiness.{number:04d}.exr"))
        assert reader
        spec = reader.spec()
        issues = json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        data = np.asarray(reader.read_image(format=oiio.FLOAT))
        reader.close()
        assert bool(issues) == (number == 3), (number, issues)
        assert np.isfinite(data).all()
        names = list(spec.channelnames)
        combined = np.stack([data[:, :, names.index(c)] for c in "RGBA"], axis=2)
        visible = combined[:, :, 3] > 0.999
        np.testing.assert_allclose(combined[:, :, :3][visible], 0.75, rtol=0, atol=3e-5)
        hashes.append(hashlib.sha256(combined.tobytes()).hexdigest())
    assert len(set(hashes)) == 5, hashes
    report.update(result="passed", batch_frames=5, batch_hashes=hashes, batch_summary=summary, scope=__doc__)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    (f.dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]), batch_frames=report.get("batch_frames"))))
