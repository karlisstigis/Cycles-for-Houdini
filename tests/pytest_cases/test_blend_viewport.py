# SPDX-License-Identifier: Apache-2.0
"""Hull previews are bounded metadata and reduce repeated prototypes once."""

from copy import deepcopy

import numpy as np
import pytest

from cycles_blender import blend_viewport_hulls as hulls
from cycles_houdini.blender import blend_viewport as viewport

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def tetra():
    return dict(points=[0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1], triangles=[0, 2, 1, 0, 1, 3, 0, 3, 2, 1, 2, 3])


def test_valid_saved_hulls_roundtrip_independent_whole_and_members():
    value = dict(whole=tetra(), members=[tetra(), tetra()])
    assert viewport.validate_viewport_hulls(value)
    assert viewport.validate_piece(value["whole"])
    assert value["whole"] is not value["members"][0]


@pytest.mark.parametrize(
    "change",
    [
        {"points": [float("nan")] * 12},
        {"points": [float("inf")] * 12},
        {"points": [True] * 12},
        {"points": [0] * 11},
        {"triangles": [0, 1]},
        {"triangles": [0, 1, 4]},
        {"triangles": [-1, 1, 2]},
        {"triangles": [False, 1, 2]},
    ],
)
def test_invalid_hull_coordinates_and_triangle_indices_are_rejected(change):
    assert not viewport.validate_piece(dict(tetra(), **change))
    assert not viewport.validate_viewport_hulls(dict(whole=dict(tetra(), **change), members=[tetra()]))


def test_hull_metadata_has_aggregate_piece_and_geometry_budgets():
    assert not viewport.validate_viewport_hulls(dict(whole=tetra(), members=[]))
    assert not viewport.validate_viewport_hulls(dict(whole=tetra(), members=[tetra()] * (viewport.MAX_PIECES + 1)))
    huge = dict(points=[0.0, 0.0, 0.0] * viewport.MAX_POINTS, triangles=[0, 1, 2])
    assert viewport.validate_piece(huge)
    assert not viewport.validate_viewport_hulls(dict(whole=huge, members=[tetra()]))


def test_repeated_instances_share_prototype_hull_and_preserve_member_transform_winding(monkeypatch):
    calls = []

    def convex(points):
        calls.append(np.asarray(points).copy())
        return deepcopy(tetra())

    monkeypatch.setattr(hulls, "_convex", convex)
    reflected = np.diag([-2.0, 3.0, 0.5, 1.0])
    reflected[:3, 3] = [5.0, 7.0, -1.0]
    manifest = dict(
        meshes=[dict(key="shared", arrays=dict(positions="P"))],
        instances=[
            dict(mesh_key="shared", transform=np.eye(4).reshape(-1).tolist()),
            dict(mesh_key="shared", transform=reflected.reshape(-1).tolist()),
        ],
    )
    result = hulls.build_viewport_hulls(manifest, {"P": np.asarray(tetra()["points"]).reshape(-1, 3)})
    assert len(calls) == 2, "Prototype hull was rebuilt for each instance"
    assert len(result["members"]) == 2
    assert result["members"][0]["points"] == tetra()["points"]
    assert result["members"][1]["points"][:6] == [5.0, 7.0, -1.0, 3.0, 7.0, -1.0]
    assert result["members"][1]["triangles"][:3] == [0, 1, 2]
    assert calls[1].shape == (8, 3)


def test_preview_reduction_is_deterministic_and_keeps_axis_extremes():
    points = np.random.default_rng(9).normal(size=(20000, 3))
    reduced = hulls._preview_points(points, 64)
    assert len(reduced) == 64
    assert np.array_equal(reduced, hulls._preview_points(points, 64))
    assert np.array_equal(reduced.min(axis=0), points.min(axis=0))
    assert np.array_equal(reduced.max(axis=0), points.max(axis=0))


def test_dense_collection_reduces_each_member_without_dropping_instances(monkeypatch):
    calls = []

    def convex(points):
        points = np.asarray(points).reshape(-1, 3)
        calls.append(len(points))
        if len(points) > 4096:
            raise ValueError("over budget")
        # Keep every input point to expose aggregate budgets independently of
        # Blender's implementation. The real bmesh integration gate checks hulls.
        return dict(points=points.reshape(-1).tolist(), triangles=[0, 1, 2])

    monkeypatch.setattr(hulls, "_convex", convex)
    points = np.random.default_rng(9).normal(size=(20000, 3))
    manifest = dict(
        meshes=[dict(key="shared", arrays=dict(positions="P"))],
        instances=[dict(mesh_key="shared", transform=np.eye(4).reshape(-1).tolist()) for _ in range(128)],
    )
    result = hulls.build_viewport_hulls(manifest, {"P": points})
    assert viewport.validate_viewport_hulls(result)
    assert result["reduced"]
    assert len(result["members"]) == 128
    assert max(len(member["points"]) // 3 for member in result["members"]) == 64
    assert len(calls) == 2, "Repeated collection members rebuilt their shared prototype hull"


@pytest.mark.parametrize("limit", [8, 32, 64, 128, 256])
def test_same_requested_limit_for_object_and_every_collection_member(monkeypatch, limit):
    monkeypatch.setattr(
        hulls,
        "_convex",
        lambda points: dict(
            points=np.asarray(points).reshape(-1).tolist(),
            triangles=[0, 1, 2],
        ),
    )
    points = np.random.default_rng(3).normal(size=(2000, 3))
    instance = dict(mesh_key="shared", transform=np.eye(4).reshape(-1).tolist())
    manifest = dict(meshes=[dict(key="shared", arrays=dict(positions="P"))], instances=[instance])
    obj = hulls.build_viewport_hulls(manifest, {"P": points}, limit)
    collection = hulls.build_viewport_hulls(dict(manifest, instances=[instance] * 8), {"P": points}, limit)
    assert obj["max_vertices"] == collection["max_vertices"] == limit
    assert len(obj["whole"]["points"]) // 3 == limit
    assert all(member == obj["members"][0] for member in collection["members"])
    assert all(len(member["points"]) // 3 == limit for member in collection["members"])


def test_aggregate_guard_refuses_without_secretly_lowering_member_detail(monkeypatch):
    calls = []

    def convex(points):
        calls.append(len(points))
        return dict(points=np.asarray(points).reshape(-1).tolist(), triangles=[0, 1, 2])

    monkeypatch.setattr(hulls, "_convex", convex)
    points = np.random.default_rng(3).normal(size=(2000, 3))
    manifest = dict(
        meshes=[dict(key="shared", arrays=dict(positions="P"))],
        instances=[dict(mesh_key="shared", transform=np.eye(4).reshape(-1).tolist())] * 128,
    )
    with pytest.raises(ValueError, match="budget"):
        hulls.build_viewport_hulls(manifest, {"P": points}, 256)
    assert calls == [256]


@pytest.mark.parametrize("value", [True, 3, 4, 5, 6, 7, 4097, 64.5, "64"])
def test_invalid_explicit_vertex_limits_are_rejected(value):
    with pytest.raises(ValueError, match="Maximum Hull Vertices"):
        viewport.hull_vertex_limit(value)
