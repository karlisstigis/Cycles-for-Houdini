# SPDX-License-Identifier: Apache-2.0
"""Shared packed motion, native binding variants, subdivision and displacement."""

import argparse, json, traceback
from functools import partial
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene
from packed_deformation_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
f = Fixture(device)
report = dict(result="running", device=device, checks=[])
material = ch.create_material(name="packed_displaced_surface", shader="emission")
material.node("surface_shader").parmTuple("color").set(tuple(float(v) for v in f.color))
material.node("surface_shader").parm("strength").set(1)
offset = material.createNode("cycles::vector_displacement::1", "packed_offset")
# The default this test was written for (new nodes now take Blender's).
offset.setParms(dict(scale=1.0, space="object"))
offset.parmTuple("vector").set((0, 0, 0.3))
material.node("surface_output").setNamedInput("displacement", offset, "displacement")
material.parm("cycles_displacement_method").set("true")
f.obj.parm("shop_materialpath").set(material.path())


def update(scheme="none", steps=3):
    settings = dict(
        type=scheme, space="object", adaptive=False, dicing_rate=0.1, max_level=3, boundary="edge_and_corner"
    )
    result = f.update(steps, sop=f.copies, material_mode="native", subdivision=settings)
    result["material_resolver"] = partial(
        native_scene.resolve_materials, shutter_times=native_scene.motion_times(result["motion"], 0)
    )
    return result


try:
    with ch.Session(
        device=device,
        width=f.width,
        height=f.height,
        samples=1024,
        threads=2,
        interactive=True,
        collect_diagnostics=True,
    ) as session:
        for scheme in ("none", "linear", "catmull_clark"):
            for temporal in (False, True):
                offset.parmTuple("vector")[0].deleteAllKeyframes()
                if temporal:
                    offset.parmTuple("vector")[0].setExpression("$T*24", hou.exprLanguage.Hscript)
                else:
                    offset.parmTuple("vector")[0].set(0)
                for steps in (3, 5):
                    frame = session.wait(session.apply(update(scheme, steps), at_time=0))
                    status = session.status()
                    assert not status["error"] and not status["source_diagnostics"], status
                    assert status["geometry_resources"] == 1 and status["object_resources"] == 4, status
                    assert bool(status["subdivision_control_faces"]) == (scheme != "none"), status
                    assert status["motion_cooks"] == steps - 1, status
                    expected = 0.4 / (1.5 if temporal else 1.0)
                    coverage = [f.coverage(frame, x, y) for x in (-0.9, 0.9) for y in (-0.4, 0.4)]
                    assert all(abs(value - expected) < 0.04 for value in coverage), (scheme, temporal, steps, coverage)
                    position = f.pixels(frame, "position")
                    valid = np.abs(position[:, :, 2]) > 0.01
                    assert valid.any() and np.allclose(position[:, :, 2][valid], 0.3, atol=1e-5), (
                        scheme,
                        temporal,
                        position[:, :, 2][valid],
                    )
                    report["checks"].append(
                        dict(
                            scheme=scheme,
                            temporal_displacement=temporal,
                            samples=steps,
                            coverage=coverage,
                            expected=expected,
                            status=status,
                        )
                    )
        # The same sampled source can have independently displaced native
        # material bindings without recooking it once per object or variant.
        other_material = ch.create_material(name="packed_other_displaced_surface", shader="emission")
        other_material.node("surface_shader").parmTuple("color").set(tuple(float(v) for v in f.color))
        other_material.node("surface_shader").parm("strength").set(1)
        other_offset = other_material.createNode("cycles::vector_displacement::1", "other_offset")
        # The default this test was written for (new nodes now take Blender's).
        other_offset.setParms(dict(scale=1.0, space="object"))
        other_offset.parmTuple("vector").set((0, 0, 0.6))
        other_material.node("surface_output").setNamedInput("displacement", other_offset, "displacement")
        other_material.parm("cycles_displacement_method").set("true")
        offset.parmTuple("vector")[0].deleteAllKeyframes()
        offset.parmTuple("vector")[0].set(0)
        other = hou.node("/obj").createNode("geo", "other_packed_displacement", run_init_scripts=False)
        other.parm("shop_materialpath").set(other_material.path())
        other.parm("tx").set(0.7)
        f.obj.parm("tx").set(-0.7)

        def bindings():
            result = f.update(material_mode="native")
            result["geometry"].append(
                ch.geometry_request(
                    other, f.pack, material="surface", material_mode="native", motion="deformation", at_time=0
                )
            )
            result["material_resolver"] = native_scene.resolve_materials
            return result

        for same in (False, True):
            if same:
                other.parm("shop_materialpath").set(material.path())
            frame = session.wait(session.apply(bindings(), at_time=0))
            status = session.status()
            assert (
                not status["source_diagnostics"]
                and status["geometry_resources"] == (1 if same else 2)
                and status["object_resources"] == 2
            ), status
            assert status["motion_cooks"] == (0 if same else 2), status
            z = f.pixels(frame, "position")[:, :, 2]
            for x, expected_z in ((-0.7, 0.3), (0.7, 0.3 if same else 0.6)):
                column = round((x / 4 + 0.5) * f.width)
                values = z[44:46, column - 1 : column + 1]
                # Position is an unfiltered data pass; shutter samples that
                # miss the narrow moving surface have the zero background.
                valid = values > 0
                assert valid.any() and np.allclose(values[valid], expected_z, atol=1e-5), (same, x, values)
                assert abs(f.coverage(frame, x, 0) - 0.4) < 0.04, (same, x, f.coverage(frame, x, 0))
            report["checks"].append(
                dict(name="Compatible displaced bindings" if same else "Different displacement bindings", status=status)
            )
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    (f.dest / "material-report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result="passed", checks=len(report["checks"]))))
