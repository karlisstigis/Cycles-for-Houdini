# SPDX-License-Identifier: Apache-2.0
"""Independent data-pass expectations and resource lifetime/error regressions."""

from support.paths import artifacts_root
from array import array
import json
import os
from pathlib import Path

import hou
import cycles_houdini as ch


def values(frame, name):
    result = array("f")
    result.frombytes(frame["planes"][name]["pixels"])
    return result


def centroid(frame):
    depth = values(frame, "depth")
    x_values = [i % frame["width"] for i, value in enumerate(depth) if 0 < value < 10]
    assert x_values, "No box pixels in independent depth pass"
    return sum(x_values) / len(x_values)


root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage1"
destination.mkdir(parents=True, exist_ok=True)
geo = hou.node("/obj").createNode("geo", "resource_box", run_init_scripts=False)
box = geo.createNode("box")
box.parmTuple("size").set((1.7, 1.2, 0.3))
box.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam", "resource_camera")
camera.parm("tz").set(5)
with ch.Session(device="CPU", width=160, height=90, samples=16) as session:
    request = ch.geometry_request(geo, box, material="red")
    initial = dict(
        geometry=[request],
        camera_path=camera.path(),
        background=(0.05, 0.05, 0.05),
        materials=[dict(key="red", color=(0.8, 0.05, 0.025), kind="diffuse")],
    )
    frame = session.wait(session.apply(initial))
    centre = 45 * 160 + 80
    position = values(frame, "position")[centre * 3 : centre * 3 + 3]
    normal = values(frame, "normal")[centre * 3 : centre * 3 + 3]
    depth = values(frame, "depth")[centre]
    assert abs(position[2] - 0.15) < 0.001, position
    assert abs(depth - 4.85) < 0.001, depth
    assert abs(normal[2] - 1) < 0.001 and abs(normal[0]) < 0.001 and abs(normal[1]) < 0.001, normal
    initial_centroid = centroid(frame)
    first = session.status()
    geo.parm("tx").set(0.35)
    moved_request = ch.geometry_request(geo, box, material="red")
    moved = session.wait(session.apply(dict(geometry=[moved_request])))
    assert centroid(moved) > initial_centroid + 5
    assert session.status()["geometry_conversions"] == first["geometry_conversions"]
    # Extraction that fails before the engine transaction must not poison the cache.
    box.parm("sizex").set(1.8)
    generation = session.status()["generation"]
    try:
        session.apply(dict(geometry=[moved_request], camera_path="/obj/missing_camera"))
    except RuntimeError:
        pass
    else:
        raise AssertionError("Missing camera was not rejected")
    assert session.status()["generation"] == generation
    session.wait(session.apply(dict(geometry=[moved_request], camera_path=camera.path())))
    assert session.status()["geometry_conversions"] == first["geometry_conversions"] + 1
    generation = session.status()["generation"]
    for invalid in [
        dict(deleted_materials=["red"]),
        dict(materials=[dict(key="red", color=(1, 0, 0))], deleted_materials=["red"]),
        dict(deleted_geometry=[request["geometry_key"]]),
    ]:
        try:
            session.apply(invalid)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Invalid ownership transaction was accepted")
        assert session.status()["generation"] == generation
    session.wait(
        session.apply(dict(deleted_objects=[request["object_key"]], deleted_geometry=[request["geometry_key"]]))
    )
    assert session.status()["owned_geometry_bytes"] == 0
    # The same unchanged SOP must be extracted after its corresponding engine resource was retired.
    session.wait(session.apply(dict(geometry=[moved_request])))
    assert session.status()["owned_geometry_bytes"] > 0
    warm_counts = []
    for i in range(40):
        color = (0.8, 0.1, 0.025) if i % 2 else (0.025, 0.1, 0.8)
        session.wait(session.apply(dict(materials=[dict(key="red", color=color)])))
        warm_counts.append(session.status()["device_allocation_bytes"])
    assert len(set(warm_counts[10:])) == 1, warm_counts
    final = session.status()
try:
    ch.Session(device="CPU", width=1_000_000_000, height=10)
except RuntimeError:
    pass
else:
    raise AssertionError("Unsafe image dimensions were accepted")

report = dict(
    position=list(position),
    normal=list(normal),
    depth=depth,
    transform_edit="image moved without geometry conversion",
    rejected_transactions=3,
    failed_extraction_retry="passed",
    resource_recreation="passed",
    shader_edit_allocations=warm_counts,
    memory_scope="Cycles tracked device allocations on this CPU fixture; physical VRAM and long FX sequences not certified",
    final=final,
)
(destination / "houdini-resource-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
