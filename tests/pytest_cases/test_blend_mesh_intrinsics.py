# SPDX-License-Identifier: Apache-2.0
"""Link meshes get Pointiness and Random per Island by Blender's own rule (task S3,
blender-sync-audit items 8 and 9).

Blender v5.3.0 intern/cycles/blender/mesh.cpp: attr_create_pointiness welds coincident
vertices and walks the mesh's edges() (polygon and loose edges) with Blender's vertex normals;
attr_create_random_per_island joins a DisjointSet over edges() in edge order and hashes the
union-find root. The exporter sends Blender's edges and vertex normals; src/core/
mesh_pointiness.h and mesh_islands.h port both rules.

The bpy cases build meshes with Blender 5.3 (the PyPI bpy module), bake Blender's Cycles
(emission = Pointiness in red, Random Per Island in green) into a float colour attribute per
corner, and export the same meshes through the worker's exporter. The numbers are checked
three ways: an independent Python transcription of Blender's code on the exported arrays,
Blender's own baked values, and the C++ headers themselves (compiled with g++ when present).
"""

import functools
import importlib.util
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]

SCRIPT = r"""
import json, math, sys, tempfile
# argv[1]: <checkout>/houdini/python3.13libs; the Blender component is <checkout>/blender.
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/../../blender"]
import bpy
import numpy as np
from cycles_blender import blend_export

out = sys.argv[2]
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
asset = bpy.data.collections.new("Intrinsics")
scene.collection.children.link(asset)


def emission_material(name, intrinsic):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    nodes, links = material.node_tree.nodes, material.node_tree.links
    nodes.clear()
    emission = nodes.new("ShaderNodeEmission")
    if intrinsic:
        geometry = nodes.new("ShaderNodeNewGeometry")
        combine = nodes.new("ShaderNodeCombineColor")
        links.new(geometry.outputs["Pointiness"], combine.inputs[0])
        links.new(geometry.outputs["Random Per Island"], combine.inputs[1])
        links.new(combine.outputs[0], emission.inputs["Color"])
    links.new(emission.outputs[0], nodes.new("ShaderNodeOutputMaterial").inputs["Surface"])
    return material


intrinsic = emission_material("Intrinsics", True)
plain = emission_material("Plain", False)


def add(name, vertices, edges, faces, location):
    data = bpy.data.meshes.new(name)
    data.from_pydata(vertices, edges, faces)
    data.materials.append(intrinsic)
    obj = bpy.data.objects.new(name, data)
    obj.location = location
    asset.objects.link(obj)
    return obj


cube_vertices = [(x, y, z) for x in (-0.5, 0.5) for y in (-0.5, 0.5) for z in (-0.5, 0.5)]
cube_faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
add("Cube", cube_vertices, [], cube_faces, (-3, 0, 0))
# A quad grid with a smooth bump: its quads are not planar, so diagonals would count.
size = 9
grid = []
for j in range(size):
    for i in range(size):
        x, y = -1 + 2 * i / (size - 1), -1 + 2 * j / (size - 1)
        grid.append((x, y, 0.45 * math.exp(-2.5 * (x * x + y * y))))
quads = [(j * size + i, j * size + i + 1, (j + 1) * size + i + 1, (j + 1) * size + i)
         for j in range(size - 1) for i in range(size - 1)]
add("Bump", grid, [], quads, (0, 0, 0))
# A hexagon surrounded by non-planar quads and a pentagon.
ring = [(0.5 * math.cos(k * math.pi / 3), 0.5 * math.sin(k * math.pi / 3), 0.0) for k in range(6)]
outer = [(1.1 * math.cos(k * math.pi / 3), 1.1 * math.sin(k * math.pi / 3), 0.35 * (k % 2)) for k in range(6)]
tip = [(1.6, 0.6, 0.2)]
faces = [tuple(range(6))] + [(k, (k + 1) % 6, 6 + (k + 1) % 6, 6 + k) for k in range(6)] + [(6, 12, 7)]
add("Ngons", ring + outer + tip, [], faces[:-1] + [(6, 12, 7, 1, 0)][:0] + [(6, 12, 7)], (3, 0, 0))
# Islands: quads A and B joined only by a loose edge, a separate quad C listed backwards,
# a loose vertex and a loose edge.
islands = [(-1.2, -0.4, 0), (-0.4, -0.4, 0), (-0.4, 0.4, 0.1), (-1.2, 0.4, 0),
           (0.4, -0.4, 0), (1.2, -0.4, 0.1), (1.2, 0.4, 0), (0.4, 0.4, 0),
           (-0.4, 1.0, 0), (0.4, 1.0, 0), (0.4, 1.6, 0.2), (-0.4, 1.6, 0),
           (1.5, 1.5, 0), (-1.5, 1.5, 0), (-1.5, 1.9, 0.2)]
add("Islands", islands, [(2, 7), (13, 14)], [(0, 1, 2, 3), (4, 5, 6, 7), (11, 10, 9, 8)], (0, -3, 0))
# A roof whose ridge vertices are duplicated (exactly, and one nearly) instead of shared.
roof = [(-0.6, -0.6, 0), (0.6, -0.6, 0), (0.6, 0, 0.5), (-0.6, 0, 0.5),
        (-0.6, 0, 0.5), (0.6 + 1e-5, 0 - 1e-5, 0.5), (0.6, 0.6, 0), (-0.6, 0.6, 0),
        (1.2, 0, 0.1)]
add("Doubles", roof, [], [(0, 1, 2, 3), (4, 5, 6, 7), (1, 8, 2)], (3, -3, 0))


def subdivided(name, material):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 6 if material is intrinsic else 9, 0))
    obj = bpy.context.active_object
    obj.name = name
    obj.data.materials.append(material)
    obj.data.polygons.foreach_set("use_smooth", [True] * len(obj.data.polygons))
    obj.modifiers.new("Subdivision", "SUBSURF").render_levels = 1
    return obj


subdivided("SubPointy", intrinsic)
subdivided("SubPlain", plain)
path = out + "/intrinsics.blend"
bpy.ops.wm.save_as_mainfile(filepath=path)

# Blender's own Cycles: bake the emission (R = Pointiness, G = Random Per Island) into a float
# colour attribute per corner. A corner sample reads the attribute at the vertex exactly.
scene.render.engine = "CYCLES"
scene.cycles.device, scene.cycles.samples = "CPU", 1
baked_objects = list(asset.objects)
for obj in scene.objects:
    obj.select_set(obj in baked_objects)
for obj in baked_objects:
    attribute = obj.data.color_attributes.new("Bake", "FLOAT_COLOR", "CORNER")
    obj.data.color_attributes.active_color = attribute
bpy.context.view_layer.objects.active = baked_objects[0]
bpy.ops.object.bake(type="EMIT", target="VERTEX_COLORS")
baked = {}
for obj in baked_objects:
    values = np.empty(len(obj.data.loops) * 4, np.float32)
    obj.data.color_attributes["Bake"].data.foreach_get("color", values)
    baked[obj.name] = dict(values=values.reshape(-1, 4)[:, :2].tolist(), location=list(obj.location))

result = dict(baked=baked)
requests = [dict(source=path, selector_type="COLLECTION", selector="Intrinsics", frame=1.0),
            dict(source=path, selector_type="OBJECT", selector="SubPointy", frame=1.0),
            dict(source=path, selector_type="OBJECT", selector="SubPlain", frame=1.0)]
with tempfile.TemporaryDirectory() as textures:
    for name, value in zip(("Intrinsics", "SubPointy", "SubPlain"), blend_export.evaluate_many(requests, textures)):
        if isinstance(value, Exception):
            result[name] = dict(error=str(value))
            continue
        manifest, arrays = value
        meshes = {mesh["key"]: mesh for mesh in manifest["meshes"]}
        result[name] = dict(
            meshes={
                instance["name"]: dict(
                    {field: arrays[meshes[instance["mesh_key"]]["arrays"][field]].tolist()
                     for field in ("positions", "vertex_normals", "edges", "vertex_indices", "triangle_vertices",
                                   "triangle_loops")
                     if field in meshes[instance["mesh_key"]]["arrays"]},
                    subdivision=meshes[instance["mesh_key"]].get("subdivision"),
                )
                for instance in manifest["instances"]
            },
            diagnostics=manifest["diagnostics"],
        )
print("RESULT:" + json.dumps(result))
"""

