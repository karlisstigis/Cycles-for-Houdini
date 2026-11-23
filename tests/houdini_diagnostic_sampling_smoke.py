# SPDX-License-Identifier: Apache-2.0
"""Diagnostic surfaces stay visible without becoming mesh lights; repairs restore lighting."""

from support.paths import artifacts_root
import argparse
import json
import os
from pathlib import Path

import hou
import numpy as np
import cycles_houdini as ch

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="")
args = parser.parse_args()
devices = [args.device] if args.device else ["CPU", ch.default_device()]
destination = artifacts_root() / "stage3/materials/diagnostic-sampling"
destination.mkdir(parents=True, exist_ok=True)

marker = hou.node("/obj").createNode("geo", "diagnostic_marker", run_init_scripts=False)
marker.setParms(dict(tx=-0.65, tz=1))
marker_grid = marker.createNode("grid", "marker_surface")
marker_grid.setParms(dict(type=0, orient=0, rows=2, cols=2))
marker_grid.parmTuple("size").set((1, 1))
receiver = hou.node("/obj").createNode("geo", "unlit_receiver", run_init_scripts=False)
receiver.parm("tx").set(0.65)
receiver_grid = receiver.createNode("grid", "receiver_surface")
# Keep this independent shader-pool probe in a distinct mesh family. An
# identical shared control mesh retains its original shader palette while any
# receiver is alive, so that slot is deliberately unavailable for recycling.
receiver_grid.setParms(dict(type=0, orient=0, rows=3, cols=3))
receiver_grid.parmTuple("size").set((1, 1))
camera = hou.node("/obj").createNode("cam", "diagnostic_sampling_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
width, height = 96, 72
reports = []


def request(key="marker"):
    return [
        ch.geometry_request(marker, marker_grid, material=key),
        ch.geometry_request(receiver, receiver_grid, material="receiver"),
    ]


def pixel(frame, x, plane="combined"):
    field = frame["planes"][plane]
    values = np.frombuffer(field["pixels"], np.float32).reshape(height, width, field["channels"])
    column = int((x / 4 + 0.5) * width)
    return values[height // 2 - 1 : height // 2 + 2, column - 1 : column + 2].mean(axis=(0, 1))


def definition(key="marker", diagnostic=True):
    return dict(
        key=key,
        kind="diagnostic_surface" if diagnostic else "emission",
        color=(1, 0, 1) if diagnostic else (0.1, 1, 0.2),
        strength=5,
        cryptomatte_name="source assignment " + key,
    )


for device in devices:
    marker.parm("tx").set(-0.65)
    rows = []
    with ch.Session(
        device=device,
        width=width,
        height=height,
        samples=512,
        interactive=False,
        collect_diagnostics=True,
        passes=["combined", "normal", "depth"],
    ) as session:

        def check(name, frame, diagnostic):
            status = session.status()
            assert status["state"] == "complete" and not status["error"], status
            assert status["geometry_resources"] == status["object_resources"] == 2, status
            assert not status["source_diagnostics"], status["source_diagnostics"]
            values = pixel(frame, marker.evalParm("tx"))[:3]
            assert np.allclose(values, (1, 0, 1) if diagnostic else (0.5, 5, 1), rtol=0, atol=0.002), values
            illumination = pixel(frame, 0.65)[:3]
            assert np.isfinite(illumination).all()
            if diagnostic:
                assert np.max(np.abs(illumination)) < 1e-6, illumination
            else:
                assert illumination[1] > 0.005 and illumination[1] > illumination[0] * 3, illumination
            diagnostics = session.diagnostics()
            assert not diagnostics["engine_statistics_pending"], diagnostics
            tree_bytes = sum(
                value["requested_bytes"]
                for key, value in diagnostics["buffer_copy_requests"].items()
                if key in ("light_tree_emitters", "light_tree_nodes", "triangle_to_tree")
            )
            if diagnostic:
                assert tree_bytes == 0, ("Camera-only diagnostic created mesh-light sampling data", diagnostics)
            else:
                assert tree_bytes > 0, ("Repair did not restore real emitter sampling", diagnostics)
            assert abs(pixel(frame, 0.65, "depth")[0] - 5) < 0.002
            assert np.allclose(np.abs(pixel(frame, 0.65, "normal")), (0, 0, 1), atol=0.002)
            rows.append(
                dict(
                    name=name,
                    marker_rgb=values.tolist(),
                    receiver_rgb=illumination.tolist(),
                    tree_copy_bytes=tree_bytes,
                    status=status,
                    diagnostics=diagnostics,
                )
            )

        first = session.wait(
            session.apply(
                dict(
                    geometry=request(),
                    camera_path=camera.path(),
                    background=(0, 0, 0),
                    materials=[definition(), dict(key="receiver", kind="diffuse", color=(0.8, 0.8, 0.8))],
                )
            )
        )
        check("Fallback is visible in darkness without lighting its neighbor or creating mesh-light data", first, True)
        conversions = session.status()["geometry_conversions"]
        marker.parm("tx").set(-0.75)
        check(
            "Moving fallback retains geometry and creates no light-tree uploads",
            session.wait(session.apply(dict(geometry=request()))),
            True,
        )
        assert session.status()["geometry_conversions"] == conversions
        for diagnostic in (False, True, False, True):
            frame = session.wait(session.apply(dict(materials=[definition(diagnostic=diagnostic)])))
            check(
                "Same material identity switches to " + ("fallback" if diagnostic else "real emission"),
                frame,
                diagnostic,
            )
            assert session.status()["geometry_conversions"] == conversions

        # A retired diagnostic slot may next be used by a genuinely emissive
        # material. Its sampling policy must not leak into that new owner.
        session.wait(session.apply(dict(geometry=[request()[1]], deleted_materials=["marker"])))
        assert session.status()["retired_shader_resources"] == 1
        check(
            "A pooled fallback shader restores emission for its new owner",
            session.wait(session.apply(dict(geometry=request("repaired"), materials=[definition("repaired", False)]))),
            False,
        )
        assert session.status()["shader_reuses"] >= 1
        reports.append(dict(device=device, checks=rows, final=session.status()))
    closed = session.status()
    assert closed["device_allocation_bytes"] == closed["owned_geometry_bytes"] == closed["engine_shader_resources"] == 0
    reports[-1]["closed"] = closed

path = destination / "report.json"
path.write_text(
    json.dumps(
        dict(
            result="passed",
            devices=reports,
            scope="Independent camera radiance, neighbor illumination and data passes, seven states per device. "
            "Logical light-tree copy requests and tracked closure; no physical memory or latency certification.",
        ),
        indent=2,
    )
)
print(path)
