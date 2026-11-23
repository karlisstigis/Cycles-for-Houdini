# SPDX-License-Identifier: Apache-2.0
"""Nonlinear shutter fields, local off-frame repair and ordered final output."""

import pass_fixtures as pass_fixture

import argparse
import json
import time
import traceback

import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import camera_properties, final_render, native_scene, object_properties, registry
from sampled_instance_attribute_fixture import Fixture, attribute_data_id, plane


parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
args = parser.parse_args()
f = Fixture(args.device)
pass_fixture.set_parameters(
    f.rop, dict(crypto_object=1, crypto_material=1, crypto_levels=2, pass_object_id=1, pass_material_id=1)
)
path = f.dest / "report.json"
report = dict(result="running", scope=__doc__, device=f.devices, checks=[])
session = None


def save():
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")


def source_state():
    source = f.nominal_control.geometry()
    result = dict(
        cooks=f.nominal_control.cookCount(),
        field_cooks=f.fields.cookCount(),
        primitive_count=len(source.prims()),
        prototype=f.prototype_ownership(),
        fields=[],
    )
    for owner, finder in (
        ("primitive", source.findPrimAttrib),
        ("point", source.findPointAttrib),
        ("vertex", source.findVertexAttrib),
        ("detail", source.findGlobalAttrib),
    ):
        field = finder(f.field)
        if field is not None:
            result["fields"].append(
                dict(owner=owner, data_id=attribute_data_id(field), size=field.size(), data_type=str(field.dataType()))
            )
    velocity = source.findPointAttrib("v")
    result["velocity"] = (
        None
        if velocity is None
        else dict(size=velocity.size(), values=[point.attribValue(velocity) for point in source.points()])
    )
    return result


def render(label, *, retain=False, camera_only=False, expect_motion=False):
    before = session.status()
    cooks = (f.grid.cookCount(), f.fields.cookCount(), f.output.cookCount(), f.nominal_control.cookCount())
    update = native_scene.capture(f.rop, camera_only=True) if camera_only else f.update()
    update["passes"] = ["combined", "position", "depth", "normal", "object_id", "material_id"]
    if pass_fixture.evaluate(f.rop, "pass_motion"):
        update["passes"].append("motion")
    frame = session.wait(session.apply(update, reuse_if_unchanged=True), timeout=180)
    deadline = time.monotonic() + 10
    status = session.status()
    while status["state"] != "complete" and time.monotonic() < deadline:
        time.sleep(0.005)
        status = session.status()
    captured = f.capture_info(session)
    row = dict(
        label=label,
        field=f.field,
        owner=f.owner,
        fault=f.fault,
        amplitude=f.amplitude,
        shutter_offset=pass_fixture.evaluate(f.rop, "shutteroffset"),
        shutter_reference=f.shutter_reference(),
        status=status,
        captured=captured,
        source=None if camera_only else source_state(),
        cooks_before=cooks,
        cooks_after=(f.grid.cookCount(), f.fields.cookCount(), f.output.cookCount(), f.nominal_control.cookCount()),
    )
    report["checks"].append(row)
    save()
    assert status["state"] == "complete" and not status["error"], status
    assert status["execution"] == "worker" and not status["update_rejected"], status
    if len(report["checks"]) > 1:
        assert status["renderer_pid"] == report["checks"][0]["status"]["renderer_pid"], status
    row["images"] = f.inspect(frame)
    issues = [value for value in status["source_diagnostics"] if value["code"] == "geometry.shutter_instance_attribute"]
    expected_incomplete = f.expected_incomplete() if f.packed and pass_fixture.evaluate(f.rop, "allowmotionblur") else 0
    assert bool(issues) == bool(expected_incomplete), (row, issues)
    assert all(value["attribute"] == f.field and "requested-frame" in value["action"] for value in issues), issues
    allowed = ("geometry.shutter_instance_attribute", "geometry.ambiguous_packed_identity")
    if f.fault == 6:
        nominal = [value for value in status["source_diagnostics"] if value["code"] == "material.missing_attribute"]
        assert len(nominal) == 1 and "non-finite" in nominal[0]["message"], status["source_diagnostics"]
        assert len(issues) == 1 and "(1 affected components)" in issues[0]["message"], issues
        assert status["material_resources"] == 2, status
        allowed += ("material.missing_attribute",)
    assert not [value for value in status["source_diagnostics"] if value["code"] not in allowed], status[
        "source_diagnostics"
    ]
    assert captured["incomplete_instance_attribute_fields"] == expected_incomplete, captured
    components = 3 if f.kind == 3 else 1
    if f.packed and f.kind in (1, 2, 3):
        assert captured["points"] == int(f.kind in (1, 3)) and captured["curves"] == int(f.kind in (2, 3)), captured
        assert captured["mesh_triangles"] == (2 if f.kind == 3 else 0), captured
    if f.packed:
        nominal_invalid = int(f.fault == 6)
        assert captured["instance_attribute_fields"] == 4 * components - nominal_invalid, captured
        assert (
            status["geometry_resources"] == components + nominal_invalid
            and status["object_resources"] == 4 * components
        ), status
        if f.kind == 0 and pass_fixture.evaluate(f.material, "cycles_displacement_method") == "bump":
            # Binding-layout recovery can create logical geometry declarations
            # while the shared topology still owns one physical mesh/BVH.
            assert status["mesh_variant_resources"] == status["mesh_family_resources"] == 1, status
        object_blur = pass_fixture.parm(f.obj, "cycles_motion_blur")
        enabled = pass_fixture.evaluate(f.rop, "allowmotionblur") and (
            not object_blur or object_blur.eval() == "inherit"
        )
        # Velocity holds instance fields at the requested frame (task M1).
        enabled = enabled and not f.velocity_mode()
        expected_sampled = 4 * components - nominal_invalid - expected_incomplete if enabled and f.amplitude else 0
        assert captured["sampled_instance_attribute_fields"] == expected_sampled, captured
        assert captured["instance_attribute_sample_values"] == expected_sampled * pass_fixture.evaluate(
            f.rop, "motion_samples"
        ), captured
    else:
        assert captured["instance_attribute_fields"] == 0 and captured["mesh_motion_samples"] > 0, captured
    if retain:
        assert status["geometry_conversions"] == before["geometry_conversions"] and f.grid.cookCount() == cooks[0], row
        if f.packed and f.kind in (1, 2, 3):
            assert row["source"]["prototype"] == report["checks"][-2]["source"]["prototype"], row
    if camera_only:
        assert cooks == row["cooks_after"], row
    if expect_motion:
        assert captured["object_motion_samples"] >= 12 and captured["camera_motion_samples"] == 3, captured
    save()
    return frame, status, captured


