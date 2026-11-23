// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
#include "mesh_payload.h"
#include "util/disjoint_set.h"
#include "util/hash.h"
#include <algorithm>
#include <limits>

namespace cycles_houdini {
// Connectivity belongs to the owned control mesh, before displacement splits
// UV/material seams. Neither position edits nor object copies define islands.
inline bool same_island_topology(const MeshData &a, const MeshData &b) {
  if (a.positions.size() != b.positions.size() || bool(a.subdivision) != bool(b.subdivision) ||
      a.blender_vertex_normals.empty() != b.blender_vertex_normals.empty() ||
      !mesh_payload_equal(a.blender_edges, b.blender_edges))
    return false;
  if (a.subdivision)
    return a.subdivision->face_sizes == b.subdivision->face_sizes &&
           a.subdivision->face_corners == b.subdivision->face_corners;
  return a.triangles == b.triangles;
}
inline std::vector<float> mesh_island_random(const MeshData &mesh) {
  ccl::DisjointSet sets(mesh.positions.size());
  // Link meshes follow Blender v5.3.0 intern/cycles/blender/mesh.cpp
  // (attr_create_random_per_island, Apache-2.0) exactly: the same DisjointSet joined over
  // Blender's edges() in edge order (loose edges included); the value is the hash of the
  // union-find root of the face's first corner (first corner of each Blender loop triangle,
  // or of each polygon for subdivision cages).
  // https://github.com/blender/blender/blob/v5.3.0/intern/cycles/blender/mesh.cpp
  const bool blender = !mesh.blender_vertex_normals.empty();
  if (blender) {
    const auto &edges = mesh.blender_edges;
    for (std::size_t e = 0; e + 1 < edges.size(); e += 2)
      sets.join(std::size_t(edges[e]), std::size_t(edges[e + 1]));
    std::vector<float> result;
    result.reserve(mesh.subdivision ? mesh.subdivision->face_sizes.size() : mesh.triangles.size());
    const auto emit = [&](int point) { result.push_back(ccl::hash_uint_to_float(unsigned(sets.find(point)))); };
    if (mesh.subdivision) {
      std::size_t offset = 0;
      for (const auto count : mesh.subdivision->face_sizes) {
        emit(mesh.subdivision->face_corners[offset]);
        offset += std::size_t(count);
      }
    } else
      for (const auto &face : mesh.triangles)
        emit(face.a);
    return result;
  }
  // Houdini meshes: validation of owned indexing precedes this helper. Do not weld equal
  // positions: disconnected coincident points remain distinct mesh islands.
  if (mesh.subdivision) {
    std::size_t offset = 0;
    for (const auto count : mesh.subdivision->face_sizes) {
      const auto &corners = mesh.subdivision->face_corners;
      for (int i = 1; i < count; ++i)
        sets.join(corners[offset], corners[offset + std::size_t(i)]);
      offset += std::size_t(count);
    }
  } else
    for (const auto &face : mesh.triangles) {
      sets.join(face.a, face.b);
      sets.join(face.a, face.c);
    }
  std::vector<unsigned> first(mesh.positions.size(), std::numeric_limits<unsigned>::max());
  for (std::size_t point = 0; point < mesh.positions.size(); ++point) {
    const auto root = sets.find(point);
    first[root] = std::min(first[root], unsigned(point));
  }
  std::vector<float> result;
  result.reserve(mesh.subdivision ? mesh.subdivision->face_sizes.size() : mesh.triangles.size());
  const auto emit = [&](int point) { result.push_back(ccl::hash_uint_to_float(first[sets.find(point)])); };
  // All unions are already complete. The minimum point makes values
  // independent of face order and of the union-by-rank representative.
  if (mesh.subdivision) {
    std::size_t offset = 0;
    for (const auto count : mesh.subdivision->face_sizes) {
      emit(mesh.subdivision->face_corners[offset]);
      offset += std::size_t(count);
    }
  } else
    for (const auto &face : mesh.triangles)
      emit(face.a);
  return result;
}
} // namespace cycles_houdini
