# SPDX-License-Identifier: Apache-2.0
"""Per-island mesh shading: topology, displacement seams, packed sharing and repair."""

from subdivision_test_setup import configure_render, set_subdivision
import argparse, json, os, traceback
from pathlib import Path
import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene, final_render
from mesh_island_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
f = Fixture(device)
report = dict(result="running", device=device, checks=[])
session = ch.Session(
    device=device, width=f.width, height=f.height, samples=1, passes=["combined", "position"], collect_diagnostics=True
)


def render(label, **expect):
    frame = session.wait(session.apply(native_scene.capture(f.rop), reuse_if_unchanged=True))
    status = session.status()
    values = f.inspect(frame, status, **expect)
    report["checks"].append(dict(label=label, values=values, status=status))
    return np.array(values)


try:
    baseline = render("Three disconnected mesh pieces")
    # UVs/material assignments do not define mesh connectivity. Nor do face order or positions.
    for options in (
        dict(reverse=True),
        dict(deform=0.025),
        dict(assignments=True),
        dict(seams=True),
        dict(assignments=True, seams=True),
    ):
        f.set_mesh(**options)
        render(str(options), expected=baseline)
    f.set_mesh(joined=True)
    joined = render("Topology joins the first two pieces", joined=True)
    assert abs(joined[0, 2] - baseline[0, 2]) < 5e-5
    f.set_mesh()
    render("Connectivity repair", expected=baseline)
    f.set_mesh(seams=True)
    f.displace()
    render("True displacement splits seams, not islands", expected=baseline, displaced=True)
    f.displace(from_island=True)
    render("Island drives true displacement", expected=baseline, displaced="island")
    f.displace(False)
    f.set_mesh()
    for scheme in ("linear", "catmull_clark"):
        configure_render(f.rop, dict(subdivision=dict(type=scheme, space="object", dicing_rate=0.15, max_level=2)))
        render("Subdivision " + scheme, expected=baseline)
        f.set_mesh(joined=True)
        render("Subdivision connectivity change " + scheme, joined=True)
        f.set_mesh()
        render("Subdivision topology repair " + scheme, expected=baseline)
        for fault in ("winding", "nonmanifold", "repeated_point"):
            f.set_mesh(fault=fault)
            render("Recover subdivision " + scheme + " " + fault, warning=True, expected=baseline)
            assert any(d["code"] == "geometry.subdivision_topology" for d in session.status()["source_diagnostics"]), (
                session.status()
            )
            issues = session.status()["source_diagnostics"]
            conversions = session.status()["geometry_conversions"]
            session.wait(session.apply(dict(camera_path=f.camera.path()), reuse_if_unchanged=True))
            assert (
                session.status()["source_diagnostics"] == issues
                and session.status()["geometry_conversions"] == conversions
            )
            f.set_mesh()
            render("Repair subdivision " + scheme + " " + fault, expected=baseline)
    set_subdivision(f.rop, type="none")
    f.output.parm("input").set(4)
    render("Packed copies retain the prototype islands", packed=True, expected=baseline)
    assert session.status()["geometry_resources"] == 1 and session.status()["object_resources"] == 2, session.status()
    for mode in (1, 2, 3):
        f.output.parm("input").set(mode)
        frame = session.wait(session.apply(native_scene.capture(f.rop), reuse_if_unchanged=True))
        status = session.status()
        assert status["state"] != "failed" and status["source_diagnostics"], status
        if mode == 3:
            f.inspect(frame, status, warning=True, expected=baseline)
        else:
            p = frame["planes"]["combined"]
            image = np.frombuffer(p["pixels"], np.float32).reshape(f.height, f.width, p["channels"])
            visible = image[:, :, 3] > 0.99
            assert visible.sum() > 2
            np.testing.assert_allclose(
                image[visible, :3], np.broadcast_to((1, 0, 1), image[visible, :3].shape), atol=5e-5, rtol=0
            )
        report["checks"].append(dict(label="Recover unsupported component " + str(mode), status=status))
        f.output.parm("input").set(0)
        render("Repair mesh kind " + str(mode), expected=baseline)
    for cycle in range(3):
        f.shade("unsupported")
        frame = session.wait(session.apply(native_scene.capture(f.rop), reuse_if_unchanged=True))
        status = session.status()
        assert status["state"] != "failed" and status["source_diagnostics"], status
        report["checks"].append(dict(label="Recover unrelated unsupported output " + str(cycle), status=status))
        f.shade()
        render("Repair shader " + str(cycle), expected=baseline)
    conversions = session.status()["geometry_conversions"]
    for label, edit in (
        ("Camera move", lambda: f.camera.parm("tx").set(0.02)),
        ("Object move", lambda: f.obj.parm("tx").set(0.02)),
    ):
        edit()
        render(label, expected=baseline)
        assert session.status()["geometry_conversions"] == conversions
    # Actual ordered ROP output, with fallback only on frame 3. One persistent session.
    session.close()
    assert session.status()["device_allocation_bytes"] == session.status()["owned_geometry_bytes"] == 0
    f.camera.parm("tx").set(0)
    f.obj.parm("tx").set(0)
    f.rop.parm("picture").set(str(f.dest / "islands.$F4.exr"))
    f.rop.parm("persistent_data").set(1)
    f.emission.setNamedInput("color", f.geometry, "random_per_island")
    switch = f.material.createNode("cycles::mix_color::1", "animated_source")
    switch.parm("data_type").set("float")
    switch.setNamedInput("a", f.geometry, "random_per_island")
    switch.parm("b_float").set(0.25)
    switch.parm("fac").setExpression("($F-1)/8", hou.exprLanguage.Hscript)
    f.emission.setNamedInput("color", switch, "result")
    bad = ch.create_material(name="frame_three_recovery", shader="emission")
    bad.node("surface_shader").parm("strength").set(1)
    unsupported = bad.createNode("cycles::texture_coordinate::1")
    unsupported.parm("from_dupli").set(1)
    bad.node("surface_shader").setNamedInput("color", unsupported, "generated")
    f.obj.parm("shop_materialpath").setExpression(
        'ifs($F==3,"' + bad.path() + '","' + f.material.path() + '")', hou.exprLanguage.Hscript
    )
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    for number in range(1, 6):
        path = f.dest / f"islands.{number:04}.exr"
        reader = oiio.ImageInput.open(str(path))
        assert reader, path
        spec = reader.spec()
        assert bool(json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")) == (number == 3)
        image = reader.read_image(format=oiio.FLOAT)
        reader.close()
        # Interior of each face, using the established orthographic projection.
        measured = image[f.height // 2, [45, 105, 165], :3]
        expected = (
            np.broadcast_to((1, 0, 1), (3, 3))
            if number == 3
            else np.repeat((baseline[0] * (1 - (number - 1) / 8) + 0.25 * (number - 1) / 8)[:, None], 3, axis=1)
        )
        np.testing.assert_allclose(measured, expected, atol=5e-5, rtol=0)
    report.update(result="passed", batch_frames=5, batch_summary=summary, closed=session.status(), scope=__doc__)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    session.close()
    (f.dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]), batch=report.get("batch_frames"))))
