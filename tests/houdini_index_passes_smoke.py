# SPDX-License-Identifier: Apache-2.0
"""Explicit native indices, opacity thresholds, mixed geometry and lossless output."""

from support.paths import artifacts_root
import pass_fixtures as pass_fixture

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import sys
import time
import traceback
import hou
import numpy as np
import OpenImageIO as OIIO
import cycles_houdini as ch
from cycles_houdini import materials, native_scene, object_properties, light_properties

device = sys.argv[sys.argv.index("--device") + 1] if "--device" in sys.argv else "CPU"
destination = artifacts_root() / "stage3/passes/indexes"
destination.mkdir(parents=True, exist_ok=True)
report_path = destination / "houdini-index-passes-smoke.json"
report = dict(result="running", device=device, execution=os.environ.get("CYCLES_HOUDINI_EXECUTION", "sync"), checks=[])


def plane(frame, name):
    p = frame["planes"][name]
    return np.frombuffer(p["pixels"], np.float32).reshape(frame["height"], frame["width"], p["channels"])


def sample(frame, name, x, y):
    # Independently specified orthographic projection: width 4, aspect 1.5.
    return float(plane(frame, name)[int((y / (4 / 1.5) + 0.5) * 64), int((x / 4 + 0.5) * 96), 0])


def check_exr(path, frame):
    image = OIIO.ImageInput.open(str(path))
    assert image
    spec = image.spec()
    pixels = image.read_image(format=OIIO.FLOAT)
    for name in ("object_id", "material_id"):
        offset = spec.channelnames.index(name + ".X")
        assert str(spec.channelformats[offset] if spec.channelformats else spec.format) == "float"
        assert np.array_equal(pixels[:, :, offset], np.flipud(plane(frame, name)[:, :, 0]))
    assert spec.get_string_attribute("compression") in ("zip", "zips")
    image.close()


