// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
#include <algorithm>
#include <array>
#include <cmath>
#include <limits>
#include <numeric>
#include <stdexcept>
#include <type_traits>

namespace cycles_houdini {
// Render original polygon vertices without invoking OpenSubdiv or adaptive
// dicing. Ear clipping preserves concave faces and source corner ownership.
inline MeshData coarse_subdivision_mesh(const MeshData &source, std::vector<int> &triangle_faces) {
  MeshData result = source;
  result.subdivision.reset();
  result.triangles.clear();
  result.triangle_material.clear();
  triangle_faces.clear();
  std::vector<int> corners;
  const auto &subd = *source.subdivision;
  std::size_t offset = 0;
  for (std::size_t face = 0; face < subd.face_sizes.size(); ++face) {
    const int count = subd.face_sizes[face];
    std::array<double, 3> normal{};
    for (int n = 0; n < count; ++n) {
      const auto a = source.positions[subd.face_corners[offset + n]],
                 b = source.positions[subd.face_corners[offset + (n + 1) % count]];
      normal[0] += (double(a.y) - b.y) * (double(a.z) + b.z);
      normal[1] += (double(a.z) - b.z) * (double(a.x) + b.x);
      normal[2] += (double(a.x) - b.x) * (double(a.y) + b.y);
    }
    int axis = 0;
    for (int n = 1; n < 3; ++n)
      if (std::abs(normal[n]) > std::abs(normal[axis]))
        axis = n;
    std::vector<std::array<double, 2>> points;
    points.reserve(count);
    for (int n = 0; n < count; ++n) {
      const auto p = source.positions[subd.face_corners[offset + n]];
      points.push_back(axis == 0   ? std::array<double, 2>{p.y, p.z}
                       : axis == 1 ? std::array<double, 2>{p.z, p.x}
                                   : std::array<double, 2>{p.x, p.y});
    }
    const auto cross = [&](int a, int b, int c) {
      return (points[b][0] - points[a][0]) * (points[c][1] - points[a][1]) -
             (points[b][1] - points[a][1]) * (points[c][0] - points[a][0]);
    };
    double area = 0, scale = 0;
    for (int n = 0; n < count; ++n) {
      const auto &a = points[n];
      const auto &b = points[(n + 1) % count];
      area += cross(0, n, (n + 1) % count);
      scale = std::max(scale, std::max(std::abs(b[0] - a[0]), std::abs(b[1] - a[1])));
    }
    const double sign = area < 0 ? -1 : 1, epsilon = scale * scale * 1e-12;
    const auto add = [&](int a, int b, int c) {
      if (corners.size() > std::size_t(std::numeric_limits<int>::max()) - 3)
        throw std::invalid_argument("Coarse polygon preview exceeds triangle indexing limits");
      for (int n : {a, b, c})
        corners.push_back(int(offset) + n);
      result.triangles.push_back(
          {subd.face_corners[offset + a], subd.face_corners[offset + b], subd.face_corners[offset + c]});
      triangle_faces.push_back(int(face));
      if (!subd.face_material.empty())
        result.triangle_material.push_back(subd.face_material[face]);
    };
    std::vector<int> remaining(count);
    std::iota(remaining.begin(), remaining.end(), 0);
    while (remaining.size() > 3) {
      bool clipped = false;
      for (std::size_t n = 0; n < remaining.size(); ++n) {
        const int a = remaining[(n + remaining.size() - 1) % remaining.size()], b = remaining[n],
                  c = remaining[(n + 1) % remaining.size()];
        const double turn = sign * cross(a, b, c);
        if (turn < -epsilon)
          continue;
        bool occupied = false;
        if (turn > epsilon)
          for (int other : remaining)
            if (other != a && other != b && other != c && sign * cross(a, b, other) >= -epsilon &&
                sign * cross(b, c, other) >= -epsilon && sign * cross(c, a, other) >= -epsilon) {
              occupied = true;
              break;
            }
        if (occupied)
          continue;
        add(a, b, c);
        remaining.erase(remaining.begin() + n);
        clipped = true;
        break;
      }
      if (!clipped)
        throw std::invalid_argument("Cannot triangulate control polygon for unsubdivided preview");
    }
    add(remaining[0], remaining[1], remaining[2]);
    offset += count;
  }
  const auto remap = [&](auto &values) {
    if (values.empty())
      return;
    std::decay_t<decltype(values)> mapped;
    mapped.reserve(corners.size());
    for (int corner : corners)
      mapped.push_back(values.at(corner));
    values = std::move(mapped);
  };
  const auto fields = [&](auto &attributes) {
    for (auto &field : attributes) {
      remap(field.values);
      for (auto &step : field.motion_values)
        remap(step);
    }
  };
  remap(result.corner_normals);
  remap(result.corner_uv);
  remap(result.corner_colors);
  for (auto &step : result.motion_corner_normals)
    remap(step);
  for (auto &step : result.motion_corner_uv)
    remap(step);
  for (auto &step : result.motion_corner_colors)
    remap(step);
  fields(result.attributes);
  if (result.nominal_pose) {
    remap(result.nominal_pose->corner_normals);
    remap(result.nominal_pose->corner_uv);
    remap(result.nominal_pose->corner_colors);
    fields(result.nominal_pose->attributes);
  }
  return result;
}
} // namespace cycles_houdini
