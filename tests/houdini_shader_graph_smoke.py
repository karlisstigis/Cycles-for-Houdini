# SPDX-License-Identifier: Apache-2.0
"""Owned surface graphs: independent radiance, linking, edits and rollback."""

from support.paths import artifacts_root
from native_test_runtime import runtime_executable
from array import array
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

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/materials/graphs"
destination.mkdir(parents=True, exist_ok=True)
obj = hou.node("/obj").createNode("geo", "graph_receiver", run_init_scripts=False)
grid = obj.createNode("grid")
grid.parm("type").set(0)
grid.parm("orient").set(0)
grid.parm("rows").set(2)
grid.parm("cols").set(2)
grid.parmTuple("size").set((8, 8))
color = obj.createNode("color")
color.setInput(0, grid)
color.parmTuple("color").set((0.2, 0.4, 0.8))
camera = hou.node("/obj").createNode("cam", "graph_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)


def node(id, type, **parameters):
    return dict(id=id, definition=f"cycles/{type}/1", parameters=parameters)


def endpoint(node, socket):
    return dict(node=node, socket=socket)


def link(a, output, b, input):
    return dict(**{"from": endpoint(a, output), "to": endpoint(b, input)})


def material(nodes, links=(), surface=("emission", "emission")):
    return dict(
        key="surface",
        kind="graph",
        graph=dict(schema_version=1, nodes=nodes, links=list(links), surface=endpoint(*surface)),
    )


def arithmetic():
    return material(
        [
            node("color", "color", value=[0.2, 0.4, 0.8]),
            node("value", "value", value=1.0),
            node("multiply", "math", math_type="multiply", value2=2, use_clamp=False),
            node("emission", "emission"),
        ],
        [
            link("color", "color", "emission", "color"),
            link("value", "value", "multiply", "value1"),
            link("multiply", "value", "emission", "strength"),
        ],
    )


def rgb(frame):
    values = array("f")
    values.frombytes(frame["planes"]["combined"]["pixels"])
    offset = (32 * 64 + 32) * 4
    result = list(values[offset : offset + 3])
    assert all(math.isfinite(value) for value in result)
    return result


def expect(frame, expected, tolerance=0.001):
    actual = rgb(frame)
    assert max(abs(a - b) for a, b in zip(actual, expected)) < tolerance, (actual, expected)
    return actual


def reject(session, candidate, message, **other):
    before = session.status()
    info = session.frame_info()
    generation = before.get("committed_generation", before["generation"])
    try:
        session.wait(session.apply(dict(materials=[candidate], **other)), timeout=15)
    except (ValueError, RuntimeError) as error:
        assert message in str(error), (message, str(error))
    else:
        raise AssertionError("Invalid graph was accepted: " + message)
    after = session.status()
    assert after.get("committed_generation", after["generation"]) == generation
    assert session.frame_info()["generation"] == info["generation"]
    assert after["geometry_resources"] == before["geometry_resources"]
    assert after["object_resources"] == before["object_resources"]


reports = []
for device in ("CPU", ch.default_device()):
    label = "cpu" if device == "CPU" else "optix"
    checks = []
    capabilities = ch.capabilities()
    catalogue = ch.shader_catalogue()
    assert set(capabilities["surface_graph_definitions"]) == {
        item["id"] for item in catalogue["definitions"] if item["surface_graph_adapter"]
    }
    assert (
        capabilities["native_material_vops"]
        and capabilities["graph_volume_output"]
        and capabilities["graph_displacement_output"]
    )
    request = ch.geometry_request(obj, color, material="surface")
    with ch.Session(device=device, width=64, height=64, samples=32, collect_diagnostics=True) as session:
        graph = arithmetic()
        frame = session.wait(
            session.apply(dict(geometry=[request], materials=[graph], camera_path=camera.path(), background=(0, 0, 0)))
        )
        actual = expect(frame, (0.4, 0.8, 1.6))
        checks.append(dict(name="Typed color/value/math links produce independent HDR radiance", measured=actual))
        session.write_exr(destination / (label + "-graph.exr"))
        reference = destination / (label + "-reference.exr")
        run = subprocess.run(
            [
                str(runtime_executable("ch_harness.exe")),
                "--resources",
                os.environ["CYCLES_HOUDINI_RESOURCES"],
                "--device",
                device,
                "--fixture",
                "shader-reference",
                "--width",
                "64",
                "--height",
                "64",
                "--samples",
                "32",
                "--output",
                str(reference),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert run.returncode == 0, run.stderr
        image = OIIO.ImageInput.open(str(reference))
        assert image, OIIO.geterror()
        pixels = image.read_image(format=OIIO.FLOAT)
        names = list(image.spec().channelnames)
        image.close()
        assert pixels is not None
        for index, name in enumerate(("R", "G", "B")):
            assert np.max(np.abs(pixels[8:-8, 8:-8, names.index(name)] - (0.4, 0.8, 1.6)[index])) < 0.001
        checks.append(dict(name="Independent headless uniform-material reference", result="passed"))
        initial = session.status()
        builds = session.diagnostics()["material_builds"]
        camera.parm("tx").set(0.05)
        expect(session.wait(session.apply(dict(materials=[graph], camera_path=camera.path()))), (0.4, 0.8, 1.6))
        assert session.diagnostics()["material_builds"] == builds
        assert "svm_nodes" not in session.diagnostics()["buffer_copy_requests"]
        assert session.status()["geometry_conversions"] == initial["geometry_conversions"]
        checks.append(dict(name="Exact graph no-op and camera edit retain shader/geometry"))
        camera.parm("tx").set(0)
        defaults = material([node("emission", "emission")])
        expect(session.wait(session.apply(dict(materials=[defaults], camera_path=camera.path()))), (8, 8, 8))
        checks.append(dict(name="Constructed engine defaults preserved independently of convenience-material defaults"))
        for type, expected in (("mix_closure", (0.75, 0, 0.25)), ("add_closure", (1, 0, 1))):
            params = {"fac": 0.25} if type == "mix_closure" else {}
            combined = material(
                [
                    node("red", "emission", color=(1, 0, 0), strength=1.0),
                    node("blue", "emission", color=(0, 0, 1), strength=1.0),
                    node("combine", type, **params),
                ],
                [link("red", "emission", "combine", "closure1"), link("blue", "emission", "combine", "closure2")],
                surface=("combine", "closure"),
            )
            expect(session.wait(session.apply(dict(materials=[combined]))), expected)
            checks.append(dict(name="Independent " + type + " emission integral"))
        attr = material(
            [node("attribute", "attribute", attribute="Cd"), node("emission", "emission", strength=1.0)],
            [link("attribute", "color", "emission", "color")],
        )
        expect(session.wait(session.apply(dict(materials=[attr]))), (0.2, 0.4, 0.8))
        assert session.status()["geometry_conversions"] == initial["geometry_conversions"]
        assert "attributes_float3" in session.diagnostics()["buffer_copy_requests"]
        checks.append(dict(name="New Cd demand uploads retained geometry attributes"))
        diffuse = material(
            [node("diffuse", "diffuse_bsdf", color=(0.6, 0.3, 0.15), roughness=0.0)], surface=("diffuse", "BSDF")
        )
        light = dict(
            key="key",
            kind="point",
            radius=0.0,
            strength=(1000, 1000, 1000),
            transform=(1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 4),
        )
        frame = session.wait(session.apply(dict(materials=[diffuse], lights=[light])))
        expected = [1000 * value / (4 * math.pi**2 * 16) for value in (0.6, 0.3, 0.15)]
        expect(frame, expected, tolerance=0.003)
        checks.append(dict(name="Independent graph Lambertian irradiance", measured=rgb(frame), expected=expected))
        plain = dict(key="surface", kind="principled", color=(0.3, 0.4, 0.5), roughness=0.4, metallic=0.2)
        expected = rgb(session.wait(session.apply(dict(materials=[plain]))))
        principled = material(
            [node("principled", "principled_bsdf", base_color=(0.3, 0.4, 0.5), roughness=0.4, metallic=0.2)],
            surface=("principled", "BSDF"),
        )
        expect(session.wait(session.apply(dict(materials=[principled]))), expected, tolerance=0.01)
        checks.append(
            dict(name="Principled graph matches existing engine convenience material", measured=rgb(session.frame()))
        )
        session.wait(session.apply(dict(materials=[arithmetic()], deleted_lights=["key"])))

        invalid = []
        bad = arithmetic()
        bad["graph"]["nodes"][0]["definition"] = "cycles/color/2"
        invalid.append((bad, "definition version"))
        # IES now has an authored photometric adapter. Its resource identity
        # stays private; callers cannot forge the engine's prepared revision.
        ies = next(item for item in catalogue["definitions"] if item["engine_type"] == "ies_light")
        assert ies["surface_graph_adapter"] and ies["volume_graph_adapter"]
        bad = arithmetic()
        bad["graph"]["nodes"][0] = node("color", "ies_light", resource_revision_high=1)
        invalid.append((bad, "Unknown/internal"))
        bad = arithmetic()
        bad["graph"]["nodes"][1]["id"] = "color"
        invalid.append((bad, "Duplicate shader node"))
        bad = arithmetic()
        bad["graph"]["nodes"][2]["parameters"]["math_type"] = "imaginary"
        invalid.append((bad, "Unknown shader enum"))
        bad = arithmetic()
        bad["graph"]["nodes"][2]["parameters"]["math_type"] = 2
        invalid.append((bad, "type mismatch"))
        bad = arithmetic()
        bad["graph"]["nodes"][0]["parameters"]["value"] = True
        invalid.append((bad, "type mismatch"))
        bad = arithmetic()
        bad["graph"]["nodes"][0]["parameters"]["value"] = (math.nan, 0, 0)
        invalid.append((bad, "Nonfinite shader vector"))
        bad = arithmetic()
        bad["graph"]["nodes"][1]["parameters"]["value"] = math.inf
        invalid.append((bad, "Nonfinite shader parameter"))
        bad = arithmetic()
        bad["graph"]["nodes"][1]["parameters"]["value"] = 2**80
        invalid.append((bad, "int32 range"))
        bad = arithmetic()
        bad["graph"]["nodes"][1]["id"] = "value\0suffix"
        invalid.append((bad, "Embedded NUL"))
        bad = arithmetic()
        bad["graph"]["nodes"][0]["parameters"]["unknown"] = 1.0
        invalid.append((bad, "Unknown/internal"))
        bad = arithmetic()
        bad["graph"]["nodes"][3]["parameters"]["surface_mix_weight"] = 1.0
        invalid.append((bad, "Unknown/internal"))
        for type in ("tangent", "normal_map"):
            bad = arithmetic()
            bad["graph"]["nodes"][0] = node("color", type, context_tangent_bases=1)
            invalid.append((bad, "Unknown/internal"))
        bad = arithmetic()
        bad["graph"]["links"].append(deepcopy(bad["graph"]["links"][0]))
        invalid.append((bad, "Duplicate shader input"))
        bad = arithmetic()
        bad["graph"]["links"][1] = link("multiply", "value", "multiply", "value1")
        invalid.append((bad, "Cyclic shader graph"))
        bad = arithmetic()
        bad["graph"]["links"][0]["from"]["node"] = "missing"
        invalid.append((bad, "Missing shader node"))
        bad = arithmetic()
        bad["graph"]["links"][0]["from"]["socket"] = "missing"
        invalid.append((bad, "Missing shader output"))
        bad = arithmetic()
        bad["graph"]["links"][1]["to"] = endpoint("value", "value")
        invalid.append((bad, "nonlinkable"))
        bad = arithmetic()
        bad["graph"]["links"][0] = link("emission", "emission", "multiply", "value3")
        invalid.append((bad, "explicit type conversion"))
        preview = arithmetic()
        preview["graph"]["surface"] = endpoint("value", "value")
        expect(session.wait(session.apply(dict(materials=[preview]))), (1, 1, 1))
        checks.append(dict(name="Numeric Surface output uses the engine emission conversion"))
        bad = arithmetic()
        bad["graph"]["volume"] = endpoint("value", "value")
        invalid.append((bad, "requires a closure"))
        bad = arithmetic()
        bad["graph"]["displacement"] = endpoint("value", "value")
        invalid.append((bad, "requires a float3 vector"))
        bad = arithmetic()
        bad["kind"] = "diffuse"
        invalid.append((bad, "other material kinds"))
        bad = dict(key="surface", kind="graph")
        invalid.append((bad, "require a graph"))
        bad = arithmetic()
        bad["graph"]["schema_version"] = 2
        invalid.append((bad, "schema version"))
        bad = arithmetic()
        bad["graph"]["schema_version"] = True
        invalid.append((bad, "requires an integer"))
        bad = arithmetic()
        bad["graph"]["links"] = bad["graph"]["links"] * 700
        invalid.append((bad, "2048 links"))
        bad = arithmetic()
        bad["graph"]["nodes"] = bad["graph"]["nodes"] * 65
        invalid.append((bad, "1..256 nodes"))
        for bad, message in invalid:
            reject(session, bad, message, deleted_objects=[request["object_key"]])
        checks.append(dict(name="Malformed graphs rejected before deletion/live mutation", cases=len(invalid)))
        # A rejected worker snapshot leaves the accepted render intact, while
        # its desired source state can still include the requested deletion.
        # Repair the complete source, as native recapture does, before testing
        # further partial material edits.
        expect(session.frame(), (1, 1, 1))
        expect(session.wait(session.apply(dict(geometry=[request], materials=[arithmetic()]))), (0.4, 0.8, 1.6))
        stable_geometry = session.status()["geometry_conversions"]
        checks.append(dict(name="Complete repaired source restores its requested object after strict rejection"))
        graph = arithmetic()
        allocations = []
        for repetition in range(40):
            graph["graph"]["nodes"][0]["parameters"]["value"] = [0.3 if repetition % 2 else 0.2, 0.4, 0.8]
            frame = session.wait(session.apply(dict(materials=[graph])))
            expect(frame, (0.6 if repetition % 2 else 0.4, 0.8, 1.6))
            allocations.append(session.status()["device_allocation_bytes"])
        assert len(set(allocations[10:])) == 1, allocations
        assert session.status()["geometry_conversions"] == stable_geometry
        assert session.diagnostics()["shader_graph_preparation_seconds"] > 0
        checks.append(
            dict(
                name="In-place graph edits retain owned definitions and stable warm tracked memory",
                allocations=allocations,
            )
        )
        session.wait(
            session.apply(
                dict(
                    deleted_objects=[request["object_key"]],
                    deleted_geometry=[request["geometry_key"]],
                    deleted_materials=["surface"],
                )
            )
        )
        assert session.status()["material_resources"] == 0 and session.status()["owned_geometry_bytes"] == 0
        checks.append(dict(name="Graph material and geometry retirement"))
    assert session.status()["device_allocation_bytes"] == 0
    reports.append(dict(device=device, checks=checks, tracked_after_close=0))

report = dict(
    result="passed",
    devices=reports,
    scope="Owned surface graph API and selected rendered semantics; native VOP certification is recorded separately. Textures, ramps, volume/displacement and full node certification remain separate gates.",
)
(destination / "houdini-shader-graph-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(
    json.dumps(
        dict(
            result="passed",
            checks_per_device=[len(row["checks"]) for row in reports],
            invalid_graphs_per_device=len(invalid),
        )
    )
)
