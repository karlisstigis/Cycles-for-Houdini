# SPDX-License-Identifier: Apache-2.0
"""Real Blender checks for byte-backed digest reuse, without rendering in Houdini.

python tests/blend_digest_blender_smoke.py --blender <blender.exe> --output <directory>
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import time

import numpy as np
from support.package_loader import import_product


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--blender", required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
output = args.output.resolve()
output.mkdir(parents=True, exist_ok=True)
source = output / "digest.blend"
sidecar = output / "sidecar"
os.environ["CH_BLEND_TIMING_SIDECAR"] = str(sidecar)
os.environ["CH_BLEND_DIGEST_REUSE"] = "1"
worker = import_product("blend_worker")
fixture = output / "fixture.py"
fixture.write_text(
    """import bpy, sys
from pathlib import Path
bpy.ops.wm.read_factory_settings(use_empty=True)
edited = '--edited' in sys.argv
collection = bpy.data.collections.new('Dense')
bpy.context.scene.collection.children.link(collection)
vertices = [(float(i % 40), float(i // 40), 0.0) for i in range(1200)]
faces = [(i, i+1, i+2) for i in range(0, 1200, 3)]
if edited:
    faces[0] = (0, 2, 1)  # Same counts; connectivity alone changes.
for name in ('Static', 'Rotating', 'Deforming'):
    mesh = bpy.data.meshes.new(name + 'Mesh')
    mesh.from_pydata(vertices, [], faces)
    uv = mesh.uv_layers.new(name='UVMap')
    color = mesh.color_attributes.new(name='Cd', type='FLOAT_COLOR', domain='CORNER')
    weight = mesh.attributes.new(name='weight', type='FLOAT', domain='POINT')
    if edited:
        uv.data[0].uv.x = .375
        color.data[-1].color = (.75, 0, 0, 1)
        weight.data[-1].value = 3
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    if name == 'Rotating':
        for frame, angle in ((17, 0), (18, .2), (19, .4)):
            obj.rotation_euler.z = angle
            obj.keyframe_insert(data_path='rotation_euler', frame=frame)
    if name == 'Deforming':
        obj.shape_key_add(name='Basis')
        key = obj.shape_key_add(name='OneVertex')
        key.data[-1].co.z = .25
        for frame, value in ((17, 0), (18, 1), (19, 0)):
            key.value = value
            key.keyframe_insert(data_path='value', frame=frame)
bpy.context.scene.frame_set(17)
bpy.ops.wm.save_as_mainfile(filepath=str(Path(__file__).with_name('digest.blend')))
""",
    encoding="utf-8",
)


def make_source(edited=False):
    subprocess.run(
        [
            args.blender,
            "--background",
            "--factory-startup",
            "--disable-autoexec",
            "--python-exit-code",
            "1",
            "--python",
            str(fixture),
            "--",
        ]
        + (["--edited"] if edited else []),
        check=True,
    )


def request(frame):
    return dict(source=str(source), selector_type="COLLECTION", selector="Dense", frame=frame)


def row_arrays(result, owner):
    manifest, arrays = result
    key = next(row["mesh_key"] for row in manifest["instances"] if row["source_name"] == owner)
    return {name.partition("/")[2]: value for name, value in arrays.items() if name.startswith(key + "/")}


def timing_rows(pid, count):
    path = sidecar / ("worker-" + str(pid) + ".jsonl")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            if len(rows) >= count:
                return rows
        except (OSError, json.JSONDecodeError):
            pass
        time.sleep(0.01)
    raise AssertionError("Worker timing sidecar was not flushed")


try:
    make_source()
    results = worker.evaluate_many(
        source, args.blender, [request(17), request(18), request(19)], output / "textures", timeout=120
    )
    assert not any(isinstance(result, Exception) for result in results), results
    instance = next(iter(worker._workers.values()))
    pid = instance.process.pid
    for owner in ("Static", "Rotating"):
        before, after = row_arrays(results[0], owner), row_arrays(results[1], owner)
        assert all(np.array_equal(before[name], after[name]) for name in before), owner
    before, after = row_arrays(results[0], "Deforming"), row_arrays(results[1], "Deforming")
    assert np.count_nonzero(before["positions"] != after["positions"]) == 1
    timings = timing_rows(pid, 3)
    frame18 = timings[1]
    for owner in ("Static", "Rotating"):
        rows = [row for row in frame18["arrays"] if row["object"] == owner and row["nbytes"] >= 4096]
        assert rows and all(row["digest_reused"] and row["hashed_bytes"] == 0 for row in rows), rows
    position = next(
        row for row in frame18["arrays"] if row["object"] == "Deforming" and row["name"].endswith("/positions")
    )
    assert position["hashed_bytes"] == position["nbytes"] and position["sent_bytes"] == position["nbytes"]
    assert not position["digest_reused"]
    assert timings[2]["sent_bytes"] == 0  # Returning bytes remain in the host transfer cache.

    # Explicit reload of exactly the same path and signature clears snapshots.
    forced = instance.evaluate_many([request(17)], output / "forced", 120, force_reload=True)[0]
    assert not isinstance(forced, Exception), forced
    forced_timing = timing_rows(pid, 4)[3]
    assert not any(row["digest_reused"] for row in forced_timing["arrays"])
    assert forced_timing["sent_bytes"] == 0

    # A source edit at the same path reloads in this process. Same-count
    # connectivity, UV, color and named attributes must all reach the host.
    make_source(edited=True)
    changed = worker.evaluate_many(source, args.blender, [request(17)], output / "changed", timeout=120)[0]
    assert not isinstance(changed, Exception), changed
    assert next(iter(worker._workers.values())).process.pid == pid
    previous, current = row_arrays(results[0], "Deforming"), row_arrays(changed, "Deforming")
    assert np.array_equal(previous["positions"], current["positions"])
    for field in ("vertex_indices", "uv_0", "color_0"):
        assert not np.array_equal(previous[field], current[field]), field
    fields = [field for field in current if field.startswith("attribute_")]
    assert any(not np.array_equal(previous[field], current[field]) for field in fields), fields
    changed_timing = timing_rows(pid, 5)[4]
    assert not any(row["digest_reused"] for row in changed_timing["arrays"])
    report = dict(
        result="passed",
        blender=args.blender,
        pid=pid,
        single_vertex_changed_components=1,
        frame18_hashed_bytes=frame18["hashed_bytes"],
        frame18_compared_bytes=frame18["compared_bytes"],
        frame18_sent_bytes=frame18["sent_bytes"],
        return_sent_bytes=timings[2]["sent_bytes"],
        forced_reload_sent_bytes=forced_timing["sent_bytes"],
        edited_source_sent_bytes=changed_timing["sent_bytes"],
    )
    (output / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
finally:
    worker.shutdown_all()