# The C++ rules under test, compiled as they are (src/core/mesh_pointiness.h, mesh_islands.h).
HARNESS = r"""
#include "core/mesh_pointiness.h"
#include "core/mesh_islands.h"
#include <cstdio>
#include <cstdlib>
#include <iostream>
namespace ccl {
void *util_aligned_malloc(size_t size, int alignment) {
  return std::aligned_alloc(alignment, (size + alignment - 1) / alignment * alignment);
}
void util_aligned_free(void *ptr, size_t) { std::free(ptr); }
} // namespace ccl
using namespace cycles_houdini;
// stdin: blender(0/1) points; positions; normals; edge count, pairs; triangle count, triples.
int main() {
  MeshData m;
  int blender, n, e, t;
  std::cin >> blender >> n;
  for (int i = 0; i < n; ++i) {
    Float3 p;
    std::cin >> p.x >> p.y >> p.z;
    m.positions.push_back(p);
  }
  for (int i = 0; i < n; ++i) {
    Float3 p;
    std::cin >> p.x >> p.y >> p.z;
    if (blender)
      m.blender_vertex_normals.push_back(p);
  }
  std::cin >> e;
  for (int i = 0; i < 2 * e; ++i) {
    int v;
    std::cin >> v;
    if (blender)
      m.blender_edges.push_back(v);
  }
  std::cin >> t;
  for (int i = 0; i < t; ++i) {
    Triangle tri;
    std::cin >> tri.a >> tri.b >> tri.c;
    m.triangles.push_back(tri);
  }
  for (float v : mesh_pointiness(m))
    std::printf("%.9g ", v);
  std::printf("\n");
  for (float v : mesh_island_random(m))
    std::printf("%.9g ", v);
  std::printf("\n");
}
"""

