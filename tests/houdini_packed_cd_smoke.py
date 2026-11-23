# SPDX-License-Identifier: Apache-2.0
"""Independent Cd images, packed sharing, binding-local recovery and live repairs."""

from subdivision_test_setup import configure_render, set_subdivision
import argparse
import json
import traceback
import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import final_render
from packed_cd_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
f = Fixture(device)
report = dict(result="running", device=device, checks=[])
try:
    assert f.rop.parmTuple("basecolor").eval() == (1, 1, 1), f.rop.parmTuple("basecolor").eval()
    assert f.rop.parm("basecolorr").parmTemplate().defaultValue() == (1, 1, 1)
    with ch.Session(
        device=device,
        width=f.width,
        height=f.height,
        samples=4,
        threads=2,
        collect_diagnostics=True,
        use_texture_cache=False,
    ) as session:

        def render(label, warning=False, expected=None):
            frame = session.wait(session.apply(f.update(), reuse_if_unchanged=True))
            status = session.status()
            assert not status["error"] and status["state"] == "complete", status
            if bool(status["source_diagnostics"]) != warning:
                report["failure_source"] = {}
                for node in (f.prototype_fields, f.copies, f.fields):
                    g = node.geometry()
                    report["failure_source"][node.path()] = dict(
                        errors=node.errors(),
                        snippet=node.evalParm("snippet") if node.parm("snippet") else "",
                        attributes=[
                            dict(
                                owner=str(a.type()),
                                storage=str(a.dataType()),
                                size=a.size(),
                                values=repr([x.attribValue(a) for x in elements[:4]]),
                            )
                            for a, elements in (
                                (g.findPointAttrib("Cd"), g.points()),
                                (g.findPrimAttrib("Cd"), g.prims()),
                                (g.findVertexAttrib("Cd"), tuple(v for p in g.prims() for v in p.vertices())),
                            )
                            if a
                        ],
                    )
            assert bool(status["source_diagnostics"]) == warning, (label, status["source_diagnostics"])
            colors = f.colors(frame, expected, warning)
            report["checks"].append(dict(label=label, colors=colors, status=status))
            return frame, status

        frame, status = render("Cd on packed primitives")
        assert status["geometry_resources"] == status["mesh_variant_resources"] == 1, status
        conversions, cooks = status["geometry_conversions"], f.grid.cookCount()
        for offset in (0.07, 0):
            f.set_cd(offset=offset)
            _, status = render("Uniform instance edit " + str(offset))
            assert status["geometry_conversions"] == conversions and f.grid.cookCount() == cooks, status
        for owner in ("point", "vertex", "primitive", "detail"):
            f.set_cd(owner=owner)
            render("Packed " + owner + " Cd")
        for color_attribute, unnamed in ((True, False), (True, True), (False, False)):
            f.select_source(color_attribute, unnamed)
            for owner in ("primitive", "point", "vertex", "detail"):
                f.set_cd(owner=owner)
                render("Shader source / packed owner " + str((color_attribute, unnamed, owner)))
                f.set_cd(owner=owner, offset=0.04)
                render("Repaired/edited packed owner " + owner)
        f.set_cd()
        for mode in ("bad_one", "good", "bad_type", "good", "bad_size", "good", "missing", "good"):
            f.set_cd(mode)
            _, status = render("Packed recovery " + mode, mode != "good")
            if mode == "bad_one":
                assert status["geometry_resources"] == 2, status
                assert any("non-finite" in issue["message"] for issue in status["source_diagnostics"]), status[
                    "source_diagnostics"
                ]
            if mode in ("bad_type", "bad_size"):
                assert any(
                    "three-component float color" in issue["message"] for issue in status["source_diagnostics"]
                ), status["source_diagnostics"]
            if mode == "good":
                assert status["geometry_resources"] == status["mesh_variant_resources"] == 1, status
        for color_attribute in (False, True):
            f.select_source(color_attribute)
            for owner in ("point", "primitive", "vertex", "detail"):
                f.set_cd(owner=owner, packed=False)
                render("Unpacked " + owner + " Cd / " + str(color_attribute))
                f.set_cd(owner=owner, offset=0.06, packed=False)
                render("Unpacked " + owner + " edit / " + str(color_attribute))
                f.set_cd("bad_type", owner=owner, packed=False)
                render("Unpacked " + owner + " invalid / " + str(color_attribute), True)
                f.set_cd(owner=owner, packed=False)
                render("Unpacked " + owner + " repair / " + str(color_attribute))
        f.select_source()
        f.set_cd()
        for scheme in ("linear", "catmull_clark"):
            configure_render(f.rop, dict(subdivision=dict(type=scheme, space="object", dicing_rate=0.25, max_level=3)))
            for owner in ("point", "primitive", "vertex", "detail"):
                f.set_cd(owner=owner, packed=False)
                render("Subdivision " + scheme + " / " + owner)
                f.set_cd(owner=owner, packed=False, offset=0.03)
                render("Subdivision edit " + scheme + " / " + owner)
        set_subdivision(f.rop, type="none")
        # Each domain is authored independently: SOP/HOM authoring can promote
        # Cd rather than preserving two attributes with the same public name.
        f.prototype_fields.parm("snippet").set("")
        f.set_cd()
        for owner in ("point", "primitive", "vertex", "detail"):
            f.prototype_fields.parm("class").set(f.prototype_fields.parm("class").menuItems().index(owner))
            f.prototype_fields.parm("snippet").set("v@Cd={.11,.51,.31};")
            f.set_cd("bad_one")
            render("Prototype " + owner + " Cd retains precedence", expected=[(0.11, 0.51, 0.31)])
        f.prototype_fields.parm("snippet").set("")
        f.set_cd()
        f.output.parm("input").set(1)
        f.outer.parm("snippet").set("v@Cd={.91,.71,.31};")
        render("Nearest nested packed Cd")
        f.set_cd("missing")
        render("Missing inner Cd inherits ancestor", expected=[(0.91, 0.71, 0.31)])
        f.output.parm("input").set(0)
        f.set_cd()
        for shape in (1, 2, 3):
            f.shape.parm("input").set(shape)
            render("Packed points/hair/mixed Cd " + str(shape))
        for shape, owners in (
            (1, ("point", "detail")),
            (2, ("point", "primitive", "vertex", "detail")),
            (3, ("point", "detail")),
        ):
            f.shape.parm("input").set(shape)
            for owner in owners:
                f.prototype_fields.parm("class").set(f.prototype_fields.parm("class").menuItems().index(owner))
                f.prototype_fields.parm("snippet").set("v@Cd={.11,.51,.31};")
                render("Prototype points/hair/mixed Cd " + str((shape, owner)), expected=[(0.11, 0.51, 0.31)])
                f.prototype_fields.parm("snippet").set("v@Cd={.41,.21,.61};")
                render("Prototype points/hair/mixed Cd edit " + str((shape, owner)), expected=[(0.41, 0.21, 0.61)])
                f.prototype_fields.parm("snippet").set("")
        f.shape.parm("input").set(0)
        # Color failures must neither poison valid displacement nor feed magenta height.
        f.displacement.setInput(f.displacement.inputNames().index("height"), None)
        f.displacement.parm("height").set(0.15)
        f.material.node("surface_output").setNamedInput("displacement", f.displacement, "displacement")
        f.material.parm("cycles_displacement_method").set("true")
        for mode in ("good", "bad_one", "good"):
            f.set_cd(mode)
            frame, _ = render("Independent height / " + mode, mode != "good")
            f.heights(frame, f.expected(), independent=True)
        separate = f.material.createNode("cycles::separate_xyz::1", "height_red")
        separate.setNamedInput("vector", f.source, "color")
        f.displacement.setNamedInput("height", separate, "x")
        f.displacement.parm("scale").set(0.1)
        for mode in ("good", "bad_one", "good"):
            f.set_cd(mode)
            frame, _ = render("Cd-dependent height / " + mode, mode != "good")
            f.heights(frame, f.expected())
        f.select_source(True, True)
        separate.setNamedInput("vector", f.source, "color")
        for mode in ("good", "bad_one", "good"):
            f.set_cd(mode)
            frame, _ = render("Color Attribute-dependent height / " + mode, mode != "good")
            f.heights(frame, f.expected())
        f.material.node("surface_output").setInput(
            f.material.node("surface_output").inputNames().index("displacement"), None
        )
        configure_render(
            f.rop, dict(use_material_assignments=0, color_attribute="", material_kind="emission", emission_strength=1)
        )
        f.set_cd()
        render("Default fallback material is white", expected=[(1, 1, 1)])
        f.rop.parm("color_attribute").set("Cd")
        render("Default white tint preserves Cd")
        f.rop.parm("emission_strength").set(2)
        f.rop.parmTuple("basecolor").set((0.5, 0.25, 0.75))
        for mode in ("good", "bad_one", "good"):
            f.set_cd(mode)
            expected = [
                (1, 0, 1) if tuple(color) == (1, 0, 1) else tuple(np.asarray(color) * (0.5, 0.25, 0.75) * 2)
                for color in f.expected()
            ]
            render("ROP emission retains tint/strength / " + mode, mode != "good", expected)
        f.rop.parmTuple("background").set((0.6, 0.6, 0.6))
        for kind in ("diffuse", "principled"):
            configure_render(f.rop, dict(material_kind=kind, roughness=0.43, metallic=0.7))
            f.set_cd(packed=False)
            reference = session.wait(session.apply(f.update(), reuse_if_unchanged=True))
            f.set_cd()
            instance = session.wait(session.apply(f.update(), reuse_if_unchanged=True))
            status = session.status()
            assert not status["error"] and not status["source_diagnostics"], status

            def pixels(frame):
                p = frame["planes"]["combined"]
                return np.frombuffer(p["pixels"], np.float32).reshape(f.height, f.width, p["channels"])

            a, b = pixels(reference), pixels(instance)
            mask = (a[:, :, 3] > 0.999) & (b[:, :, 3] > 0.999)
            assert mask.sum() > 400
            np.testing.assert_allclose(
                a[mask, :3], b[mask, :3], rtol=0, atol=2e-4, err_msg="ROP " + kind + " packed/prototype material parity"
            )
            report["checks"].append(
                dict(
                    label="ROP " + kind + " retains tint/roughness/metallic",
                    status=status,
                    compared_pixels=int(mask.sum()),
                )
            )
        configure_render(f.rop, dict(use_material_assignments=1, color_attribute=""))
        f.rop.parmTuple("basecolor").revertToDefaults()
        assert f.rop.parmTuple("basecolor").eval() == (1, 1, 1)
        f.rop.parmTuple("background").set((0, 0, 0))
    report["closed"] = session.status()
    assert report["closed"]["device_allocation_bytes"] == report["closed"]["owned_geometry_bytes"] == 0
    f.set_cd("animation")
    f.rop.parm("picture").set(str(f.dest / "Cd.$F4.exr"))
    configure_render(f.rop, dict(persistent_data=True, use_denoise=False))
    f.rop.render(frame_range=(1, 5, 1))
    result = f.rop.cachedUserData(final_render._summary_key)
    assert result["completed_frames"] == 5 and result["created_sessions"] == 1 and not result["session_alive"], result
    for number in range(1, 6):
        image = oiio.ImageInput.open(str(f.dest / ("Cd.%04d.exr" % number)))
        assert image, number
        issues = json.loads(image.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        assert bool(issues) == (number == 3), (number, issues)
        pixels = np.asarray(image.read_image(format=oiio.FLOAT)).reshape(f.height, f.width, -1)
        image.close()
        expected = [(0.1 * number + 0.1 * k, 0.1 * (k + 1), 0.3) for k in range(4)]
        if number == 3:
            expected[1] = (1, 0, 1)
        f.colors(
            dict(planes=dict(combined=dict(pixels=pixels.astype(np.float32).tobytes(), channels=pixels.shape[2]))),
            expected,
            number == 3,
        )
    report.update(result="passed", batch_frames=5, batch_summary=result)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
finally:
    (f.dest / "report.json").write_text(json.dumps(report, indent=2))
assert report["result"] == "passed", report.get("error")