try:
    scene = hou.node("/obj").createNode("subnet", "index_fixture")
    builders = []
    for name, index in (("rear", 7), ("front", 4), ("mixed", 47), ("packed", 32767)):
        builder = materials.create_material(hou.node("/mat"), "index_" + name, "emission")
        pass_fixture.parm(builder, "cycles_pass_index").set(index)
        builder.node("surface_shader").parmTuple("color").set((0.25, 0.5, 0.75))
        builders.append(builder)
    front_material = builders[1]
    transparent = front_material.createNode("cycles::transparent_bsdf::1")
    blend = front_material.createNode("cycles::mix_closure::1")
    pass_fixture.parm(blend, "fac").set(0.75)
    blend.setInput(blend.inputIndex("closure1"), front_material.node("surface_shader"))
    blend.setInput(blend.inputIndex("closure2"), transparent)
    front_material.node("surface_output").setInput(0, blend)

    def obj(name, index, material):
        node = scene.createNode("geo", name)
        for child in node.children():
            child.destroy()
        object_properties.install(node)
        pass_fixture.parm(node, "cycles_pass_index").set(index)
        pass_fixture.parm(node, "shop_materialpath").set(material.path())
        return node

    rear = obj("rear", 17, builders[0])
    grid = rear.createNode("grid", "rear_plane")
    pass_fixture.set_parameters(grid, dict(rows=2, cols=2))
    grid.parmTuple("size").set((3.8, 2.5))
    pass_fixture.parm(grid, "orient").set(0)
    grid.setRenderFlag(True)
    front = obj("front", 9, builders[1])
    grid = front.createNode("grid", "transparent_plane")
    pass_fixture.set_parameters(grid, dict(rows=2, cols=2))
    grid.parmTuple("size").set((0.8, 0.8))
    pass_fixture.parm(grid, "orient").set(0)
    pass_fixture.set_parameters(front, dict(tx=-0.8, tz=0.5))
    grid.setRenderFlag(True)
    mixed = obj("mixed", 13, builders[2])
    script = mixed.createNode("python", "one_mixed_reference_sop")
    pass_fixture.parm(script, "python").set("""g=hou.pwd().geometry()
g.addAttrib(hou.attribType.Point,"pscale",.11)
g.addAttrib(hou.attribType.Point,"width",.10)
face=g.createPolygon()
for p in ((.25,.2,.5),(.75,.2,.5),(.75,.7,.5),(.25,.7,.5)):
    point=g.createPoint();point.setPosition(p);face.addVertex(point)
curve=g.createPolygon();curve.setIsClosed(False)
for p in ((.5,-.7,.6),(.5,-.15,.6)):
    point=g.createPoint();point.setPosition(p);curve.addVertex(point)
point=g.createPoint();point.setPosition((1.25,-.4,.8))
""")
    script.setRenderFlag(True)
    packed = obj("packed", 23, builders[3])
    box = packed.createNode("box", "prototype")
    box.parmTuple("size").set((0.4, 0.4, 0.4))
    pack = packed.createNode("pack", "packed_box")
    pack.setInput(0, box)
    pack.setRenderFlag(True)
    pass_fixture.set_parameters(packed, dict(tx=1.2, ty=0.65, tz=0.6))
    light = scene.createNode("hlight", "visible_area")
    pass_fixture.set_parameters(
        light, dict(light_type="grid", tx=-1.45, ty=0.8, tz=1, light_contribprimary=1, singlesided=0)
    )
    light.parmTuple("areasize").set((0.3, 0.3))
    light_properties.install(light)
    assert pass_fixture.parm(light, "cycles_light_pass_index") is None
    camera = hou.node("/obj").createNode("cam", "index_camera")
    pass_fixture.set_parameters(camera, dict(tz=5, projection=1, orthowidth=4))
    rop = hou.node("/out").createNode("cycles_ipr", "index_output")
    set_rop_devices(rop, device)
    pass_fixture.set_parameters(
        rop,
        dict(
            source_mode="objects",
            objectroot=scene.path(),
            camera=camera.path(),
            resx=96,
            resy=64,
            samples=64,
            pass_object_id=1,
            pass_material_id=1,
            transparent_background=1,
        ),
    )
    with ch.Session(device=device, width=96, height=64, samples=64, collect_diagnostics=True) as session:

        def render():
            return session.wait(session.apply(native_scene.capture(rop)))

        frame = render()
        converted = session.status()["geometry_conversions"]
        assert sample(frame, "object_id", -0.8, 0) == 17 and sample(frame, "material_id", -0.8, 0) == 7
        for x, y in ((0.5, 0.45), (0.5, -0.4), (1.25, -0.4)):
            assert sample(frame, "object_id", x, y) == 13 and sample(frame, "material_id", x, y) == 47, (x, y)
        assert sample(frame, "object_id", 1.2, 0.65) == 23 and sample(frame, "material_id", 1.2, 0.65) == 32767
        # Analytic light intersections write emission, not surface data. Verify
        # that visible emission leaves the receiving surface's index intact.
        assert plane(frame, "combined")[int((0.8 / (4 / 1.5) + 0.5) * 64), int((-1.45 / 4 + 0.5) * 96), 0] > 1
        assert sample(frame, "object_id", -1.45, 0.8) == 17
        pass_fixture.parm(rop, "pass_alpha_threshold").set(0.1)
        threshold = render()
        assert sample(threshold, "object_id", -0.8, 0) == 9 and sample(threshold, "material_id", -0.8, 0) == 4
        for name in ("object_id", "material_id"):
            data = plane(threshold, name)
            assert np.array_equal(data, np.floor(data))
            metadata = session.frame_info()["pass_metadata"][name]
            assert not metadata["filtered"] and not metadata["color"] and not metadata["denoise_eligible"]
        report["checks"].append(
            "Native surface materials, mixed mesh/curve/sphere-point components and packed object indices; analytic emission leaves surface indices intact; opacity threshold and unfiltered exact integers"
        )
        before = session.diagnostics()
        pass_fixture.parm(mixed, "cycles_pass_index").set(32767)
        pass_fixture.parm(builders[2], "cycles_pass_index").set(123)
        edited = render()
        after = session.diagnostics()
        assert sample(edited, "object_id", 0.5, 0.45) == 32767 and sample(edited, "material_id", 0.5, 0.45) == 123
        assert session.status()["geometry_conversions"] == converted
        assert after["material_builds"] == before["material_builds"] and session.status()["displacement_rebuilds"] == 0
        for invalid in (-1, 32768, True, 1.5, 2**63):
            for update in (
                dict(materials=[dict(key="invalid_index", pass_index=invalid)]),
                dict(
                    geometry=[
                        dict(
                            sop=script.path(),
                            geometry_key="invalid_index",
                            object_key="invalid_index",
                            object_properties=dict(pass_index=invalid),
                        )
                    ]
                ),
            ):
                before_generation = session.status()["generation"]
                try:
                    session.apply(update)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("Invalid Pass Index accepted: " + repr(invalid))
                assert session.status()["generation"] == before_generation
        denoised = session.wait(session.apply(dict(denoising=dict(enabled=True, method="oidn", use_gpu=False))))
        for name in ("object_id", "material_id"):
            assert np.array_equal(plane(edited, name), plane(denoised, name))
        session.write_exr(destination / "indexes.exr")
        check_exr(destination / "indexes.exr", denoised)
        report["checks"].append(
            "Live object/material IDs retain geometry, displacement and shader graphs; malformed values reject before generation changes; denoising leaves IDs raw; exact float32 EXR round trip"
        )
        target = session.apply(dict(denoising=dict(enabled=False), rendering=dict(samples=1048576)))
        session.set_preview_pass("object_id")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            session.status()  # Drive host-side asynchronous capture; frame reads stay read-only.
            info = session.frame_info()
            if info and info["generation"] == target and not info["complete"] and "object_id" in info["passes"]:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("No progressive Object Index preview")
        preview = session.display_image(**{"pass": "object_id"})
        assert np.unique(np.frombuffer(preview["rgba"], np.uint8).reshape(-1, 4)[:, :3], axis=0).shape[0] >= 4
        session.open_native_display("null:")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            session.status()
            info = session.frame_info()
            if set(info["passes"]) == set(info["enabled_passes"]):
                session.present_native()
                break
            time.sleep(0.01)
        else:
            raise AssertionError("Native multi-plane delivery stalled")
        session.close_native_display()
        session.cancel()
        final = render()
        assert session.status()["geometry_conversions"] == converted
        report["checks"].append(
            "Stable false-color progressive previews and native raw index planes; immediate Stop/Render retains resources"
        )
    assert session.status()["owned_geometry_bytes"] == session.status()["device_allocation_bytes"] == 0
    hip = destination / "index-authoring.hiplc"
    hou.hipFile.save(str(hip))
    hou.hipFile.load(str(hip), suppress_save_prompt=True)
    rop = hou.node("/out/index_output")
    pass_fixture.set_parameters(rop, dict(trange=0, picture=str(destination / "native-batch.exr")))
    rop.render(frame_range=(1, 1), ignore_inputs=True)
    check_exr(destination / "native-batch.exr", final)
    report["checks"].append(
        "Native ROP/SOHO batch and HIP reload preserve object and surface-material Pass Index authoring"
    )
    report["result"] = "passed"
except Exception:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(report_path)
