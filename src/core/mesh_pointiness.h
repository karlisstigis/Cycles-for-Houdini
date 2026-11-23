/* SPDX-FileCopyrightText: 2011-2022 Blender Foundation
 * SPDX-License-Identifier: Apache-2.0 */
#pragma once

// Adapted from Blender v5.3.0 intern/cycles/blender/mesh.cpp (attr_create_pointiness),
// Apache-2.0. Link meshes (MeshData::blender_vertex_normals) follow it exactly: Blender's
// vertex normals and edges() in edge order, single precision in the operation order of
// Cycles' SSE4.2 host build (util/math_float3.h). Houdini meshes have no Blender edges or
// normals: they use the faces' edges and angle-weighted face normals instead.
// A spatial index accelerates the original ordered duplicate search, including
// equal-coordinate chains.
// https://github.com/blender/blender/blob/v5.3.0/intern/cycles/blender/mesh.cpp
#include "cycles_houdini/session.h"
#include <algorithm>
#include <array>
#include <bit>
#include <cmath>
#include <cfloat>
#include <limits>
#include <numeric>
#include <span>
#include <stdexcept>
#include <tuple>
#include <unordered_map>
#include <unordered_set>

namespace cycles_houdini {
namespace pointiness_detail {
struct Vector {
  double x{}, y{}, z{};
  Vector operator+(Vector b) const { return {x + b.x, y + b.y, z + b.z}; }
  Vector operator-(Vector b) const { return {x - b.x, y - b.y, z - b.z}; }
  Vector operator*(double scale) const { return {x * scale, y * scale, z * scale}; }
  Vector &operator+=(Vector b) {
    x += b.x;
    y += b.y;
    z += b.z;
    return *this;
  }
};
inline Vector vector(Float3 p) { return {p.x, p.y, p.z}; }
inline double dot(Vector a, Vector b) { return a.x * b.x + a.y * b.y + a.z * b.z; }
inline Vector cross(Vector a, Vector b) {
  return {a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x};
}
inline Vector unit(Vector v) {
  const auto length = std::sqrt(dot(v, v));
  return length > 0 ? v * (1 / length) : Vector{};
}
struct Cell {
  std::array<std::int64_t, 3> axes;
  bool operator==(const Cell &) const = default;
};
struct CellHash {
  std::size_t operator()(const Cell &cell) const {
    std::uint64_t hash = 1469598103934665603ull;
    for (auto axis : cell.axes) {
      hash ^= std::uint64_t(axis);
      hash *= 1099511628211ull;
    }
    return std::size_t(hash);
  }
};
constexpr std::int64_t exact_axis = std::int64_t(1) << 62;
inline std::int64_t cell_axis(float value) {
  // At this magnitude distinct float32 coordinates cannot lie within the
  // welding radius. Exact-bit keys also avoid overflowing integer cells.
  if (std::abs(double(value)) >= 0x1p40)
    return exact_axis + std::bit_cast<std::uint32_t>(value);
  return std::int64_t(std::floor(double(value) / std::sqrt(double(FLT_EPSILON))));
}
inline Cell cell(Float3 p) { return {{cell_axis(p.x), cell_axis(p.y), cell_axis(p.z)}}; }
inline float coordinate_sum(Float3 p) { return p.x + p.y + p.z; }
// Cycles' float3 operations as its SSE4.2 host build evaluates them: dot is
// (x*x + y*y) + z*z (DPPS), normalize divides by the length, safe_normalize
// multiplies by the reciprocal length, and a vector / scalar divides.
inline float fdot(Float3 a, Float3 b) { return (a.x * b.x + a.y * b.y) + a.z * b.z; }
inline Float3 fadd(Float3 a, Float3 b) { return {a.x + b.x, a.y + b.y, a.z + b.z}; }
inline Float3 fsub(Float3 a, Float3 b) { return {a.x - b.x, a.y - b.y, a.z - b.z}; }
inline Float3 fdiv(Float3 a, float f) { return {a.x / f, a.y / f, a.z / f}; }
inline Float3 fnormalize(Float3 a) {
  const float length = std::sqrt(fdot(a, a));
  return {a.x / length, a.y / length, a.z / length};
}
inline Float3 fsafe_normalize(Float3 a) {
  const float length = std::sqrt(fdot(a, a));
  if (length == 0.0f)
    return a;
  const float inverse = 1.0f / length;
  return {a.x * inverse, a.y * inverse, a.z * inverse};
}
// util/math_base.h: clamp is min(max(a, lo), hi) with ternaries, so NaN becomes -1.
inline float fsafe_acos(float a) {
  a = a > -1.0f ? a : -1.0f;
  a = a < 1.0f ? a : 1.0f;
  return std::acos(a);
}

// Blender's duplicate welding (STEP 1): every point maps to the first point of its
// chain of coincident points (squared distance below FLT_EPSILON, single precision).
inline std::vector<int> weld_duplicates(std::span<const Float3> positions) {
  const auto count = positions.size();
  if (count > std::size_t(std::numeric_limits<int>::max()))
    throw std::length_error("Pointiness exceeds the mesh index interface");
  std::vector<int> sorted(count), original(count), rank(count);
  std::iota(sorted.begin(), sorted.end(), 0);
  std::sort(sorted.begin(), sorted.end(), [&](int a, int b) {
    if (positions[a] == positions[b])
      return a > b;
    const auto x = coordinate_sum(positions[a]), y = coordinate_sum(positions[b]);
    if (x != y)
      return x < y;
    // Equal-sum distinct points need a total tie order. Otherwise mixing
    // their equivalence with the duplicate index tie violates sort's
    // strict-weak-order contract (common on planar grids).
    const auto p = positions[a], q = positions[b];
    return std::tie(p.x, p.y, p.z) < std::tie(q.x, q.y, q.z);
  });
  for (std::size_t i = 0; i < count; ++i)
    rank[sorted[i]] = int(i);
  std::unordered_map<Cell, std::vector<int>, CellHash> buckets;
  buckets.reserve(count);
  for (std::size_t reverse = count; reverse > 0; --reverse) {
    const int index = sorted[reverse - 1];
    const auto p = positions[index];
    const auto location = cell(p);
    int found = -1;
    const auto lo = [&](int axis) { return location.axes[axis] >= exact_axis ? 0 : -1; };
    const auto hi = [&](int axis) { return location.axes[axis] >= exact_axis ? 0 : 1; };
    for (int x = lo(0); x <= hi(0); ++x)
      for (int y = lo(1); y <= hi(1); ++y)
        for (int z = lo(2); z <= hi(2); ++z) {
          Cell key = location;
          key.axes[0] += x;
          key.axes[1] += y;
          key.axes[2] += z;
          const auto bucket = buckets.find(key);
          if (bucket == buckets.end())
            continue;
          // Reverse insertion order is the original search's ascending rank.
          // Stop at the first matching candidate in each cell; coincident
          // high-multiplicity points therefore do not become quadratic.
          for (auto it = bucket->second.rbegin(); it != bucket->second.rend(); ++it) {
            const int other = *it;
            if (found >= 0 && rank[other] >= rank[found])
              break;
            if (coordinate_sum(positions[other]) - coordinate_sum(p) > 3 * FLT_EPSILON)
              continue;
            const auto difference = fsub(positions[other], p);
            if (fdot(difference, difference) < FLT_EPSILON) {
              found = other;
              break;
            }
          }
        }
    original[index] = found < 0 ? index : original[found];
    buckets[location].push_back(index);
  }
  return original;
}

// Blender's attr_create_pointiness after welding, for Link meshes: vertex_normals are
// Blender's (Mesh::vert_normals), edges its edges() as vertex pairs in edge order.
inline std::vector<float> blender_pointiness(std::span<const Float3> positions, std::span<const Float3> vertex_normals,
                                             std::span<const int> edges) {
  const auto count = positions.size();
  if (!count)
    return {};
  if (vertex_normals.size() != count || edges.size() % 2)
    throw std::logic_error("Blender pointiness needs one normal per point and edge vertex pairs");
  const auto original = weld_duplicates(positions);
  // STEP 2: accumulate normals into the original point, then normalize (only
  // the originals are read below).
  std::vector<Float3> normal(count);
  for (std::size_t i = 0; i < count; ++i)
    normal[original[i]] = fadd(normal[original[i]], vertex_normals[i]);
  for (std::size_t i = 0; i < count; ++i)
    if (original[i] == int(i))
      normal[i] = fnormalize(normal[i]);
  // STEP 3: single-ring pointiness over the unique welded edges, in edge order.
  std::vector<int> counter(count);
  std::vector<float> raw(count);
  std::vector<Float3> accumulated(count);
  std::unordered_set<std::uint64_t> visited;
  const auto first_visit = [&](int a, int b) {
    if (a > b)
      std::swap(a, b);
    return visited.insert((std::uint64_t(std::uint32_t(a)) << 32) | std::uint32_t(b)).second;
  };
  for (std::size_t e = 0; e + 1 < edges.size(); e += 2) {
    const int v0 = original[edges[e]], v1 = original[edges[e + 1]];
    if (!first_visit(v0, v1))
      continue;
    const auto edge = fsafe_normalize(fsub(positions[v1], positions[v0]));
    accumulated[v0] = fadd(accumulated[v0], edge);
    accumulated[v1] = fadd(accumulated[v1], Float3{-edge.x, -edge.y, -edge.z});
    ++counter[v0];
    ++counter[v1];
  }
  constexpr float inverse_pi = 0.3183098861837067f; // M_1_PI_F
  for (std::size_t i = 0; i < count; ++i)
    if (original[i] == int(i) && counter[i] > 0)
      raw[i] = fsafe_acos(fdot(normal[i], fdiv(accumulated[i], float(counter[i])))) * inverse_pi;
  // STEP 3 (blur): approximate the two-ring neighbourhood.
  std::vector<float> data(raw);
  std::fill(counter.begin(), counter.end(), 0);
  visited.clear();
  for (std::size_t e = 0; e + 1 < edges.size(); e += 2) {
    const int v0 = original[edges[e]], v1 = original[edges[e + 1]];
    if (!first_visit(v0, v1))
      continue;
    data[v0] += raw[v1];
    data[v1] += raw[v0];
    ++counter[v0];
    ++counter[v1];
  }
  for (std::size_t i = 0; i < count; ++i)
    data[i] /= float(counter[i] + 1);
  // STEP 4: duplicates take their original's value.
  for (std::size_t i = 0; i < count; ++i)
    data[i] = data[original[i]];
  return data;
}
} // namespace pointiness_detail

// visit_faces invokes its argument with each original polygon's point indices.
// Callers validate owned indexing first. No shader, live OP or source attribute
// participates; authored N cannot invalidate this intrinsic geometric field.
template <class VisitFaces>
std::vector<float> mesh_pointiness(std::span<const Float3> positions, VisitFaces visit_faces,
                                   bool reverse_winding = false) {
  using namespace pointiness_detail;
  const auto count = positions.size();
  const auto original = weld_duplicates(positions);

  std::vector<Vector> normals(count);
  std::vector<std::array<int, 2>> edges;
  std::unordered_set<std::uint64_t> visited;
  visit_faces([&](std::span<const int> face) {
    if (face.size() < 3)
      return;
    Vector area;
    const auto origin = vector(positions[face[0]]);
    for (std::size_t i = 1; i + 1 < face.size(); ++i)
      area += cross(vector(positions[face[i]]) - origin, vector(positions[face[i + 1]]) - origin);
    auto normal = unit(area);
    if (reverse_winding)
      normal = normal * (-1);
    for (std::size_t i = 0; i < face.size(); ++i) {
      const auto p = vector(positions[face[i]]);
      const auto prev = unit(vector(positions[face[(i + face.size() - 1) % face.size()]]) - p);
      const auto next = unit(vector(positions[face[(i + 1) % face.size()]]) - p);
      const double angle = std::acos(std::clamp(dot(prev, next), -1.0, 1.0));
      normals[face[i]] += normal * angle;
      int a = original[face[i]], b = original[face[(i + 1) % face.size()]];
      if (a > b)
        std::swap(a, b);
      const auto key = (std::uint64_t(std::uint32_t(a)) << 32) | std::uint32_t(b);
      if (visited.insert(key).second)
        edges.push_back({a, b});
    }
  });
  visited.clear();
  visited.rehash(0);
  for (auto &normal : normals)
    normal = unit(normal);
  std::vector<Vector> welded(count);
  for (std::size_t i = 0; i < count; ++i)
    welded[original[i]] += normals[i];
  for (auto &normal : welded)
    normal = unit(normal);
  std::vector<Vector>().swap(normals);
  std::vector<Vector> directions(count);
  std::vector<std::uint32_t> neighbors(count);
  for (const auto edge : edges) {
    const auto direction = unit(vector(positions[edge[1]]) - vector(positions[edge[0]]));
    directions[edge[0]] += direction;
    directions[edge[1]] += direction * (-1);
    ++neighbors[edge[0]];
    ++neighbors[edge[1]];
  }
  constexpr double inverse_pi = 0.31830988618379067154;
  std::vector<float> raw(count);
  for (std::size_t i = 0; i < count; ++i)
    if (original[i] == int(i) && neighbors[i])
      raw[i] = float(std::acos(std::clamp(dot(welded[i], directions[i]) / neighbors[i], -1.0, 1.0)) * inverse_pi);
  std::vector<double> blurred(raw.begin(), raw.end());
  std::fill(neighbors.begin(), neighbors.end(), 0);
  for (const auto edge : edges) {
    blurred[edge[0]] += raw[edge[1]];
    blurred[edge[1]] += raw[edge[0]];
    ++neighbors[edge[0]];
    ++neighbors[edge[1]];
  }
  std::vector<float> result(count);
  for (std::size_t i = 0; i < count; ++i) {
    const auto root = original[i];
    result[i] = float(blurred[root] / (neighbors[root] + 1));
  }
  return result;
}

inline std::vector<float> mesh_pointiness(const MeshData &mesh) {
  const auto positions = mesh.nominal_pose ? std::span<const Float3>(mesh.nominal_pose->positions)
                                           : std::span<const Float3>(mesh.positions);
  // Link meshes: Blender's rule on Blender's edges and normals, for subdivision cages too.
  if (!mesh.blender_vertex_normals.empty())
    return pointiness_detail::blender_pointiness(positions, mesh.blender_vertex_normals, mesh.blender_edges);
  return mesh_pointiness(positions, [&](auto emit) {
    if (mesh.subdivision) {
      std::size_t offset = 0;
      for (int size : mesh.subdivision->face_sizes) {
        emit(std::span<const int>(mesh.subdivision->face_corners).subspan(offset, std::size_t(size)));
        offset += std::size_t(size);
      }
    } else
      for (const auto triangle : mesh.triangles) {
        const std::array face{triangle.a, triangle.b, triangle.c};
        emit(std::span<const int>(face));
      }
  });
}
} // namespace cycles_houdini
