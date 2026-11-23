# SPDX-License-Identifier: Apache-2.0
"""Run in Blender: dense convex previews keep every member within saved budgets."""

import importlib.util
import json
from pathlib import Path
import sys

import numpy as np

# The hull builder is part of the Blender component (blender/cycles_blender); it imports its budgets
# and validators from the Houdini package (houdini/python3.13libs).
source = Path(__file__).resolve().parents[1] / "blender/cycles_blender/blend_viewport_hulls.py"
sys.path.insert(0, str(source.parents[2] / "houdini/python3.13libs"))
spec = importlib.util.spec_from_file_location("blend_viewport_hulls", source)
viewport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(viewport)
checks = []
for vertices, count in ((512, 2), (4096, 1), (20000, 8), (20000, 128)):
    points = np.random.default_rng(33).normal(size=(vertices, 3))
    points /= np.linalg.norm(points, axis=1)[:, None]
    instances = []
    for index in range(count):
        transform = np.eye(4)
        transform[0, 3] = index * 3
        instances.append(dict(mesh_key="shared", transform=transform.reshape(-1).tolist()))
    manifest = dict(meshes=[dict(key="shared", arrays=dict(positions="P"))], instances=instances)
    hulls = viewport.build_viewport_hulls(manifest, {"P": points})
    assert viewport.validate_viewport_hulls(hulls)
    assert len(hulls["members"]) == count
    assert hulls["max_vertices"] == 64
    assert all(len(member["points"]) // 3 == 64 for member in hulls["members"])
    assert hulls.get("reduced"), "Dense fixture unexpectedly failed to exercise preview reduction"
    for index, member in enumerate(hulls["members"]):
        observed = np.asarray(member["points"]).reshape(-1, 3)
        assert np.allclose(observed.min(axis=0), points.min(axis=0) + [index * 3, 0, 0])
        assert np.allclose(observed.max(axis=0), points.max(axis=0) + [index * 3, 0, 0])
    checks.append(dict(input_points=vertices, members=count, metadata_bytes=len(json.dumps(hulls).encode())))

for limit in (32, 64, 128, 256):
    one = dict(manifest, instances=instances[:1])
    many = dict(manifest, instances=instances[:8])
    obj = viewport.build_viewport_hulls(one, {"P": points}, limit)
    collection = viewport.build_viewport_hulls(many, {"P": points}, limit)
    assert len(obj["whole"]["points"]) // 3 == limit
    assert all(len(member["points"]) // 3 == limit for member in collection["members"])
    assert obj["members"][0] == collection["members"][0]
    checks.append(dict(max_vertices=limit, object_vertices=limit, collection_members=len(collection["members"])))
print(json.dumps(dict(result="passed", checks=checks)), flush=True)