f32 = np.float32
FLT_EPSILON = f32(1.1920928955078125e-07)
MESHES = ("Cube", "Bump", "Ngons", "Islands", "Doubles")


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    if importlib.util.find_spec("bpy") is None:
        pytest.skip("bpy (Blender as a Python module) is not installed")
    folder = tmp_path_factory.mktemp("blend_mesh_intrinsics")
    script = folder / "make_bake_export.py"
    script.write_text(SCRIPT, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-I", str(script), str(ROOT / "houdini/python3.13libs"), str(folder)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
        cwd=folder,
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith("RESULT:")]
    assert lines, completed.stdout[-4000:] + completed.stderr[-4000:]
    return json.loads(lines[-1][len("RESULT:") :])


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    compiler = shutil.which("g++")
    cycles = ROOT / "third_party/cycles/src"
    if compiler is None or not (cycles / "util/disjoint_set.h").exists():
        pytest.skip("g++ or the Cycles sources are not available")
    folder = tmp_path_factory.mktemp("intrinsics_harness")
    (folder / "harness.cpp").write_text(HARNESS, encoding="utf-8")
    binary = folder / "harness"
    completed = subprocess.run(
        [compiler, "-std=c++20", "-O2", "-msse4.2", "-DCCL_NAMESPACE_BEGIN=namespace ccl {", "-DCCL_NAMESPACE_END=}"]
        + ["-I" + str(ROOT / "include"), "-I" + str(ROOT / "src"), "-I" + str(cycles)]
        + [str(folder / "harness.cpp"), "-o", str(binary)],
        capture_output=True,
        text=True,
        timeout=170,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]

    def run(mesh, blender=True):
        positions = np.asarray(mesh["positions"], f32).reshape(-1, 3)
        normals = np.asarray(mesh["vertex_normals"], f32).reshape(-1, 3)
        edges = np.asarray(mesh["edges"], np.int64).reshape(-1, 2)
        triangles = np.asarray(mesh["triangle_vertices"], np.int64).reshape(-1, 3)
        numbers = [int(blender), len(positions)]
        numbers += [repr(float(v)) for v in positions.ravel()] + [repr(float(v)) for v in normals.ravel()]
        numbers += [len(edges)] + edges.ravel().tolist() + [len(triangles)] + triangles.ravel().tolist()
        completed = subprocess.run(
            [str(binary)], input=" ".join(map(str, numbers)), capture_output=True, text=True, timeout=60
        )
        assert completed.returncode == 0, completed.stderr
        pointiness, islands = completed.stdout.split("\n")[:2]
        return np.array(pointiness.split(), f32), np.array(islands.split(), f32)

    return run


