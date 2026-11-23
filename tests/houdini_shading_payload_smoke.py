# SPDX-License-Identifier: Apache-2.0
"""Artist field/palette repair retains displacement and completed dicing."""

from subdivision_test_setup import configure_render, set_subdivision
import argparse, json, traceback
import hou, numpy as np
import cycles_houdini as ch
from cycles_houdini import object_properties
from pointiness_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
f = Fixture(device)
dest = f.dest.parent / "shading-payload"
dest.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=device, checks=[], generated_references=[], scope=__doc__)
f.set_shape("plane", deform=0.25, seams=True)
base = hou.Geometry()
base.merge(f.stash.geometry())
f.user.parm("attribute").set("rand")
f.shade("user")
f.displace()
object_properties.install(f.obj)
info = f.mat.createNode("cycles::object_info::1", "height_index")
generated = ch.create_material(name="retained_generated", shader="emission")
generated.node("surface_shader").parm("strength").set(1)
coordinates = generated.createNode("cycles::texture_coordinate::1", "coordinates")
generated.node("surface_shader").setNamedInput("color", coordinates, "generated")
intrinsic = generated.createNode("cycles::geometry::1", "intrinsic")
height = generated.createNode("cycles::displacement::1", "height")
height.setParms(dict(scale=0.2, midlevel=0.5, space="object"))
height.setNamedInput("height", intrinsic, "pointiness")
generated.node("surface_output").setNamedInput("displacement", height, "displacement")
generated.parm("cycles_displacement_method").set("both")


def fields(value=0.37):
    geo = hou.Geometry()
    geo.merge(base)
    geo.addAttrib(hou.attribType.Point, "rand", float(value))
    geo.addAttrib(hou.attribType.Point, "whole", 3)
    geo.addAttrib(hou.attribType.Point, "direction", (0.2, 0.45, 0.7))
    geo.addAttrib(hou.attribType.Point, "Cd", (0.3, 0.5, 0.7))
    positions = np.asarray([tuple(point.position()) for point in geo.points()])
    low = positions.min(axis=0)
    extent = np.ptp(positions, axis=0)
    geo.addAttrib(hou.attribType.Point, "normalized", (0.0, 0.0, 0.0))
    for point, position in zip(geo.points(), positions):
        point.setAttribValue("normalized", tuple(np.divide(position - low, extent, out=np.zeros(3), where=extent > 0)))
    f.stash.parm("stash").set(geo)


