# SPDX-License-Identifier: Apache-2.0
"""K33: import real pinned Blender materials and compare actual rendered pixels."""

from ramp_curve_parity_gate import pixels_pass

import argparse, json, os, traceback
from pathlib import Path
import hou, numpy as np
import cycles_houdini as ch
from cycles_houdini import blend_materials, native_scene
from cycles_houdini.device_selection import set_devices
from support.paths import artifacts_root

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--device", default="CPU")
args = parser.parse_args()
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
reference = json.loads((root / "tests/fixtures/blender-ramp-curve-gate.json").read_text())
destination = artifacts_root() / "stage3/materials/ramp-curve-blender"
destination.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=args.device, reference=reference["blender_commit"], checks=[])
report["sampling"] = reference["sampling"]
report["houdini_color_policy"] = ch.color_policy()
obj = hou.node("/obj").createNode("geo", "k33_blender_gate", run_init_scripts=False)
stash = obj.createNode("stash", "BLENDER_INPUTS")
camera = hou.node("/obj").createNode("cam", "k33_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
rop = hou.node("/out").createNode("cycles_ipr", "k33_gate")
set_devices(rop, args.device)
rop.setParms(
    dict(
        soppath=stash.path(),
        camera=camera.path(),
        resx=192,
        resy=32,
        samples=1,
        ipr_samples=1,
        use_denoise=0,
        ipr_use_denoise=0,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
    )
)
rop.parmTuple("background").set((0, 0, 0))
rop.parmTuple("lightstrength").set((0, 0, 0))


def geometry(factors):
    g = hou.Geometry()
    # Primitive data is constant over the entire face; point attributes would
    # add barycentric float rounding absent from Blender Object Info Color.
    g.addAttrib(hou.attribType.Prim, "k33_input", (0.0, 0.0, 0.0))
    for i, factor in enumerate(factors):
        face = g.createPolygon()
        for position in ((i - 2, -1, 0), (i - 2, 1, 0), (i - 1, 1, 0), (i - 1, -1, 0)):
            point = g.createPoint()
            point.setPosition(position)
            face.addVertex(point)
        face.setAttribValue("k33_input", (factor,) * 3)
    stash.parm("stash").set(g)
    stash.setDisplayFlag(True)
    stash.setRenderFlag(True)


def render_pixels(session):
    frame = session.wait(session.apply(native_scene.capture(rop)), timeout=120)
    assert frame["samples"] == 1, frame["samples"]
    status = session.status()
    assert not status["error"] and not status["source_diagnostics"], status
    assert status["render_device_ids"] == [args.device], status["render_device_ids"]
    plane = frame["planes"]["combined"]
    image = np.frombuffer(plane["pixels"], np.float32).reshape(32, 192, plane["channels"])
    return image[16, [24, 72, 120, 168], :3].copy(), frame, status


try:
    with ch.Session(
        device=args.device, width=192, height=32, samples=1, execution="worker", threads=2, collect_diagnostics=True
    ) as session:
        for index, case in enumerate(reference["cases"]):
            # The record is exported from the saved .blend by the real product
            # exporter, then imported through the real Houdini material importer.
            record = dict(case["material"], key="k33_" + case["id"], name="k33_" + case["id"])
            diagnostics = []
            paths = blend_materials.import_materials(
                [record], root / "artifacts/ramp-curve-k33", diagnostics=diagnostics
            )
            assert not diagnostics, diagnostics
            material = hou.node(paths[record["key"]])
            # Substitute a constant-per-face attribute for Blender Object Info Color.
            # Both scenes feed the same authored float32 factors to the imported
            # curve/ramp. This keeps the varying shader input under test.
            info = next(n for n in material.children() if n.type().name() == "cycles::object_info::1")
            attribute = material.createNode("cycles::attribute::1", "reference_input")
            attribute.parm("attribute").set("k33_input")
            for wire in tuple(info.outputConnections()):
                assert wire.inputName() == "color", wire.inputName()
                wire.outputNode().setNamedInput(wire.outputName(), attribute, "color")
            geometry(case["factors"])
            rop.parm("materialpath").set(material.path())
            graph = ch.material_graph(material)
            if case["kind"] != "ramp":
                curve = next(n for n in graph["nodes"] if n["definition"] == "cycles/" + case["kind"] + "/1")
                assert curve["parameters"]["curve_source"][1] == 3, "Imported Blender table was not preserved"
                mapping = next(
                    n["properties"]["curve_mapping"] for n in record["nodes"] if "curve_mapping" in n["properties"]
                )
                native_table = json.loads(
                    ch._native.shader_curve_table(
                        curve["parameters"]["curve_source"],
                        {"float_curve": "float", "rgb_curves": "rgb", "vector_curves": "vector"}[case["kind"]],
                    )
                )
                np.testing.assert_array_equal(
                    np.asarray(native_table["table"], np.float32), np.asarray(mapping["table"], np.float32)
                )
            emission = next(n for n in material.children() if n.type().name() == "cycles::emission::1")
            source = next(wire for wire in emission.inputConnections() if wire.outputName() == "color")
            emission.setNamedInput("color", attribute, "color")
            inputs, _, _ = render_pixels(session)
            np.testing.assert_array_equal(
                inputs, np.repeat(np.asarray(case["factors"], np.float32)[:, None], 3, axis=1)
            )
            if index == 0:
                emission.setInput(emission.inputIndex("color"), None)
                emission.parmTuple("color").set((0.6,) * 3)
                neutral, _, _ = render_pixels(session)
                np.testing.assert_array_equal(neutral, np.full((4, 3), 0.6, np.float32))
                report["neutral_emission"] = neutral.tolist()
            emission.setNamedInput("color", source.inputNode(), source.inputName())
            actual, frame, status = render_pixels(session)
            expected = np.asarray(
                case["blender_optix_pixels"] if "OPTIX" in args.device.upper() else case["blender_pixels"], np.float32
            )
            error = float(np.abs(actual - expected).max())
            check = dict(
                case=case["id"],
                max_error=error,
                actual=actual.tolist(),
                blender=expected.tolist(),
                blender_cpu_max_error=float(np.abs(actual - np.asarray(case["blender_pixels"], np.float32)).max()),
                input_pixels=inputs.tolist(),
                samples=frame["samples"],
                render_device_ids=status["render_device_ids"],
                passed=pixels_pass(case, "OptiX" if "OPTIX" in args.device.upper() else "CPU", actual),
            )
            report["checks"].append(check)
        assert all(check["passed"] for check in report["checks"]), report["checks"]
        report.update(result="passed", max_error=max(c["max_error"] for c in report["checks"]))
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    (destination / ("OptiX.json" if "OPTIX" in args.device.upper() else "CPU.json")).write_text(
        json.dumps(report, indent=2)
    )
print(json.dumps(dict(result=report["result"], cases=len(report["checks"]), max_error=report["max_error"])))
