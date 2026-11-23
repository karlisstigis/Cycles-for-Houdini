# SPDX-License-Identifier: Apache-2.0
"""Author actual native VOPs from the independent Blender export fixture."""

from support.paths import artifacts_root

from pathlib import Path
import argparse
import json
import sys
import hou
import numpy as np
import cycles_houdini as ch

root = Path(__file__).resolve().parents[1]
ch.__path__.insert(0, str(root / "houdini/python3.13libs/cycles_houdini"))
from cycles_houdini import blend_materials, materials
from blend_mesh_fixture import direct_request

destination = artifacts_root() / "blend-material-transfer"
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--out", type=Path, default=destination)
args = parser.parse_args()
args.out.mkdir(parents=True, exist_ok=True)
record = json.loads((destination / "materials.json").read_text())
diagnostics = []
paths = blend_materials.import_materials(record["materials"], destination, diagnostics=diagnostics)
assert len(paths) == 3 and not diagnostics, diagnostics
material = hou.node(paths[record["materials"][0]["key"]])
graph = materials.material_graph(material)
assert graph["surface"] and any(n["definition"] == "cycles/image_texture/1" for n in graph["nodes"]), graph
assert any(n["definition"] == "cycles/uvmap/1" for n in graph["nodes"])
assert any(
    n["definition"] == "cycles/math/1" and n["parameters"].get("math_type") == "multiply" for n in graph["nodes"]
)
assert any(n["definition"] == "cycles/owned_ramp/1" for n in graph["nodes"])
image = next(n for n in graph["nodes"] if n["definition"] == "cycles/image_texture/1")
assert Path(image["parameters"]["filename"]).is_file()
assert image["parameters"]["colorspace"] not in ("auto", "")
assert material.userData("cycles_external_blend") == "1"
assert any(
    n["parameters"].get("attribute") == "__cycles_blender_alpha_" + hou.text.encodeAttrib("Color Map.!")
    for n in graph["nodes"]
)
assert any(n["parameters"].get("layer_name") == "__cycles_blender_default_color" for n in graph["nodes"])
assert image["parameters"]["filename"]
uv = next(n for n in graph["nodes"] if n["definition"] == "cycles/uvmap/1")
assert uv["parameters"]["attribute"] == hou.text.encodeAttrib("UV Map.!")
mesh = json.loads((destination / "mesh.json").read_text())
with np.load(destination / "mesh.npz", allow_pickle=False) as arrays:
    arrays = dict(arrays)
obj = hou.node("/obj").createNode("geo", "direct_blender_attribute_fixture", run_init_scripts=False)
sop = obj.createNode("attribwrangle")
sop.parm("class").set(0)
camera = hou.node("/obj").createNode("cam")
camera.setParms(dict(tx=0.5, ty=0.5, tz=3, projection=1, orthowidth=1.5))


def probe(kind, parameters, output):
    return dict(
        key="attribute_probe",
        kind="graph",
        graph=dict(
            nodes=[
                dict(id="field", definition="cycles/" + kind + "/1", parameters=parameters),
                dict(id="emission", definition="cycles/emission/1", parameters=dict(strength=1.0)),
            ],
            links=[{"from": dict(node="field", socket=output), "to": dict(node="emission", socket="color")}],
            surface=dict(node="emission", socket="emission"),
        ),
    )


images = {}
checks = []
with ch.Session(
    device="CPU", width=48, height=48, samples=8, execution="worker", interactive=False, threads=4
) as session:
    request = direct_request(obj, sop, mesh, arrays, "attribute_probe")
    for name, kind, parameters, socket, expected in (
        ("color", "attribute", dict(attribute=hou.text.encodeAttrib("Color Map.!")), "color", (0.2, 0.4, 0.6)),
        (
            "alpha",
            "attribute",
            dict(attribute="__cycles_blender_alpha_" + hou.text.encodeAttrib("Color Map.!")),
            "fac",
            (0.35,) * 3,
        ),
        ("default_color", "attribute", dict(attribute="__cycles_blender_default_color"), "color", (0.2, 0.4, 0.6)),
        ("named_uv", "uvmap", dict(attribute=hou.text.encodeAttrib("UV Map.!")), "UV", None),
        ("default_uv", "texture_coordinate", {}, "UV", None),
    ):
        frame = session.wait(
            session.apply(
                dict(
                    geometry=[request],
                    materials=[probe(kind, parameters, socket)],
                    camera_path=camera.path(),
                    background=(0, 0, 0),
                    transparent_background=True,
                )
            )
        )
        plane = frame["planes"]["combined"]
        image = np.frombuffer(plane["pixels"], np.float32).reshape(48, 48, plane["channels"]).copy()
        visible = image[:, :, 3] > 0.999
        assert visible.sum() > 100, (name, int(visible.sum()))
        if expected is not None:
            assert np.allclose(image[:, :, :3][visible], expected, atol=2e-5), (
                name,
                image[:, :, :3][visible].mean(axis=0),
            )
        images[name] = image
        checks.append(dict(name=name, pixels=int(visible.sum()), mean=image[:, :, :3][visible].mean(axis=0).tolist()))
    assert np.allclose(images["named_uv"], images["default_uv"], atol=2e-5), "Default UV differs from named Blender UV"
    visible = images["named_uv"][:, :, 3] > 0.999
    assert np.ptp(images["named_uv"][:, :, 0][visible]) > 0.2, "UV probe did not interpolate varying corner data"
(args.out / "attribute-checks.json").write_text(
    json.dumps(dict(result="passed", checks=checks, uv=uv["parameters"]["attribute"]))
)
(args.out / "imported-graph.json").write_text(json.dumps(graph, indent=2))
print("BLEND_MATERIAL_IMPORT_PASSED", paths)