session_options = dict(
    device=device,
    width=f.width,
    height=f.height,
    samples=4,
    execution="worker",
    threads=2,
    use_texture_cache=False,
    collect_diagnostics=True,
)
try:
    with ch.Session(**session_options) as session:

        def render(label, expected=None, warning=False):
            frame = session.wait(session.apply(f.update(), reuse_if_unchanged=True))
            status = session.status()
            planes = f.planes(frame)
            assert (
                status["state"] == "complete" and not status["error"] and bool(status["source_diagnostics"]) == warning
            ), (label, status)
            assert all(np.isfinite(plane).all() for plane in planes.values()), label
            visible = planes["combined"][:, :, 3] > 0.999
            assert visible.sum() > 40, (label, int(visible.sum()))
            if expected is not None:
                rgb = np.broadcast_to(np.asarray(expected, dtype=np.float32), planes["combined"][visible, :3].shape)
                np.testing.assert_allclose(planes["combined"][visible, :3], rgb, rtol=0, atol=3e-5, err_msg=label)
            report["checks"].append(dict(label=label, status=status))
            return planes, status

        for scheme in ("none", "linear", "catmull_clark"):
            configure_render(f.rop, dict(subdivision=dict(type=scheme, space="object", dicing_rate=0.25, max_level=3)))
            for packed in (False, True):
                f.output.parm("input").set(int(packed))
                f.displace()
                f.user.parm("attribute").set("rand")
                f.emission.setNamedInput("color", f.user, "fac")
                fields()
                label = f"{scheme}, packed={packed}"
                baseline, status = render(label + " baseline", (0.37,) * 3)
                counters = (status["displacement_rebuilds"], status["subdivision_tessellations"])

                def unchanged(planes, current):
                    assert (current["displacement_rebuilds"], current["subdivision_tessellations"]) == counters, (
                        label,
                        counters,
                        current,
                    )
                    assert current["geometry_resources"] == 1 and current["mesh_variant_resources"] == 1, current
                    assert current["object_resources"] == (4 if packed else 1), current
                    np.testing.assert_allclose(
                        planes["position"], baseline["position"], rtol=0, atol=3e-6, err_msg=label
                    )
                    np.testing.assert_allclose(planes["normal"], baseline["normal"], rtol=0, atol=3e-6, err_msg=label)

                for value in (0.21, 0.64, float("nan"), 0.82):
                    fields(value)
                    broken = not np.isfinite(value)
                    planes, current = render(
                        label + " scalar " + str(value), (1, 0, 1) if broken else (value,) * 3, broken
                    )
                    unchanged(planes, current)
                for name, socket, expected in (
                    ("whole", "fac", (3, 3, 3)),
                    ("direction", "vector", (0.2, 0.45, 0.7)),
                    ("Cd", "color", (0.3, 0.5, 0.7)),
                ):
                    f.user.parm("attribute").set(name)
                    f.emission.setNamedInput("color", f.user, socket)
                    planes, current = render(label + " named " + name, expected)
                    unchanged(planes, current)
                f.user.parm("attribute").set("normalized")
                f.emission.setNamedInput("color", f.user, "vector")
                reference, current = render(label + " independent normalized coordinates")
                unchanged(reference, current)
                f.obj.parm("shop_materialpath").set(generated.path())
                actual, current = render(label + " new material requests Generated")
                unchanged(actual, current)
                # Face-varying fields and vertex Generated coordinates have
                # different Catmull-Clark boundary interpolation. A fresh engine
                # remains an independent reference for late vertex-field demand.
                if scheme != "catmull_clark":
                    np.testing.assert_allclose(
                        actual["combined"], reference["combined"], rtol=0, atol=3e-5, err_msg=label
                    )
                with ch.Session(**session_options) as fresh:
                    expected_generated = f.planes(fresh.wait(fresh.apply(f.update())))
                    assert not fresh.status()["source_diagnostics"], fresh.status()
                np.testing.assert_allclose(
                    actual["combined"], expected_generated["combined"], rtol=0, atol=3e-5, err_msg=label
                )
                report["generated_references"].append(
                    dict(
                        label=label,
                        maximum_difference=float(np.max(np.abs(actual["combined"] - expected_generated["combined"]))),
                    )
                )
                f.obj.parm("shop_materialpath").set(f.mat.path())
                f.user.parm("attribute").set("Cd")
                f.emission.setNamedInput("color", f.user, "color")
                actual, current = render(label + " restore original material", (0.3, 0.5, 0.7))
                unchanged(actual, current)
                # Actual height edits must still restore/rebuild physical geometry.
                f.displacement.parm("scale").set(0.35)
                changed, current = render(label + " actual height parameter", (0.3, 0.5, 0.7))
                assert (
                    current["displacement_rebuilds"] > counters[0]
                    and np.max(np.abs(changed["position"] - baseline["position"])) > 1e-5
                ), current
                f.user.parm("attribute").set("rand")
                f.emission.setNamedInput("color", f.user, "fac")
                f.displacement.setNamedInput("height", f.user, "fac")
                fields(0.21)
                first, current = render(label + " attribute-driven height", (0.21,) * 3)
                rebuilds = current["displacement_rebuilds"]
                fields(0.81)
                second, current = render(label + " actual height attribute", (0.81,) * 3)
                assert (
                    current["displacement_rebuilds"] > rebuilds
                    and np.max(np.abs(second["position"] - first["position"])) > 1e-4
                ), current
                fields(float("nan"))
                render(label + " broken shared height input", (1, 0, 1), True)
                fields(0.6)
                render(label + " repaired shared height input", (0.6,) * 3)
                f.displacement.setParms(dict(scale=0.03, midlevel=0))
                for socket, node in (("object_index", f.obj), ("material_index", f.mat)):
                    f.displacement.setNamedInput("height", info, socket)
                    node.parm("cycles_pass_index").set(1)
                    first, current = render(label + " " + socket + " height baseline", (0.6,) * 3)
                    rebuilds = current["displacement_rebuilds"]
                    assert current["mesh_variant_resources"] == 1, current
                    node.parm("cycles_pass_index").set(2)
                    second, current = render(label + " changed " + socket + " height", (0.6,) * 3)
                    assert (
                        current["displacement_rebuilds"] > rebuilds
                        and np.max(np.abs(second["position"] - first["position"])) > 1e-5
                    ), current
                    assert current["mesh_variant_resources"] == 1, current
                f.displacement.setNamedInput("height", f.geometry, "pointiness")
                f.displacement.setParms(dict(scale=0.2, midlevel=0.5))
                held, current = render(label + " intrinsic height before index metadata", (0.6,) * 3)
                index_counters = (current["displacement_rebuilds"], current["subdivision_tessellations"])
                for index in (3, 4):
                    f.obj.parm("cycles_pass_index").set(index)
                    f.mat.parm("cycles_pass_index").set(index)
                    actual, current = render(label + " unused index metadata " + str(index), (0.6,) * 3)
                    assert (current["displacement_rebuilds"], current["subdivision_tessellations"]) == index_counters, (
                        current
                    )
                    np.testing.assert_allclose(actual["position"], held["position"], rtol=0, atol=3e-6, err_msg=label)
                f.obj.parm("cycles_pass_index").set(0)
                f.mat.parm("cycles_pass_index").set(0)
        shader_counts = [r["status"]["engine_shader_resources"] for r in report["checks"]]
        assert max(shader_counts) <= shader_counts[0] + 4, shader_counts
        report["shader_bounds"] = dict(initial=shader_counts[0], peak=max(shader_counts), final=shader_counts[-1])
        session.close()
        closed = session.status()
        assert closed["device_allocation_bytes"] == closed["owned_geometry_bytes"] == 0, closed
        report.update(result="passed", closed=closed)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    (dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
