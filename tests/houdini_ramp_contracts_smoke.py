# SPDX-License-Identifier: Apache-2.0
"""Owned ramp bounds and worker-side source recovery preserve independent displacement."""

from ramp_table_reference import native_lookup
from support.paths import artifacts_root
import argparse, json, os, struct, traceback
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini.ramps import lower_ramp, BASES

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
dest = artifacts_root() / "stage3/materials/ramp-contracts"
dest.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=device, checks=[])
obj = hou.node("/obj").createNode("geo", "receiver", run_init_scripts=False)
grid = obj.createNode("grid")
grid.setParms(dict(type=0, orient=0, rows=2, cols=2))
grid.parmTuple("size").set((8, 8))
camera = hou.node("/obj").createNode("cam")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
f32 = lambda v: struct.unpack("f", struct.pack("f", v))[0]
bases = (
    hou.rampBasis.Constant,
    hou.rampBasis.Linear,
    hou.rampBasis.CatmullRom,
    hou.rampBasis.MonotoneCubic,
    hou.rampBasis.Bezier,
    hou.rampBasis.BSpline,
    hou.rampBasis.Hermite,
)
request = ch.geometry_request(obj, grid, material="controls", material_mode="uniform")


def graph(tokens, keys, values, color=False, space="rgb", displacement=False):
    nodes, links, output = lower_ramp(
        "controls", tokens, keys, values, color, factor_value=f32(0.425), color_space=space
    )
    result = dict(schema_version=1, nodes=nodes, links=links, surface=output)
    if displacement:
        nodes.append(
            dict(
                id="independent_displacement",
                definition="cycles/displacement/1",
                parameters=dict(height=0.25, midlevel=0.0, scale=1.0, space="object"),
            )
        )
        result.update(
            displacement=dict(node="independent_displacement", socket="displacement"), displacement_method="true"
        )
    return result


try:
    with ch.Session(device=device, width=64, height=32, samples=1, passes=["combined", "position"]) as session:

        def render(label, source, expected, warning=False):
            frame = session.wait(
                session.apply(
                    dict(
                        geometry=[request],
                        camera_path=camera.path(),
                        background=(0, 0, 0),
                        materials=[dict(key="controls", kind="graph", graph=source)],
                    )
                )
            )
            status = session.status()
            planes = {
                name: np.frombuffer(v["pixels"], np.float32).reshape(32, 64, v["channels"])
                for name, v in frame["planes"].items()
            }
            np.testing.assert_allclose(planes["combined"][16, 32, :3], expected, atol=3e-5, rtol=3e-5)
            assert bool(status["source_diagnostics"]) == warning, status["source_diagnostics"]
            report["checks"].append(dict(label=label, status=status))
            return planes, status

        for count in (1, 32):
            keys = tuple(f32(i / (count - 1)) if count > 1 else 0.5 for i in range(count))
            values = tuple(f32(0.2 + (i % 5) * 0.2) for i in range(count))
            for token, basis in zip(BASES, bases):
                native = hou.Ramp((basis,) * count, keys, values)
                render(
                    str(count) + " keys / " + token,
                    graph((token,) * count, keys, values),
                    (native_lookup(native, f32(0.425)),) * 3,
                )
        keys = tuple(map(f32, (0.05, 0.12, 0.3, 0.45, 0.67, 0.85, 0.95)))
        v = (0.2, 1.3, -0.4, 0.7, 2.1, -0.3, 0.9)
        bad_values = tuple(tuple(f32(v[(i + n * 2) % 7]) for n in range(3)) for i in range(7))
        bad = graph(
            tuple("constant" if i % 2 else "monotone_cubic" for i in range(7)), keys, bad_values, True, "hsv", True
        )
        # Previously an exact polynomial fit could reject this finite native
        # HSV input for precision/capacity. Host sampling has no such limit.
        import copy

        legacy = copy.deepcopy(bad)
        source = next(n["parameters"]["source"] for n in legacy["nodes"] if n["definition"] == "cycles/owned_ramp/1")
        source[1] = 1
        source[5] = 0
        for i in range(7):
            source[8 + 5 * i] = 0 if i % 2 else 3
        legacy_reference = hou.Ramp(
            tuple(hou.rampBasis.Constant if i % 2 else hou.rampBasis.MonotoneCubic for i in range(7)), keys, bad_values
        )
        legacy_reference.setColorType(hou.colorType.HSV)
        render(
            "Previously capacity-limited native input now samples successfully",
            legacy,
            native_lookup(legacy_reference, f32(0.425)),
        )
        # The original v2 authored hue source has negative colors and remains a
        # genuine semantic failure, independent of table precision/capacity.
        good = graph(("catmull_rom",) * 2, (0.0, 1.0), ((0.2, 0.4, 0.8), (0.8, 0.2, 0.1)), True, "rgb", True)
        reference = hou.Ramp((hou.rampBasis.CatmullRom,) * 2, (0.0, 1.0), ((0.2, 0.4, 0.8), (0.8, 0.2, 0.1)))
        image, before = render("Independent displacement", good, native_lookup(reference, f32(0.425)))
        position = image["position"][16, 32, :3].copy()
        assert abs(abs(position[2]) - 0.25) < 1e-5, position
        for cycle in range(3):
            image, status = render("Unsafe authored hue colors " + str(cycle), bad, (1, 0, 1), True)
            assert {d["code"] for d in status["source_diagnostics"]} == {"material.ramp_unavailable"}, status[
                "source_diagnostics"
            ]
            np.testing.assert_allclose(image["position"][16, 32, :3], position, atol=1e-6, rtol=0)
            assert status["geometry_conversions"] == before["geometry_conversions"], status
            image, status = render("Repair " + str(cycle), good, native_lookup(reference, f32(0.425)))
            np.testing.assert_allclose(image["position"][16, 32, :3], position, atol=1e-6, rtol=0)
            assert status["geometry_conversions"] == before["geometry_conversions"], status
    assert session.status()["device_allocation_bytes"] == session.status()["owned_geometry_bytes"] == 0
    report.update(result="passed", closed=session.status())
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    (dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