# ---------------------------------------------------------------- independent transcription


def _dot(a, b):
    return f32(f32(a[0] * b[0]) + f32(a[1] * b[1])) + f32(a[2] * b[2])


def blender_pointiness(positions, vertex_normals, edges):
    """Blender v5.3.0 attr_create_pointiness, line by line, in float32."""
    positions = np.asarray(positions, f32).reshape(-1, 3)
    normals = np.asarray(vertex_normals, f32).reshape(-1, 3)
    edges = np.asarray(edges, np.int64).reshape(-1, 2)
    count = len(positions)
    sums = [f32(f32(p[0] + p[1]) + p[2]) for p in positions]

    def compare(a, b):  # VertexAverageComparator
        if np.array_equal(positions[a], positions[b]):
            return -1 if a > b else (1 if a < b else 0)
        return -1 if sums[a] < sums[b] else (1 if sums[b] < sums[a] else 0)

    order = sorted(range(count), key=functools.cmp_to_key(compare))
    original = list(range(count))
    for s, vertex in enumerate(order):
        for other in order[s + 1 :]:
            if f32(sums[other] - sums[vertex]) > f32(3) * FLT_EPSILON:
                break
            difference = positions[other] - positions[vertex]
            if _dot(difference, difference) < FLT_EPSILON:
                original[vertex] = other
                break
    for vertex in range(count):
        root = original[vertex]
        while root != original[root]:
            root = original[root]
        original[vertex] = root
    accumulated = np.zeros((count, 3), f32)
    for vertex in range(count):
        accumulated[original[vertex]] += normals[vertex]
    normal = np.zeros((count, 3), f32)
    for vertex in range(count):
        length = np.sqrt(_dot(accumulated[original[vertex]], accumulated[original[vertex]]))
        normal[vertex] = accumulated[original[vertex]] / length
    counter = np.zeros(count, np.int64)
    raw = np.zeros(count, f32)
    edge_sum = np.zeros((count, 3), f32)
    visited = set()
    for a, b in edges:
        v0, v1 = original[a], original[b]
        if (min(v0, v1), max(v0, v1)) in visited:
            continue
        visited.add((min(v0, v1), max(v0, v1)))
        edge = positions[v1] - positions[v0]
        length = np.sqrt(_dot(edge, edge))
        if length != 0:
            edge = edge * (f32(1) / length)
        edge_sum[v0] += edge
        edge_sum[v1] += -edge
        counter[v0] += 1
        counter[v1] += 1
    for vertex in range(count):
        if original[vertex] == vertex and counter[vertex] > 0:
            cosine = _dot(normal[vertex], edge_sum[vertex] / f32(counter[vertex]))
            cosine = min(max(cosine, f32(-1)), f32(1))
            raw[vertex] = f32(math.acos(float(cosine))) * f32(0.3183098861837067)
    data = raw.copy()
    counter[:] = 0
    visited.clear()
    for a, b in edges:
        v0, v1 = original[a], original[b]
        if (min(v0, v1), max(v0, v1)) in visited:
            continue
        visited.add((min(v0, v1), max(v0, v1)))
        data[v0] += raw[v1]
        data[v1] += raw[v0]
        counter[v0] += 1
        counter[v1] += 1
    for vertex in range(count):
        data[vertex] /= f32(counter[vertex] + 1)
    return np.array([data[original[vertex]] for vertex in range(count)], f32)


_UINT = 0xFFFFFFFF


def _rot(value, shift):
    return ((value << shift) | (value >> (32 - shift))) & _UINT


