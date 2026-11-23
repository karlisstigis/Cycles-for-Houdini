# SPDX-License-Identifier: Apache-2.0
"""Owned native volume graphs, analytic integrals and transactional field errors."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
from cycles_houdini.device_selection import set_devices as set_rop_devices
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import subprocess

import hou
import numpy as np
import OpenImageIO as OIIO
import cycles_houdini as ch
from cycles_houdini import native_scene

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/materials/volumes"
destination.mkdir(parents=True, exist_ok=True)
fixture = destination / "source.vdb"
subprocess.run([str(runtime_executable("ch_harness.exe")), "--write-pyro-fixture", str(fixture)], check=True)
obj = hou.node("/obj").createNode("geo", "volume_material_receiver", run_init_scripts=False)
source = obj.createNode("file")
source.parm("file").set(str(fixture))
stash = obj.createNode("stash")
stash.parm("stash").set(source.geometry())
source.destroy()
fixture.unlink()
assert len(stash.geometry().prims()) == 4
camera = hou.node("/obj").createNode("cam", "volume_material_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)
absorb = ch.create_material(name="native_absorption", shader="absorption_volume")
absorb.node("volume_shader").parmTuple("color").set((0, 0, 0))
principled = ch.create_material(name="native_pyro", shader="principled_volume")
assert principled.node("volume_shader").parm("density_attribute").eval() == "density"
assert principled.node("volume_shader").parm("temperature_attribute").eval() == "temperature"
assert not principled.node("volume_shader").parm("color_attribute").eval()
emission = hou.node("/mat").createNode("cycles::volume_material::1", "native_field_emission")
emission.node("volume_shader").destroy()
emit = emission.createNode("cycles::emission::1", "volume_shader")
emit.parmTuple("color").set((0.8, 0.2, 0.1))
attribute = emission.createNode("cycles::attribute::1", "emission_field")
attribute.parm("attribute").set("emission")
scale = emission.createNode("cycles::math::1", "emission_scale")
scale.parm("math_type").set("multiply")
scale.parm("value2").set(2.0)
scale.setNamedInput("value1", attribute, "fac")
emit.setNamedInput("strength", scale, "value")
emission.node("volume_output").setNamedInput("volume", emit, "emission")
obj.parm("shop_materialpath").set(absorb.path())
decorated = obj.createNode("python", "field_assignments")
decorated.setInput(0, stash)
decorated.addSpareParmTuple(hou.StringParmTemplate("decorator", "Decorator", 1, default_value=("",)))
decorated.parm("python").set("import hou\nexec(hou.pwd().evalParm('decorator'))")
missing = obj.createNode("python", "missing_temperature")
missing.setInput(0, stash)
# This is a missing-field fixture, not a retained loose-point fixture. Native
# mixed SOPs now render unreferenced points as a point cloud; delete the unused
# grid anchor as well so the assigned volume shader still has only VDB geometry.
missing.parm("python").set(
    "import hou\ng=hou.pwd().geometry();g.deletePrims([p for p in g.prims() if p.attribValue('name')=='temperature'],keep_points=False)"
)
assert len(missing.geometry().prims()) == 3 and len(missing.geometry().points()) == 3
renamed = obj.createNode("python", "custom_fields")
renamed.setInput(0, stash)
renamed.parm("python").set(
    "import hou\nfor p in hou.pwd().geometry().prims():\n if p.attribValue('name')=='velocity':p.setAttribValue('name','tint')"
)
tint = obj.createNode("volumewrangle", "positive_tint")
tint.setInput(0, renamed)
tint.parm("snippet").set("v@tint=set(.2,.4,.8);")


def scene(sop=stash, background=(1, 1, 1), **other):
    return dict(
        geometry=[ch.geometry_request(obj, sop, material_mode="native", material="unused_fallback")],
        material_resolver=native_scene.resolve_materials,
        camera_path=camera.path(),
        background=background,
        **other,
    )


def mean(frame):
    pixels = np.frombuffer(frame["planes"]["combined"]["pixels"], dtype=np.float32).reshape(
        frame["height"], frame["width"], 4
    )
    return pixels[21:27, 37:43, :3].mean(axis=(0, 1)).tolist()


def expect(frame, expected, tolerance=0.025):
    actual = mean(frame)
    assert max(abs(a - b) for a, b in zip(actual, expected)) < tolerance, (actual, expected)
    return actual


def reject(session, payload, fragment):
    old = session.status()
    info = session.frame_info()
    try:
        session.apply(payload)
    except RuntimeError as error:
        assert fragment in str(error), (fragment, str(error))
    else:
        raise AssertionError("Unsupported volume transaction accepted: " + fragment)
    assert session.status()["generation"] == old["generation"] and session.frame_info() == info
    assert session.status()["owned_geometry_bytes"] == old["owned_geometry_bytes"]


def recover(session, payload, fragment):
    frame = session.wait(session.apply(payload))
    state = session.status()
    assert state["state"] != "failed" and state["geometry_resources"] == 1, state
    assert any(fragment in d["message"] for d in state["source_diagnostics"]), state
    assert np.isfinite(np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32)).all()
    return frame


reports = []
for device in ("CPU", ch.default_device()):
    label = "cpu" if device == "CPU" else "optix"
    checks = []
    obj.parm("shop_materialpath").set(absorb.path())
    absorb.node("density_scale").parm("value2").set(1.0)
    with ch.Session(device=device, width=80, height=48, samples=512, collect_diagnostics=True) as session:
        frame = session.wait(session.apply(scene()))
        baseline = session.status()
        scans = baseline["material_binding_scans"]
        actual = expect(frame, [math.exp(-9 / 8)] * 3)
        session.write_exr(destination / (label + "-absorption.exr"))
        checks.append(
            dict(
                name="Native Volume Info/density-scale absorption matches independent Beer-Lambert integral",
                measured=actual,
            )
        )
        absorb.node("density_scale").parm("value2").set(2.0)
        expect(session.wait(session.apply(scene())), [math.exp(-9 / 4)] * 3)
        assert session.status()["geometry_conversions"] == baseline["geometry_conversions"]
        assert session.status()["material_binding_scans"] == scans
        camera.parm("tx").set(0.05)
        expect(session.wait(session.apply(scene())), [math.exp(-9 / 4)] * 3)
        obj.parm("scale").set(2.0)
        expect(session.wait(session.apply(scene())), [math.exp(-9 / 2)] * 3)
        assert session.status()["geometry_conversions"] == baseline["geometry_conversions"]
        camera.parm("tx").set(0.0)
        obj.parm("scale").set(1.0)
        absorb.node("density_scale").parm("value2").set(1.0)
        checks.append(dict(name="Shader/camera/object-scale edits preserve owned sparse grids and binding metadata"))

        obj.parm("shop_materialpath").set(emission.path())
        frame = session.wait(session.apply(scene(background=(0, 0, 0))))
        expected = [value * 0.25 * 2 * 9 / 8 for value in (0.8, 0.2, 0.1)]
        actual = expect(frame, expected)
        session.write_exr(destination / (label + "-emission.exr"))
        checks.append(
            dict(
                name="Custom scalar Attribute and volume Emission match independent path integral",
                measured=actual,
                expected=expected,
            )
        )
        attribute.parm("attribute").set("density")
        scale.parm("value2").set(0.5)
        expect(session.wait(session.apply(scene(background=(0, 0, 0)))), expected)
        attribute.parm("attribute").set("emission")
        scale.parm("value2").set(2.0)
        checks.append(dict(name="Scalar field selection changes shader demand without retaining source files"))

        obj.parm("shop_materialpath").set(principled.path())
        p = principled.node("volume_shader")
        p.parmTuple("color").set((0, 0, 0))
        p.parm("density").set(1.0)
        p.parm("blackbody_intensity").set(0.0)
        expect(session.wait(session.apply(scene())), [math.exp(-9 / 8)] * 3)
        assert session.status()["geometry_resources"] == 1
        p.parm("density").set(0.0)
        p.parm("blackbody_intensity").set(1.0)
        p.parm("temperature").set(1000.0)
        warm = mean(session.wait(session.apply(scene(background=(0, 0, 0)))))
        p.parm("temperature").set(1500.0)
        hot = mean(session.wait(session.apply(scene(background=(0, 0, 0)))))
        assert warm[0] > warm[1] > warm[2] >= 0 and sum(hot) > 2 * sum(warm), (warm, hot)
        session.write_exr(destination / (label + "-blackbody.exr"))
        checks.append(
            dict(name="Principled constructed field defaults, Kelvin scale and blackbody update", warm=warm, hot=hot)
        )
        recover(session, scene(missing, background=(0, 0, 0)), "Missing shader volume field: temperature")
        p.parm("blackbody_intensity").set(0.0)
        expect(session.wait(session.apply(scene(missing, background=(0, 0, 0)))), [0, 0, 0])
        checks.append(
            dict(
                name="Only active blackbody inputs require temperature; missing fields use a non-emitting density fallback"
            )
        )
        p.parm("density").set(1.0)
        p.parm("density_attribute").set("tint")
        recover(session, scene(tint), "wrong scalar/vector type: tint")
        p.parm("density_attribute").set("density")
        p.parm("color_attribute").set("emission")
        recover(session, scene(), "wrong scalar/vector type: emission")
        p.parm("color_attribute").set("")
        p.parm("density_attribute").set("missing")
        recover(session, scene(), "Missing shader volume field: missing")
        p.parm("density_attribute").set("density")
        checks.append(dict(name="Named Principled scalar/vector field requirements validated before mutation"))

        obj.parm("shop_materialpath").set(emission.path())
        color_field = emission.createNode("cycles::attribute::1", "tint_field")
        color_field.parm("attribute").set("tint")
        emit.setNamedInput("color", color_field, "color")
        expect(
            session.wait(session.apply(scene(tint, background=(0, 0, 0)))),
            [value * 0.25 * 2 * 9 / 8 for value in (0.2, 0.4, 0.8)],
        )
        recover(session, scene(background=(0, 0, 0)), "Missing shader volume field: tint")
        emit.setInput(0, None)
        color_field.destroy()
        expect(session.wait(session.apply(scene(background=(0, 0, 0)))), expected)
        checks.append(dict(name="Custom vec3 volume field reads and shader-demand removal recover"))

        # A volume is a collection of grids. Primitive bindings can supply its
        # one material, but cannot turn each field into a separate medium.
        obj.parm("shop_materialpath").set("/mat/nonexistent_unused_fallback")
        decorated.parm("decorator").set(
            "g=hou.pwd().geometry();g.addAttrib(hou.attribType.Prim,'shop_materialpath','')\nfor p in g.prims():p.setAttribValue('shop_materialpath',"
            + repr(absorb.path())
            + ")"
        )
        expect(session.wait(session.apply(scene(decorated))), [math.exp(-9 / 8)] * 3)
        decorated.parm("decorator").set(
            decorated.evalParm("decorator")
            + "\ng.prims()[1].setAttribValue('shop_materialpath',"
            + repr(emission.path())
            + ")"
        )
        recover(session, scene(decorated), "conflicting materials")
        obj.parm("shop_materialpath").set(absorb.path())
        decorated.parm("decorator").set(
            "g=hou.pwd().geometry();g.addAttrib(hou.attribType.Global,'shop_materialpath','');g.setGlobalAttribValue('shop_materialpath',"
            + repr(emission.path())
            + ")"
        )
        expect(session.wait(session.apply(scene(decorated, background=(0, 0, 0)))), expected)
        checks.append(
            dict(name="OBJ/detail/primitive volume precedence, unused fallback and conflicting field bindings")
        )
        for owner in ("Point", "Vertex"):
            decorated.parm("decorator").set(
                "g=hou.pwd().geometry();g.addAttrib(hou.attribType."
                + owner
                + ",'shop_materialpath','')\nfor item in "
                + ("g.points()" if owner == "Point" else "[v for p in g.prims() for v in p.vertices()]")
                + ":item.setAttribValue('shop_materialpath',"
                + repr(absorb.path())
                + ")"
            )
            session.wait(session.apply(scene(decorated)))
            assert session.status()["source_diagnostics"] and not session.status()["error"]
        checks.append(
            dict(name="Unsupported per-grid-point and vertex bindings warn and retain supported underlying bindings")
        )

        obj.parm("shop_materialpath").set(absorb.path())
        frame = session.wait(session.apply(scene()))
        info = absorb.node("volume_fields")
        unused = absorb.createNode("cycles::math::1", "unused_temperature_branch")
        unused.setNamedInput("value1", info, "temperature")
        expect(session.wait(session.apply(scene(missing))), [math.exp(-9 / 8)] * 3)
        unused.destroy()
        checks.append(dict(name="Unused Volume Info outputs do not require unrelated fields"))
        # Native subnet closures retain the output domain after flattening.
        sub = ch.create_subnet(absorb, name="volume_subnet")
        connector = ch.create_connector(sub, "medium", type="bsdf", kind="output")
        shader = absorb.node("volume_shader")
        shader.setInput(shader.inputIndex("density"), None)
        nested = sub.createNode("cycles::absorption_volume::1", "nested_absorption")
        nested.parmTuple("color").set((0, 0, 0))
        nested.parm("density").set(0.5)
        connector.setInput(0, nested, 0)
        absorb.node("volume_output").setNamedInput("volume", sub, "medium")
        # Constant Density has no implicit grid read; the integral is over the
        # engine's padded bounds, so use a explicit field inside the subnet.
        field = sub.createNode("cycles::volume_info::1", "density_field")
        nested.setNamedInput("density", field, "density")
        expect(session.wait(session.apply(scene())), [math.exp(-9 / 8)] * 3)
        absorb.node("volume_output").setNamedInput("volume", shader, "volume")
        shader.setNamedInput("density", absorb.node("density_scale"), "value")
        sub.destroy()
        checks.append(dict(name="Native volume closures and fields flatten through typed subnetworks"))

        graph = ch.material_graph(absorb)
        key = "transaction_validation"
        bad = deepcopy(graph)
        bad["surface"] = bad["volume"]
        session.wait(session.apply(dict(materials=[dict(key=key, kind="graph", graph=bad)])))
        bad = deepcopy(graph)
        bad["surface"] = bad.pop("volume")
        session.wait(session.apply(dict(materials=[dict(key=key, kind="graph", graph=bad)])))
        bad = dict(
            nodes=[dict(id="surface", definition="cycles/diffuse_bsdf/1", parameters={})],
            links=[],
            volume=dict(node="surface", socket="BSDF"),
        )
        session.wait(session.apply(dict(materials=[dict(key=key, kind="graph", graph=bad)])))
        session.wait(session.apply(dict(deleted_materials=[key])))
        checks.append(dict(name="Cross-domain closure routing produces compatible diagnostic graphs"))
        surface = dict(
            nodes=[dict(id="surface", definition="cycles/diffuse_bsdf/1", parameters={})],
            links=[],
            surface=dict(node="surface", socket="BSDF"),
        )

        def replacement(graph):
            return dict(
                material_resolver=lambda references, now: [
                    dict(key=reference["key"], kind="graph", graph=graph) for reference in references
                ]
            )

        recover(session, replacement(surface), "Sparse volume requires a usable volume-only shader")
        missing_field = deepcopy(graph)
        field_id = next(node["id"] for node in missing_field["nodes"] if node["definition"] == "cycles/volume_info/1")
        for node in missing_field["nodes"]:
            if node["id"] == field_id:
                node.update(definition="cycles/attribute/1", parameters=dict(attribute="missing_retained_field"))
        for link in missing_field["links"]:
            if link["from"]["node"] == field_id:
                link["from"]["socket"] = "fac"
        recover(session, replacement(missing_field), "Missing shader volume field: missing_retained_field")
        checks.append(dict(name="Retained volumes recover incompatible shader domains and new missing field demands"))
        # Failed host captures conservatively invalidate extraction metadata;
        # recover once before measuring the warm interactive edit sequence.
        recovery_before = session.status()
        expect(session.wait(session.apply(scene())), [math.exp(-9 / 8)] * 3)
        recovery_conversion_delta = session.status()["geometry_conversions"] - recovery_before["geometry_conversions"]
        assert recovery_conversion_delta <= 1, recovery_conversion_delta
        generations = []
        allocations = []
        before = session.status()
        for iteration in range(16):
            absorb.node("density_scale").parm("value2").set(1.0 + 0.1 * (iteration % 2))
            frame = session.wait(session.apply(scene()))
            expect(frame, [math.exp(-9 / 8 * (1.0 + 0.1 * (iteration % 2)))] * 3)
            generations.append(frame["generation"])
            allocations.append(session.status()["device_allocation_bytes"])
        assert generations == sorted(set(generations)), generations
        # Density * 1 is folded away by Cycles, while * 1.1 keeps its SVM
        # instructions. Compare like compiled configurations after warm-up.
        for phase in (0, 1):
            assert len({value for index, value in enumerate(allocations) if index >= 6 and index % 2 == phase}) == 1, (
                allocations
            )
        assert session.status()["geometry_conversions"] == before["geometry_conversions"]
        checks.append(
            dict(
                name="Repeated native graph edits retain sparse ownership and stable warm tracked allocations",
                allocations=allocations,
                failed_capture_recovery_conversions=recovery_conversion_delta,
            )
        )

        # Float Curve replaces Float Ramp (Range 0..1, Horizontal, Factor 1).
        ramp = absorb.createNode("cycles::float_curve::1", "density_ramp")
        ramp.parmTuple("curve_domain").set((0.0, 1.0))
        ramp.parm("curve_extension").set(1)
        ramp.parm("fac").set(1.0)
        ramp.parm("curve_value").set(hou.Ramp((hou.rampBasis.Linear,) * 2, (0.0, 1.0), (0.0, 2.0)))
        ramp.setNamedInput("value", absorb.node("volume_fields"), "density")
        shader.setNamedInput("density", ramp, "value")
        promoted = ch.promote_parameter(absorb, ramp, "curve_value", name="density_profile", label="Density Profile")[0]
        expect(session.wait(session.apply(scene())), [math.exp(-9 / 4)] * 3)
        # The multiplier is sampled at the requested time; geometry remains
        # nominal and static, and Houdini's current frame is never advanced.
        absorb.parm("density_profile2value").setExpression("$F", hou.exprLanguage.Hscript)
        original_frame = hou.frame()
        expect(session.wait(session.apply(scene(), at_time=hou.frameToTime(3))), [math.exp(-27 / 8)] * 3)
        assert hou.frame() == original_frame
        absorb.parm("density_profile2value").deleteAllKeyframes()
        promoted.set(hou.Ramp((hou.rampBasis.Linear,) * 2, (0.0, 1.0), (0.0, 2.0)))
        shader.setNamedInput("density", absorb.node("density_scale"), "value")
        ramp.destroy()
        absorb.removeSpareParmTuple(promoted.tuple())
        checks.append(dict(name="Per-ray volume density ramps and whole-ramp promotion evaluate explicit render time"))
    assert session.status()["device_allocation_bytes"] == session.status()["owned_geometry_bytes"] == 0
    reports.append(dict(device=device, result="passed", checks=checks, close=session.status()))

obj.parm("shop_materialpath").set(absorb.path())
absorb.node("density_scale").parm("value2").set(1.0)
rop = hou.node("/out").createNode("cycles_ipr", "native_volume_material_cycles")
set_rop_devices(rop, "CPU")
for name, value in dict(soppath=stash.path(), camera=camera.path(), resx=80, resy=48, samples=512).items():
    rop.parm(name).set(value)
rop.parmTuple("background").set((1, 1, 1))
rop.parmTuple("lightstrength").set((0, 0, 0))
hou.hipFile.save(str(destination / "native-volume-materials.hiplc"))
saved = ch.material_graph(absorb)
hou.hipFile.load(str(destination / "native-volume-materials.hiplc"), suppress_save_prompt=True)
assert ch.material_graph(hou.node("/mat/native_absorption")) == saved
rop = hou.node("/out/native_volume_material_cycles")
absorb = hou.node("/mat/native_absorption")
absorb.node("density_scale").parm("value2").setExpression("$F", hou.exprLanguage.Hscript)
rop.parm("picture").set(str(destination / "volume-frame.$F4.exr"))
for path in destination.glob("volume-frame.*.exr"):
    path.unlink()
rop.render(frame_range=(1, 3, 1))
for frame_number in (1, 2, 3):
    image = OIIO.ImageBuf(str(destination / f"volume-frame.{frame_number:04}.exr")).get_pixels()
    assert image.shape[:2] == (48, 80)
    actual = image[21:27, 37:43, :3].mean(axis=(0, 1))
    assert max(abs(float(value) - math.exp(-9 / 8 * frame_number)) for value in actual) < 0.025, (frame_number, actual)
report = dict(
    result="passed",
    devices=reports,
    color_policy=ch.color_policy(),
    ocio_config=os.environ.get("OCIO"),
    host_checks=[
        "Constructed field defaults",
        "Editable preset graphs",
        "Volume subnets",
        "Saved volume builder identities",
        "Native SOHO batch renders three independently checked animated-density frames",
    ],
    scope="Sparse float/vec3 VDB volume graphs with one output domain. Closed mesh media, combined outputs, displacement and full phase-function certification remain separate work.",
)
(destination / "houdini-volume-material-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", checks_per_device=[len(item["checks"]) for item in reports])))