try:
    with ch.Session(
        device=f.devices,
        width=f.width,
        height=f.height,
        samples=f.samples,
        threads=2,
        execution="worker",
        collect_diagnostics=True,
        use_texture_cache=False,
    ) as session:
        initial = f.output.geometry()
        assert not any(
            (
                initial.findPointAttrib("id"),
                initial.findPrimAttrib("id"),
                initial.findPointAttrib("name"),
                initial.findPrimAttrib("name"),
            )
        )
        f.stable_nominal(True)
        render("Controlled unchanged nominal data IDs")
        nominal_ids = f.nominal_ids()
        for amplitude, fault in ((0.55, 0), (1.0, 1), (1.0, 0), (1.0, 2), (1.0, 0), (1.0, 3), (1.0, 0)):
            f.configure(amplitude=amplitude, fault=fault)
            render("Off-frame edit with identical nominal IDs " + str((amplitude, fault)), retain=True)
            assert f.nominal_ids() == nominal_ids, (nominal_ids, f.nominal_ids())
            report["checks"][-1]["unchanged_nominal_ids"] = nominal_ids
        f.stable_nominal(False)
        f.configure(fault=6)
        render("Simultaneous nominal-invalid and off-frame-only fields retain distinct reasons/actions")
        f.configure()
        render("Repair simultaneous nominal/off-frame failures")
        camera_properties.install(f.camera)
        pass_fixture.parm(f.camera, "cycles_shutter_curve").set(
            hou.Ramp((hou.rampBasis.Linear,) * 2, (0.0, 1.0), (0.4, 0.4))
        )
        pass_fixture.parm(f.camera, "cycles_use_shutter_curve").set(1)
        render("Explicit positive uniform exposure retains exact box-shutter reference", retain=True)
        pass_fixture.parm(f.camera, "cycles_use_shutter_curve").set(0)
        for field in ("rand", "counter", "vector_field", "Cd"):
            f.configure(field)
            baseline, _, _ = render("Nonlinear packed " + field)
            controls = {name: plane(baseline, name).copy() for name in baseline["planes"] if name != "combined"}
            f.configure(field, amplitude=0.55)
            changed, _, _ = render("Off-frame-only amplitude edit " + field, retain=True)
            for name, values in controls.items():
                np.testing.assert_array_equal(
                    plane(changed, name), values, err_msg="Surface-only field edit changed " + name
                )
            assert (
                max(
                    np.max(np.abs(np.asarray(row["expected"]) - f.temporal_values(hou.frame(), row["copy"]) ** 2))
                    for row in report["checks"][-1]["images"]
                )
                > 0.002
            ), "A held centre value must not satisfy the nonlinear reference"
            for fault in (1, 0, 2, 0, 3, 0):
                f.configure(field, fault=fault)
                render("Off-frame field break/repair " + field + " " + str(fault), retain=True)
                if fault == 1:
                    render("Cached off-frame warning " + field, retain=True)
            for offset in (-1, 1):
                pass_fixture.parm(f.rop, "shutteroffset").set(offset)
                render("Offset shutter " + field + " " + str(offset), retain=True)
            pass_fixture.parm(f.rop, "shutteroffset").set(0)
            f.configure(field, owner="point")
            packed, _, _ = render("Packed point owner " + field, retain=True)
            f.configure(field, owner="point", packed=False)
            pass_fixture.parm(f.rop, "geometry_motion").set("deformation")
            expanded, _, _ = render("Independent expanded NamedAttribute motion " + field)
            difference = float(np.abs(plane(packed, "combined") - plane(expanded, "combined")).mean())
            assert difference < 0.0015, (field, difference)
            report["checks"][-1]["packed_expanded_mean_difference"] = difference
            pass_fixture.parm(f.rop, "geometry_motion").set("transform")
        for owner in ("vertex", "detail"):
            f.configure("Cd", owner=owner, color_attribute=True)
            render("Color Attribute shutter owner " + owner)
        f.configure("rand", nested=True)
        render("Nearest nested owner retains precedence")
        f.configure("rand", nested=True, fault=2)
        render("Off-frame missing inner owner holds nominal rather than switching ancestor", retain=True)
        f.configure("rand", nested=True)
        render("Repair nearest nested sampled owner", retain=True)
        f.configure("rand", ancestor=True)
        render("Missing inner field samples nearest valid ancestor")
        pass_fixture.parm(f.rop, "geometry_motion").set("velocity")
        for owner in ("primitive", "point"):
            for velocity in (False, True, False):
                f.configure("rand", owner=owner, duplicates=True, velocity=velocity)
                _, _, captured = render(
                    "Duplicate ordinary identities with "
                    + owner
                    + " fields and "
                    + ("valid" if velocity else "missing")
                    + " velocity"
                )
                source_velocity = report["checks"][-1]["source"]["velocity"]
                assert bool(source_velocity) == velocity, source_velocity
                if velocity:
                    assert source_velocity["size"] == 3 and captured["object_motion_samples"] >= 12, captured
                    np.testing.assert_allclose(
                        source_velocity["values"], np.tile((0.8, 0.0, 0.0), (4, 1)), atol=1e-6, rtol=0
                    )
        f.configure("rand", owner="point", duplicates=True, velocity=True, fault=1)
        render("Valid packed velocity retains local off-frame field hold")
        f.configure("rand", owner="point", duplicates=True, velocity=True)
        render("Valid packed velocity retains repaired field samples", retain=True)
        object_properties.install(f.obj)
        pass_fixture.parm(f.obj, "cycles_motion_blur").set("off")
        render("Velocity mode honors per-object blur disabled")
        pass_fixture.parm(f.obj, "cycles_motion_blur").set("inherit")
        render("Velocity mode restores independent sampled fields")
        pass_fixture.set_parameters(f.rop, dict(allowmotionblur=0, pass_motion=1))
        vector_frame, vector_status, captured = render("Vector-pass-only window does not acquire shader shutter fields")
        vectors = plane(vector_frame, "motion")
        assert np.isfinite(vectors).all() and np.max(np.abs(vectors)) > 0.1, vectors
        assert vector_status["motion_cooks"] == captured["sampled_instance_attribute_fields"] == 0, (
            vector_status,
            captured,
        )
        pass_fixture.set_parameters(f.rop, dict(allowmotionblur=1, pass_motion=0))
        render("ROP blur repair restores independent sampled fields")
        f.configure("rand", fault=5)
        render("Changing off-frame packed population holds valid nominal fields")
        f.configure("rand")
        render("Repair changing off-frame packed population", retain=True)
        pass_fixture.parm(f.rop, "geometry_motion").set("transform")
        for kind in (1, 2, 3):
            f.configure("vector_field", kind=kind)
            render("Packed point/hair/mixed temporal fields " + str(kind))
            f.configure("vector_field", kind=kind, fault=1)
            render("Packed point/hair/mixed local hold " + str(kind), retain=True)
            f.configure("vector_field", kind=kind)
            base, base_status, _ = render("Packed point/hair/mixed repair " + str(kind), retain=True)
            if kind in (1, 2):
                name, original, changed = ("point_radius", 0.2, 0.24) if kind == 1 else ("curve_width", 0.15, 0.2)
                pass_fixture.parm(f.rop, name).set(changed)
                resized, resized_status, _ = render("Authored " + name + " changes rendered coverage")
                assert resized_status["geometry_conversions"] > base_status["geometry_conversions"], resized_status
                area_ratio = float(plane(resized, "combined")[:, :, 3].sum() / plane(base, "combined")[:, :, 3].sum())
                assert area_ratio > 1.1, area_ratio
                report["checks"][-1]["coverage_area_ratio"] = area_ratio
                pass_fixture.parm(f.rop, name).set(original)
                restored, _, _ = render("Authored " + name + " repair restores coverage")
                np.testing.assert_allclose(
                    plane(restored, "combined")[:, :, 3], plane(base, "combined")[:, :, 3], atol=0.003, rtol=0
                )
        f.configure("rand")
        pass_fixture.parm(f.rop, "motion_samples").set(5)
        render("Five field samples use their own shutter grid")
        pass_fixture.parm(f.rop, "motion_samples").set(3)
        object_properties.install(f.obj)
        pass_fixture.parm(f.obj, "cycles_motion_blur").set("off")
        render("Object blur override holds requested-frame field")
        pass_fixture.parm(f.obj, "cycles_motion_blur").set("inherit")
        render("Object blur override repair")
        pass_fixture.parm(f.obj, "tx").setExpression("$T*1.2", hou.exprLanguage.Hscript)
        pass_fixture.parm(f.camera, "tx").setExpression("$T*1.4", hou.exprLanguage.Hscript)
        pass_fixture.parm(f.prototype_fields, "snippet").set("@P.z=.01*@Frame;")
        pass_fixture.parm(f.rop, "geometry_motion").set("deformation")
        for fault in (0, 1, 0):
            f.configure("rand", fault=fault)
            _, _, captured = render("Independent geometry/camera/object motion " + str(fault), expect_motion=True)
            assert captured["mesh_motion_samples"] > 0, captured
        pass_fixture.parm(f.prototype_fields, "snippet").set("")
        pass_fixture.parm(f.obj, "tx").deleteAllKeyframes()
        pass_fixture.parm(f.obj, "tx").set(0)
        pass_fixture.parm(f.camera, "tx").deleteAllKeyframes()
        pass_fixture.parm(f.camera, "tx").set(0)
        f.configure("rand")
        f.displacement.setNamedInput("height", f.square, "value")
        pass_fixture.parm(f.displacement, "scale").set(0.3)
        f.material.node("surface_output").setNamedInput("displacement", f.displacement, "displacement")
        pass_fixture.parm(f.material, "cycles_displacement_method").set("true")
        displaced, displaced_status, _ = render("Sampled instance field drives true displacement")
        assert displaced_status["mesh_variant_resources"] == 4, displaced_status
        f.configure("rand", owner="point", packed=False)
        expanded, _, _ = render("Expanded NamedAttribute displacement reference")
        mask = (plane(expanded, "combined")[:, :, 3] > 0.999) & (plane(displaced, "combined")[:, :, 3] > 0.999)
        position_difference = float(
            np.abs(plane(expanded, "position")[mask] - plane(displaced, "position")[mask]).mean()
        )
        assert position_difference < 0.003, position_difference
        report["checks"][-1]["packed_expanded_position_mean_difference"] = position_difference
        for fault in (1, 0):
            f.configure("rand", fault=fault)
            render("True displacement field hold/repair " + str(fault))
        f.material.node("surface_output").setInput(
            f.material.node("surface_output").inputNames().index("displacement"), None
        )
        pass_fixture.parm(f.material, "cycles_displacement_method").set("bump")
        pass_fixture.parm(f.rop, "geometry_motion").set("transform")
        f.configure("rand")
        render("Restore ordinary sampled field")
        pass_fixture.parm(f.camera, "tx").set(0.05)
        render("Camera-only reuse preserves sampled fields", retain=True, camera_only=True)
        pass_fixture.parm(f.camera, "tx").set(0)
        session.close(wait=True)
        closed = session.status()
        assert (
            closed["state"] == "closed" and closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0
        ), closed
        report["closed"] = closed
    f.configure("rand", fault=2)
    pass_fixture.parm(f.fields, "temporal_fault").setExpression(
        "2 if round(hou.frame())==3 else 0", hou.exprLanguage.Python
    )
    pass_fixture.set_parameters(
        f.rop,
        dict(
            picture=str(f.dest / "sampled-fields.$F4.exr"),
            persistent_data=1,
            output_precision="float",
            samples=2048,
            ipr_samples=2048,
        ),
    )
    f.rop.render(frame_range=(1, 5, 1))
    summary = f.rop.cachedUserData(final_render._summary_key)
    report["batch_summary"] = summary
    save()
    assert summary["completed_frames"] == 5 and summary["created_sessions"] == 1 and not summary["session_alive"], (
        summary
    )
    catalogue = {row["id"]: row for row in ch._native.pass_catalogue()}
    report["batch"] = []
    for number in range(1, 6):
        hou.setFrame(number)
        f.fault = 2 if number == 3 else 0
        filename = f.dest / ("sampled-fields." + str(number).zfill(4) + ".exr")
        reader = oiio.ImageInput.open(str(filename))
        assert reader is not None, filename
        spec = reader.spec()
        pixels = np.asarray(reader.read_image(format=oiio.FLOAT))
        reader.close()
        assert (spec.width, spec.height) == (f.width, f.height), (spec.width, spec.height)
        batch = dict(frame=number, file=str(filename), channels=list(spec.channelnames))
        report["batch"].append(batch)
        save()
        frame = dict(width=f.width, height=f.height, planes={})
        for name in ("combined", "position"):
            names = [channel if name == "combined" else name + "." + channel for channel in catalogue[name]["channels"]]
            indices = [spec.channelnames.index(channel) for channel in names]
            values = np.ascontiguousarray(pixels[:, :, indices], np.float32)
            frame["planes"][name] = dict(channels=len(indices), pixels=values.tobytes())
        issues = json.loads(spec.get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        batch["diagnostics"] = issues
        save()
        assert bool(issues) == (number == 3), (number, issues)
        if number == 3:
            assert (
                len(issues) == 1
                and issues[0]["code"] == "geometry.shutter_instance_attribute"
                and issues[0]["attribute"] == "rand"
            ), issues
            assert (
                "is missing at a shutter time" in issues[0]["message"]
                and "(4 affected components)" in issues[0]["message"]
            ), issues
            assert "requested-frame" in issues[0]["action"], issues
        images = f.inspect(frame)
        batch.update(images=images, shutter_reference=f.shutter_reference(), diagnostics=issues)
        save()
    report.update(result="passed", batch_frames=5, batch_summary=summary)
except BaseException:
    report.update(
        result="failed",
        error=traceback.format_exc(),
        last_image_check=getattr(f, "last_inspection", None),
        sop_errors={node.path(): list(node.errors()) for node in f.obj.children() if node.errors()},
    )
    raise
finally:
    if session is not None:
        session.close(wait=True)
    registry.close_all()
    save()
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]), batch_frames=report.get("batch_frames"))))