def hash_uint_to_float(kx):
    """Cycles util/hash.h hash_uint (Jenkins lookup3 final) and uint_to_float_incl."""
    a = b = c = (0xDEADBEEF + (1 << 2) + 13) & _UINT
    a = (a + kx) & _UINT
    c ^= b
    c = (c - _rot(b, 14)) & _UINT
    a ^= c
    a = (a - _rot(c, 11)) & _UINT
    b ^= a
    b = (b - _rot(a, 25)) & _UINT
    c ^= b
    c = (c - _rot(b, 16)) & _UINT
    a ^= c
    a = (a - _rot(c, 4)) & _UINT
    b ^= a
    b = (b - _rot(a, 14)) & _UINT
    c ^= b
    c = (c - _rot(b, 24)) & _UINT
    return f32(c) * (f32(1) / f32(4294967295.0))


def blender_islands(vertex_count, edges, first_corners):
    """Blender v5.3.0 attr_create_random_per_island with Cycles' util/disjoint_set.h."""
    parents, ranks = list(range(vertex_count)), [0] * vertex_count

    def find(x):
        root = x
        while parents[root] != root:
            root = parents[root]
        while parents[x] != root:
            parents[x], x = root, parents[x]
        return root

    for a, b in np.asarray(edges, np.int64).reshape(-1, 2).tolist():
        x, y = find(a), find(b)
        if x == y:
            continue
        if ranks[x] < ranks[y]:
            x, y = y, x
        parents[y] = x
        if ranks[x] == ranks[y]:
            ranks[x] += 1
    return np.array([hash_uint_to_float(find(vertex)) for vertex in first_corners], f32), find


def _mesh(exported, name):
    case = exported["Intrinsics"]
    assert "error" not in case, case
    return case["meshes"][name]


def _baked(exported, name, mesh):
    """Blender's baked Random per Island per triangle, and a function comparing per-vertex
    Pointiness values with the baked corners. A corner bake samples its first loop triangle
    near the corner: Cycles (kernel/integrator/init_from_bake.h, bake_offset_towards_center)
    moves the point towards the triangle's centre by 1e-4 * max(1, largest |coordinate|) in
    world space, so the bake reads the vertex value blended that far towards the centre."""
    bake = exported["baked"][name]
    values = np.asarray(bake["values"], f32)
    corners = np.asarray(mesh["vertex_indices"], np.int64)
    loops = np.asarray(mesh["triangle_loops"], np.int64).reshape(-1, 3)
    triangles = np.asarray(mesh["triangle_vertices"], np.int64).reshape(-1, 3)
    world = np.asarray(mesh["positions"], np.float64).reshape(-1, 3) + np.asarray(bake["location"])
    first = {}
    for triangle, corner_loops in enumerate(loops.tolist()):
        for loop in corner_loops:
            first.setdefault(loop, triangle)

    def pointiness_error(per_vertex):
        errors = []
        for loop, triangle in first.items():
            vertex, corners_of = corners[loop], triangles[triangle]
            position, centre = world[vertex], world[corners_of].mean(axis=0)
            distance = np.linalg.norm(centre - position)
            fraction = min(distance, max(np.abs(position).max(), 1.0) * 1e-4) / distance
            mean = float(np.mean(per_vertex[corners_of].astype(np.float64)))
            predicted = float(per_vertex[vertex]) + fraction * (mean - float(per_vertex[vertex]))
            errors.append(abs(predicted - float(values[loop, 0])))
        return max(errors)

    return pointiness_error, values[loops[:, 0], 1]


# ---------------------------------------------------------------- item 8: Pointiness


@pytest.mark.parametrize("name", MESHES)
@pytest.mark.blender
@pytest.mark.cpu
def test_pointiness_transcription_equals_blenders_bake(exported, name):
    mesh = _mesh(exported, name)
    expected = blender_pointiness(mesh["positions"], mesh["vertex_normals"], mesh["edges"])
    error, _ = _baked(exported, name, mesh)
    assert error(expected) <= 3e-6


