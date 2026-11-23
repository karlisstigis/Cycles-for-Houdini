# SPDX-License-Identifier: Apache-2.0
"""Small authored native mesh fixtures: no hidden OBJ or mesh SOP import."""

import numpy as np
from cycles_houdini import _native


def mesh(key, size=(1.0, 1.0, 1.0), material="", kind="box"):
    if kind == "sphere":
        positions = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))
        faces = ((0, 2, 4), (2, 1, 4), (1, 3, 4), (3, 0, 4), (2, 0, 5), (1, 2, 5), (3, 1, 5), (0, 3, 5))
    elif kind == "torus":
        # A hollow square ring keeps the fixture compact and changes topology.
        positions = []
        for z in (-0.2, 0.2):
            for radius in (1.0, 0.45):
                positions.extend((x * radius, y * radius, z) for x, y in ((-1, -1), (1, -1), (1, 1), (-1, 1)))
        faces = []
        for i in range(4):
            j = (i + 1) % 4
            faces.extend(
                (
                    (i, j, j + 8, i + 8),
                    (i + 4, i + 12, j + 12, j + 4),
                    (i + 8, j + 8, j + 12, i + 12),
                    (i, i + 4, j + 4, j),
                )
            )
    else:
        positions = ((-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1), (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1))
        faces = ((0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7))
    positions = np.asarray(positions, np.float32) * (np.asarray(size, np.float32) * 0.5)
    indices, triangles, loops, polygons = [], [], [], []
    for i, face in enumerate(faces):
        offset = len(indices)
        indices.extend(face)
        for corner in range(1, len(face) - 1):
            triangles.append((face[0], face[corner], face[corner + 1]))
            loops.append((offset, offset + corner, offset + corner + 1))
            polygons.append(i)
    arrays = dict(
        positions=np.ascontiguousarray(positions),
        polygon_counts=np.asarray([len(f) for f in faces], np.int32),
        vertex_indices=np.asarray(indices, np.int32),
        material_indices=np.zeros(len(faces), np.int32),
        smooth=np.zeros(len(faces), np.uint8),
        triangle_vertices=np.asarray(triangles, np.int32),
        triangle_loops=np.asarray(loops, np.int32),
        triangle_polygons=np.asarray(polygons, np.int32),
        normals=np.zeros((len(indices), 3), np.float32),
    )
    record = dict(key=key, arrays={name: name for name in arrays}, normals_domain="FACE")
    return _native.blend_mesh(record, arrays, [material])


def prototype(key, capsule, translate=(0.0, 0.0, 0.0), dependencies=()):
    x, y, z = translate
    return dict(
        dependencies=list(dependencies),
        diagnostics=[],
        instances=[
            dict(key=key, geometry_key=key, mesh=capsule, transform=[1, 0, 0, x, 0, 1, 0, y, 0, 0, 1, z, 0, 0, 0, 1])
        ],
    )
