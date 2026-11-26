# SPDX-License-Identifier: Apache-2.0
"""Native shader output previews retain adaptive dicing and true displacement."""

import argparse
import json
import traceback

import hou
import numpy as np

from cycles_houdini import native_scene, network_preview, registry
from cycles_houdini.scene import tessellation
from shader_bypass_preview_fixture import Fixture
from subdivision_test_setup import configure_render


def run(device, report, mode, *, test_host_path):
    f = Fixture(device)
    f.rop.setParms(dict(persistent_data=1, tessellation_update=int(mode == "automatic")))
    configure_render(
        f.rop,
        dict(
            pass_position=1,
            pass_normal=1,
            subdivision=dict(type="catmull_clark", space="pixel", dicing_rate=2, max_level=6),
        ),
    )
    f.emission.parmTuple("color").set((0.12, 0.42, 0.72))
    f.output.setNamedInput("surface", f.emission, "emission")
    noise = f.builder.createNode("cycles::noise_texture::1", "shared_noise")
    noise.parm("scale").set(1.5)
    coordinates = f.builder.createNode("cycles::texture_coordinate::1", "coordinates")
    noise.setNamedInput("vector", coordinates, "object")
    generated = f.builder.createNode("cycles::noise_texture::1", "surface_generated_noise")
    generated.parm("scale").set(8.0)
    displacement = f.builder.createNode("cycles::displacement::1", "height")
    displacement.setParms(dict(midlevel=0, scale=0.55, space="object"))
    displacement.setNamedInput("height", noise, "fac")
    f.output.setNamedInput("displacement", displacement, "displacement")
    f.builder.parm("cycles_displacement_method").set("both")
    curve = f.builder.createNode("cycles::float_curve::1", "preview_curve")
    curve.parm("value").set(0.64)

    config = native_scene.configuration(f.rop)
    entry = registry.for_rop(f.rop, config)
    entry.session.close()
    entry.session = registry.session_from_configuration(config, collect_diagnostics=True)
    counts = (
        "geometry_conversions",
        "subdivision_control_faces",
        "subdivision_triangles",
        "subdivision_tessellations",
        "displacement_rebuilds",
    )
    previous = 0
    camera_transform = f.camera.worldTransform().asTuple()

    def planes(frame):
        return {
            name: np.frombuffer(frame["planes"][name]["pixels"], np.float32).reshape(64, 64, -1).copy()
            for name in ("combined", "position", "normal")
        }

    def render(label, *, refresh=False, path=None):
        nonlocal previous
        assert f.camera.worldTransform().asTuple() == camera_transform
        now = hou.time()
        if refresh:
            actual = "refresh"
            generation = tessellation.refresh(f.rop)
        else:
            update = None
            if test_host_path and path is not None:
                camera = native_scene.capture(f.rop, now, camera_only=True)
                update = entry.narrow_update(now, camera["motion"], camera)
            if update is None:
                update = native_scene.capture(f.rop, now)
            actual = update.get("_narrow", "full")
            if test_host_path and path is not None:
                assert actual == path, (label, path, entry.narrow_reason)
                if path == "materials":
                    assert "geometry" not in update and "dicing" not in update, label
            generation = entry.apply(update, now, change_serial=entry.change_serial)
        frame = entry.session.wait(generation, timeout=60)
        status = entry.session.status()
        if entry.observer:
            entry.observer.refresh_material_bindings(entry.session)
        assert status["generation"] == generation and generation > previous, status
        previous = generation
        assert status["state"] == "complete" and status["render_device_ids"] == [device], status
        assert not status["source_diagnostics"], status
        rendered = planes(frame)
        assert all(np.isfinite(values).all() for values in rendered.values()), label
        report["checks"].append(
            dict(name=label, generation=generation, path=actual, **{key: status[key] for key in counts})
        )
        return rendered, status

    def match_fresh(rendered, *, diced=None):
        with registry.session_from_configuration(config) as fresh:
            update = native_scene.capture(f.rop)
            update["dicing"].update(update=mode != "cold" if diced is None else diced, refresh_revision=0)
            reference = planes(fresh.wait(fresh.apply(update), timeout=60))
        for name in rendered:
            np.testing.assert_allclose(rendered[name], reference[name], rtol=0, atol=3e-6, err_msg=name)

    report["execution"] = entry.session.execution
    assert entry.session.execution == "worker", entry.session.execution
    original, initial = render("Original emission with shared procedural displacement")
    if mode == "refresh":
        original, initial = render("Explicit refresh with adaptive updates off", refresh=True)
    assert initial["subdivision_control_faces"] > 0 and initial["displacement_rebuilds"] > 0, initial
    if mode == "cold":
        assert initial["subdivision_tessellations"] == 0 and initial["subdivision_triangles"] == 2, initial
    else:
        assert initial["subdivision_tessellations"] > 0 and initial["subdivision_triangles"] > 2, initial
    center = np.s_[16:48, 16:48]
    assert np.max(np.abs(original["position"][center][..., 2])) > 0.1, "The fixture must be displaced"
    assert np.all(original["combined"][center][..., 3] == 1)
    match_fresh(original)

    for node, socket, expected in (
        (noise, "fac", None),
        (noise, "color", None),
        (generated, "fac", None),
        (f.value, "value", (0.3,) * 3),
        (f.color, "color", (0.2, 0.4, 0.8)),
        (curve, "value", (0.64,) * 3),
        (f.emission, "emission", (0.12, 0.42, 0.72)),
    ):
        selected = network_preview.connect_to_output(node)
        assert node.outputNames()[selected] == socket, (node.path(), selected, socket)
        assert f.output.input(f.output.inputNames().index("displacement")) == displacement
        rendered, status = render("Preview " + node.name() + "." + socket, path="materials")
        rgb = rendered["combined"][center][..., :3]
        if expected is None:
            assert np.ptp(rgb[..., 0]) > 0.08, socket
            if socket == "fac":
                np.testing.assert_allclose(rgb[..., 0], rgb[..., 1], rtol=0, atol=1e-6)
            else:
                assert np.max(np.abs(rgb[..., 0] - rgb[..., 1])) > 0.08, socket
        else:
            np.testing.assert_allclose(rgb, np.broadcast_to(expected, rgb.shape), rtol=0, atol=2e-5)
        for name in ("position", "normal"):
            np.testing.assert_allclose(rendered[name], original[name], rtol=0, atol=3e-6, err_msg=name)
        match_fresh(rendered)
        assert all(status[key] == initial[key] for key in counts), report["checks"]

    f.emission.parmTuple("color").set((0.32, 0.52, 0.22))
    rendered, status = render("Actual Surface-only color edit", path="materials")
    np.testing.assert_allclose(rendered["position"], original["position"], rtol=0, atol=3e-6)
    match_fresh(rendered)
    assert all(status[key] == initial[key] for key in counts), report["checks"]

    # Enable automatic updates for the displacement edit in every case so the same
    # positive control has to produce a new, fully diced mesh.
    f.rop.parm("tessellation_update").set(1)
    displacement.parm("scale").set(0.95)
    rendered, status = render("Actual edit of the Displacement scale", path="full")
    assert status["subdivision_tessellations"] == initial["subdivision_tessellations"] + 1, report["checks"]
    assert status["displacement_rebuilds"] == initial["displacement_rebuilds"] + 1, report["checks"]
    assert status["geometry_conversions"] == initial["geometry_conversions"], report["checks"]
    distance = float(np.max(np.abs(rendered["position"][center] - original["position"][center])))
    assert distance > 0.04, (mode, distance)
    match_fresh(rendered, diced=True)


if __name__ == "__main__":
    from support.paths import artifact_path

    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="CPU")
    parser.add_argument("--full-capture", action="store_true", help="Verify native reuse even after a complete read")
    args = parser.parse_args()
    report = dict(result="running", device=args.device, houdini=hou.applicationVersionString(), cases=[])
    destination = artifact_path("stage3", "materials", "preview-displacement.json")
    try:
        for mode in ("cold", "refresh", "automatic"):
            case = dict(mode=mode, checks=[])
            report["cases"].append(case)
            run(args.device, case, mode, test_host_path=not args.full_capture)
            registry.close_all()
            hou.hipFile.clear(suppress_save_prompt=True)
        report["result"] = "passed"
    except BaseException:
        report.update(result="failed", error=traceback.format_exc())
        raise
    finally:
        registry.close_all()
        destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(destination)