@pytest.mark.parametrize("name", MESHES)
@pytest.mark.blender
@pytest.mark.cpu
def test_native_pointiness_equals_blender(exported, harness, name):
    mesh = _mesh(exported, name)
    ours, _ = harness(mesh)
    expected = blender_pointiness(mesh["positions"], mesh["vertex_normals"], mesh["edges"])
    error, _ = _baked(exported, name, mesh)
    # Up to a few float32 steps from acosf (C library) are allowed.
    numbers = dict(native_vs_transcription=float(np.abs(ours - expected).max()), native_vs_bake=error(ours))
    print(f"POINTINESS {name}: " + json.dumps(numbers))
    assert numbers["native_vs_transcription"] <= 2.5e-7, (ours, expected)
    assert numbers["native_vs_bake"] <= 3e-6


@pytest.mark.blender
@pytest.mark.cpu
def test_previous_pointiness_rule_differs_where_quads_bend(exported, harness):
    """The face-derived rule (still used for Houdini meshes) counted triangle diagonals, used
    its own normals and ignored loose edges, so Link renders change by these amounts."""
    differences = {}
    for name in MESHES:
        mesh = _mesh(exported, name)
        old, _ = harness(mesh, blender=False)
        new, _ = harness(mesh)
        differences[name] = round(float(np.abs(old - new).max()), 4)
    print("PREVIOUS_RULE_MAX_DIFFERENCE:" + json.dumps(differences))
    assert differences["Cube"] > 0.01 and differences["Bump"] > 0.003 and differences["Islands"] > 0.1, differences


@pytest.mark.blender
@pytest.mark.cpu
def test_duplicates_are_welded_like_blender(exported):
    mesh = _mesh(exported, "Doubles")
    values = blender_pointiness(mesh["positions"], mesh["vertex_normals"], mesh["edges"])
    # Ridge vertices 3/4 (exact) and 2/5 (1e-5 apart) share their original's value; without
    # welding the ridge would be an open border on both sides.
    assert values[3] == values[4] and values[2] == values[5]
    error, _ = _baked(exported, "Doubles", mesh)
    assert error(values) <= 3e-6


# ---------------------------------------------------------------- item 9: Random per Island


@pytest.mark.parametrize("name", MESHES)
@pytest.mark.blender
@pytest.mark.cpu
def test_random_per_island_equals_blender(exported, harness, name):
    mesh = _mesh(exported, name)
    triangles = np.asarray(mesh["triangle_vertices"], np.int64).reshape(-1, 3)
    expected, _ = blender_islands(len(mesh["positions"]), mesh["edges"], triangles[:, 0])
    _, baked = _baked(exported, name, mesh)
    assert np.array_equal(expected, baked), (expected, baked)
    _, ours = harness(mesh)
    assert np.array_equal(ours, expected), (ours, expected)


@pytest.mark.blender
@pytest.mark.cpu
def test_loose_edge_joins_islands_and_roots_are_not_lowest_vertices(exported, harness):
    mesh = _mesh(exported, "Islands")
    triangles = np.asarray(mesh["triangle_vertices"], np.int64).reshape(-1, 3)
    values, find = blender_islands(len(mesh["positions"]), mesh["edges"], triangles[:, 0])
    # Quads A (0-3) and B (4-7) meet only through the loose edge 2-7; quad C is separate.
    groups = {}
    for triangle, value in zip(triangles, values):
        groups.setdefault(int(min(triangle) // 4), set()).add(float(value))
    assert groups[0] == groups[1] and len(groups[0]) == 1 and groups[2] != groups[0], groups
    _, previous = harness(mesh, blender=False)
    assert not np.array_equal(previous, values)  # the previous rule hashed the lowest vertex


# ---------------------------------------------------------------- subdivision


@pytest.mark.blender
@pytest.mark.cpu
def test_native_subdivision_falls_back_when_a_material_reads_intrinsics(exported):
    pointy, plain = exported["SubPointy"], exported["SubPlain"]
    assert "error" not in pointy and "error" not in plain, (pointy, plain)
    assert pointy["meshes"]["SubPointy"]["subdivision"] is None
    assert any(
        d["code"] == "blend.subdivision_evaluated" and "Pointiness" in d["message"] for d in pointy["diagnostics"]
    ), pointy["diagnostics"]
    assert plain["meshes"]["SubPlain"]["subdivision"] is not None
