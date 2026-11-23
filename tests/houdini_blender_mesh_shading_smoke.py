# SPDX-License-Identifier: Apache-2.0
"""Compare Blender flat/smooth warped polygon shading through Houdini worker."""

import argparse, json
import copy
from pathlib import Path
import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--out", type=Path, required=True)
parser.add_argument("--allow-mismatch", action="store_true")
parser.add_argument("--device", default="CPU")
parser.add_argument("--neutral-normal-maps", action="store_true")
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
ch.__path__.insert(0, str(root / "houdini/python3.13libs/cycles_houdini"))
from blend_mesh_fixture import direct_request


def pixels(path):
    reader = oiio.ImageInput.open(str(path))
    assert reader
    try:
        names = reader.spec().channelnames
        return reader.read_image(format=oiio.FLOAT)[:, :, [names.index(c) for c in "RGBA"]]
    finally:
        reader.close()


def node(ident, kind, **parameters):
    return dict(id=ident, definition=f"cycles/{kind}/1", parameters=parameters)


def link(a, out, b, inp):
    return {"from": dict(node=a, socket=out), "to": dict(node=b, socket=inp)}


material = dict(
    key="normal",
    kind="graph",
    graph=dict(
        nodes=[
            node("geometry", "geometry"),
            node("half", "vector_math", math_type="scale", scale=0.5),
            node("encode", "vector_math", math_type="add", vector2=[0.5, 0.5, 0.5]),
            node("emission", "emission", strength=1),
        ],
        links=[
            link("geometry", "normal", "half", "vector1"),
            link("half", "vector", "encode", "vector1"),
            link("encode", "vector", "emission", "color"),
        ],
        surface=dict(node="emission", socket="emission"),
    ),
)
obj = hou.node("/obj").createNode("geo", "warped_blender_polygon", run_init_scripts=False)
sop = obj.createNode("attribwrangle")
sop.parm("class").set(0)
camera = hou.node("/obj").createNode("cam")
camera.setParms(dict(tz=5, projection=1, orthowidth=3))
report = dict(cases=[])
with ch.Session(
    device=args.device, width=96, height=96, samples=16, interactive=False, execution="worker", threads=4
) as session:
    for case in json.loads((args.out / "reference.json").read_text())["cases"]:
        kind = case["kind"]
        record = json.loads((args.out / (kind + ".json")).read_text())
        with np.load(args.out / (kind + ".npz"), allow_pickle=False) as data:
            request = direct_request(obj, sop, record, dict(data), "normal")
        subdivision = record.get("subdivision")
        frame = session.wait(
            session.apply(
                dict(geometry=[request], materials=[material], camera_path=camera.path(), background=(0, 0, 0))
            )
        )
        session.write_exr(args.out / ("houdini-" + kind + ".exr"))
        actual = pixels(args.out / ("houdini-" + kind + ".exr"))
        reference = pixels(args.out / (kind + ".exr"))
        # Exclude silhouettes and compare only interior opaque reference pixels.
        mask = reference[:, :, 3] > 0.999
        mask[1:, :] &= reference[:-1, :, 3] > 0.999
        mask[:-1, :] &= reference[1:, :, 3] > 0.999
        mask[:, 1:] &= reference[:, :-1, 3] > 0.999
        mask[:, :-1] &= reference[:, 1:, 3] > 0.999
        assert mask.sum() > 1000, int(mask.sum())
        error = actual[:, :, :3][mask] - reference[:, :, :3][mask]
        row = dict(
            kind=kind,
            pixels=int(mask.sum()),
            rmse=float(np.sqrt(np.mean(error**2))),
            max_error=float(np.max(np.abs(error))),
            tolerance=0.045 if subdivision else 0.008,
            source_diagnostics=session.status()["source_diagnostics"],
        )
        print(json.dumps(row), flush=True)
        report["cases"].append(row)
        if args.neutral_normal_maps:
            assert record["default_uv"], "Neutral normal-map parity requires an authored UV map"
            for basis in ("original", "displaced"):
                mapped = copy.deepcopy(material)
                mapped["graph"]["nodes"][0] = node(
                    "geometry", "normal_map", color=[0.5, 0.5, 1.0], strength=1.0, base=basis
                )
                session.wait(session.apply(dict(materials=[mapped])))
                mapped_path = args.out / ("houdini-" + kind + "-neutral-" + basis + ".exr")
                session.write_exr(mapped_path)
                values = pixels(mapped_path)
                difference = values[:, :, :3][mask] - actual[:, :, :3][mask]
                normal_row = dict(
                    kind=kind,
                    basis=basis,
                    pixels=int(mask.sum()),
                    rmse=float(np.sqrt(np.mean(difference**2))),
                    max_error=float(np.max(np.abs(difference))),
                    tolerance=0.0001,
                )
                print(json.dumps(normal_row), flush=True)
                report["cases"].append(normal_row)
report["result"] = "passed" if all(c["rmse"] < c["tolerance"] for c in report["cases"]) else "mismatch"
(args.out / "comparison.json").write_text(json.dumps(report, indent=2))
assert args.allow_mismatch or report["result"] == "passed", report
