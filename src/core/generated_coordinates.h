// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
#include "util/transform.h"
#include <algorithm>
#include <span>

namespace cycles_houdini {
// Generated texture coordinates.
// Houdini geometry: normalized undeformed SOP bounds; zero extent maps to zero rather than NaN.
// Blender Link meshes carry the object's texture space {location, size} (MeshData::texture_space,
// Blender's BKE_mesh_texspace_get: the pre-modifier box or a custom texture space, a zero-size
// side already set to 1). The mapping follows Blender v5.3.0 intern/cycles/blender/util.h
// mesh_texture_space() and mesh.cpp create_mesh() (Apache-2.0, SPDX-FileCopyrightText:
// 2011-2022 Blender Foundation): size' = 0.5 / size, loc' = loc * size' - 0.5,
// generated = position * size' - loc'. Blender's orco (undeformed coordinates) is not used.
// https://github.com/blender/blender/blob/v5.3.0/intern/cycles/blender/util.h
struct GeneratedCoordinates {
  Float3 low{}, extent{};
  bool texture_space = false;
  Float3 size{}, loc{};
  explicit GeneratedCoordinates(std::span<const Float3> points) { bounds(points); }
  GeneratedCoordinates(std::span<const Float3> points, std::span<const Float3> space) {
    if (space.size() != 2) {
      bounds(points);
      return;
    }
    texture_space = true;
    const auto scale = [](float s) { return s != 0.0f ? 0.5f / s : s; };
    size = {scale(space[1].x), scale(space[1].y), scale(space[1].z)};
    loc = {space[0].x * size.x - 0.5f, space[0].y * size.y - 0.5f, space[0].z * size.z - 0.5f};
  }
  void bounds(std::span<const Float3> points) {
    if (points.empty())
      return;
    low = points.front();
    auto high = low;
    for (const auto p : points) {
      low = {std::min(low.x, p.x), std::min(low.y, p.y), std::min(low.z, p.z)};
      high = {std::max(high.x, p.x), std::max(high.y, p.y), std::max(high.z, p.z)};
    }
    extent = {high.x - low.x, high.y - low.y, high.z - low.z};
  }
  ccl::Transform volume_transform() const {
    const auto reciprocal = [](float e) { return e > 0.0f ? 1.0f / e : 0.0f; };
    const auto scale = texture_space ? size : Float3{reciprocal(extent.x), reciprocal(extent.y), reciprocal(extent.z)};
    const auto origin = texture_space ? loc : Float3{low.x * scale.x, low.y * scale.y, low.z * scale.z};
    return ccl::make_transform(scale.x, 0, 0, -origin.x, 0, scale.y, 0, -origin.y, 0, 0, scale.z, -origin.z);
  }
  Float3 operator()(Float3 p) const {
    if (texture_space)
      return {p.x * size.x - loc.x, p.y * size.y - loc.y, p.z * size.z - loc.z};
    return {extent.x > 0 ? (p.x - low.x) / extent.x : 0, extent.y > 0 ? (p.y - low.y) / extent.y : 0,
            extent.z > 0 ? (p.z - low.z) / extent.z : 0};
  }
};
} // namespace cycles_houdini
