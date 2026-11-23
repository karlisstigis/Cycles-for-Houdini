# SPDX-License-Identifier: Apache-2.0
"""Packed deformation retention must respect shader fields, repairs and removal."""

import argparse, json, traceback
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene
from packed_deformation_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
f = Fixture(device)
report = dict(result="running", checks=[])
f.motion.parm("snippet").set(f.motion.parm("snippet").eval() + "\nv@paint={.2,.6,.3};v@other_paint={.1,.3,.7};")
material = ch.create_material(name="retained_packed_surface", shader="emission")
shader = material.node("surface_shader")
shader.parmTuple("color").set(tuple(float(v) for v in f.color))
shader.parm("strength").set(1)
field = material.createNode("cycles::attribute::1", "demanded_field")
field.parm("attribute").set("paint")
f.obj.parm("shop_materialpath").set(material.path())


def update():
    result = f.update(sop=f.copies, material_mode="native")
    result["material_resolver"] = native_scene.resolve_materials
    return result


def check(frame, color, tx=0):
    image = f.pixels(frame)
    assert np.isfinite(image).all()
    for x in (-0.9, 0.9):
        for y in (-0.4, 0.4):
            column = round(((x + tx) / 4 + 0.5) * f.width)
            row = round((y / 2.25 + 0.5) * f.height)
            rgb = image[row - 1 : row + 1, column - 1 : column + 1, :3].mean(axis=(0, 1))
            assert np.allclose(rgb, np.asarray(color) * 0.4, atol=0.035), (rgb, color, tx)


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

        def render(name, color, warning=False, tx=0, data=None):
            frame = session.wait(session.apply(data if data is not None else update(), at_time=0))
            status = session.status()
            assert status["state"] == "complete" and not status["error"], status
            assert status["render_device_ids"] == [device], status
            assert bool(status["source_diagnostics"]) == warning, status["source_diagnostics"]
            assert status["object_resources"] == 4 and status["geometry_resources"] == 1, status
            check(frame, color, tx)
            report["checks"].append(dict(name=name, status=status))
            return status

        first = render("Initial sampled prototype", f.color)
        warm = render("Warm full update retains the complete packed recipe", f.color)
        assert warm["motion_cooks"] == 0 and warm["geometry_conversions"] == first["geometry_conversions"]
        assert warm["partition_cache_hits"] - first["partition_cache_hits"] < 10
        f.obj.parm("tx").set(0.1)
        moved = render("OBJ movement rebases retained local shutter poses", f.color, tx=0.1)
        assert moved["motion_cooks"] == 0 and moved["geometry_conversions"] == warm["geometry_conversions"]
        assert moved["partition_cache_hits"] - warm["partition_cache_hits"] < 10
        f.obj.parm("tx").set(0)
        shader.setNamedInput("color", field, "color")
        demanded = render("Shader field demand invalidates the retained geometry recipe", (0.2, 0.6, 0.3))
        assert demanded["geometry_conversions"] > moved["geometry_conversions"]
        repeated = render("Warm named field remains correctly bound", (0.2, 0.6, 0.3))
        assert repeated["motion_cooks"] == 0 and repeated["geometry_conversions"] == demanded["geometry_conversions"]
        field.parm("attribute").set("other_paint")
        changed = render("Changing only the shader field name captures the other values", (0.1, 0.3, 0.7))
        assert changed["geometry_conversions"] > repeated["geometry_conversions"]
        field.parm("attribute").set("missing_paint")
        broken = render("Missing demanded field keeps geometry visible", (1, 0, 1), True)
        cached = render("Missing-field warning survives a warm update", (1, 0, 1), True)
        assert cached["source_diagnostics"] == broken["source_diagnostics"] and cached["motion_cooks"] == 0
        shader.setInput(0, None)
        render("Disconnecting the unused broken field clears its diagnostic", f.color)
        shader.setNamedInput("color", field, "color")
        render("Reconnecting the broken field restores its local diagnostic", (1, 0, 1), True)

        f.motion.parm("snippet").set(f.motion.parm("snippet").eval() + "\nv@missing_paint={.4,.1,.2};")
        render("SOP field repair restores the intended shader", (0.4, 0.1, 0.2))
        valid_source = f.motion.parm("snippet").eval()
        malformed_source = valid_source.replace("v@missing_paint={.4,.1,.2};", 's@missing_paint="not_numeric";')
        assert malformed_source != valid_source
        f.motion.parm("snippet").set(malformed_source)
        render("Malformed present field stays local to its shader", (1, 0, 1), True)
        shader.setInput(0, None)
        render("Disconnecting malformed data clears unused source diagnostics", f.color)
        shader.setNamedInput("color", field, "color")
        render("Reconnecting malformed data restores its diagnostic", (1, 0, 1), True)
        f.motion.parm("snippet").set(valid_source)
        render("Repairing malformed present data restores shading", (0.4, 0.1, 0.2))

        path = material.path()
        material.destroy()
        broken = render("Deleting an assigned material keeps every instance", (1, 0, 1), True)
        render("Deleted-material diagnostics persist on a warm update", (1, 0, 1), True)
        material = ch.create_material(name=path.rsplit("/", 1)[1], shader="emission")
        shader = material.node("surface_shader")
        shader.parmTuple("color").set((0.6, 0.3, 0.2))
        shader.parm("strength").set(1)
        render("Recreating the assigned material clears the diagnostic", (0.6, 0.3, 0.2))
        f.motion.parm("test_broken").set(1)
        for index in range(2):
            frame = session.wait(session.apply(update(), at_time=0))
            status = session.status()
            assert (
                status["source_diagnostics"] and status["object_resources"] == 4 and status["geometry_resources"] == 1
            ), status
            assert np.max(f.pixels(frame)[:, :, :3]) > 0.5
            if index:
                assert status["motion_cooks"] == 0
            report["checks"].append(
                dict(name="Broken shutter topology stays visible and warns through retention", status=status)
            )
        f.motion.parm("test_broken").set(0)
        render("Repairing shutter topology restores the sampled image", (0.6, 0.3, 0.2))
        frame = session.wait(session.apply(dict(geometry=[]), at_time=0))
        status = session.status()
        assert status["geometry_resources"] == status["object_resources"] == 0, status
        report["checks"].append(dict(name="Authoritative source removal retires the cached packed root", status=status))
        render("Reintroducing the source rebuilds its resources", (0.6, 0.3, 0.2))
        identities = f.obj.createNode("attribwrangle", "optional_packed_identity")
        identities.setInput(0, f.copies)
        identities.setParms({"class": 1, "snippet": "i@id=0;"})
        original = f.copies
        f.copies = identities
        for index in range(2):
            frame = session.wait(session.apply(update(), at_time=0))
            status = session.status()
            assert status["object_resources"] == 4 and status["geometry_resources"] == 1, status
            assert any(
                issue["code"] == "geometry.ambiguous_packed_identity" for issue in status["source_diagnostics"]
            ), status
            assert np.max(f.pixels(frame)[:, :, :3]) > 0.5
            if index:
                assert status["motion_cooks"] == 0
            report["checks"].append(
                dict(name="Ambiguous optional identity retains geometry and persistent diagnostics", status=status)
            )
        identities.parm("snippet").set("i@id=@primnum;")
        render("Repairing optional identity restores shutter correspondence", (0.6, 0.3, 0.2))
        identities.parm("snippet").set("")
        render("Removing optional identity remains usable without manual IDs", (0.6, 0.3, 0.2))

    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    (f.dest / "retention-report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
