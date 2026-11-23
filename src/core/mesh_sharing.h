// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "mesh_payload.h"
#include <bit>
#include <stdexcept>
#include <unordered_set>

namespace cycles_houdini {
// Aggregate the same pairwise ownership restrictions without revisiting every
// earlier logical binding. Values remain subject to exact field comparisons.
struct MeshSharingFieldOwners {
  std::unordered_set<std::string> fields, uniform_only;
  bool corner_color{}, uniform_color_only{};
  bool compatible(const MeshSharingFieldOwners &other) const {
    if ((corner_color && other.uniform_color_only) || (other.corner_color && uniform_color_only))
      return false;
    for (const auto &name : other.uniform_only)
      if (fields.contains(name))
        return false;
    for (const auto &name : other.fields)
      if (uniform_only.contains(name))
        return false;
    return true;
  }
  void include(const MeshSharingFieldOwners &other) {
    fields.insert(other.fields.begin(), other.fields.end());
    uniform_only.insert(other.uniform_only.begin(), other.uniform_only.end());
    corner_color |= other.corner_color;
    uniform_color_only |= other.uniform_color_only;
  }
};
// The physical shape excludes material identities and optional shading fields.
// Equality remains exact; a hash only selects candidates for this comparison.
template <class T> auto mesh_shape_members(T &m) {
  // The Blender texture space decides Generated coordinates, and Blender's edges and vertex
  // normals decide Pointiness and Random per Island, so they count as shape.
  return std::tie(m.positions, m.triangles, m.corner_normals, m.smooth, m.motion, m.motion_corner_normals,
                  m.motion_blur, m.texture_space, m.blender_edges, m.blender_vertex_normals);
}
inline bool same_material_slots(const std::vector<int> &a, const std::vector<int> &b) {
  if (a.empty())
    return std::all_of(b.begin(), b.end(), [](int value) { return value == 0; });
  if (b.empty())
    return std::all_of(a.begin(), a.end(), [](int value) { return value == 0; });
  return mesh_payload_equal(a, b);
}
inline bool same_mesh_shape(const MeshData &a, const MeshData &b) {
  if (!mesh_payload_equal(mesh_shape_members(a), mesh_shape_members(b)) ||
      !same_material_slots(a.triangle_material, b.triangle_material) || bool(a.subdivision) != bool(b.subdivision) ||
      bool(a.nominal_pose) != bool(b.nominal_pose))
    return false;
  if (a.subdivision &&
      (!mesh_payload_equal(subdivision_payload_members(*a.subdivision), subdivision_payload_members(*b.subdivision)) ||
       !same_material_slots(a.subdivision->face_material, b.subdivision->face_material)))
    return false;
  if (a.nominal_pose && !mesh_payload_equal(std::tie(a.nominal_pose->positions, a.nominal_pose->corner_normals),
                                            std::tie(b.nominal_pose->positions, b.nominal_pose->corner_normals)))
    return false;
  return true;
}

inline void mesh_shape_hash_bytes(std::uint64_t &hash, const void *data, std::size_t size) {
  const auto *bytes = static_cast<const unsigned char *>(data);
  while (size >= 8) {
    std::uint64_t value;
    std::memcpy(&value, bytes, 8);
    hash = std::rotl(hash ^ value, 27) * 0x9e3779b185ebca87ull;
    bytes += 8;
    size -= 8;
  }
  std::uint64_t tail{};
  if (size)
    std::memcpy(&tail, bytes, size);
  hash = std::rotl(hash ^ tail ^ size, 31) * 0xc2b2ae3d27d4eb4full;
}
template <class T> void mesh_shape_hash_value(std::uint64_t &hash, const T &value) {
  using U = std::remove_cvref_t<T>;
  if constexpr (payload_bulk<U>)
    mesh_shape_hash_bytes(hash, &value, sizeof(value));
  else if constexpr (payload_vector<U>::value) {
    mesh_shape_hash_value(hash, value.size());
    if constexpr (payload_bulk<typename U::value_type>)
      mesh_shape_hash_bytes(hash, value.data(), value.size() * sizeof(typename U::value_type));
    else
      for (const auto &item : value)
        mesh_shape_hash_value(hash, item);
  } else if constexpr (requires { std::tuple_size<U>::value; })
    std::apply([&](const auto &...item) { (mesh_shape_hash_value(hash, item), ...); }, value);
  else if constexpr (std::is_same_v<U, GeometryMotion>)
    mesh_shape_hash_value(hash, std::tie(value.positions, value.radii));
  else if constexpr (std::is_same_v<U, SubdivisionSettings>)
    mesh_shape_hash_value(hash, std::tie(value.type, value.space, value.boundary, value.fvar, value.dicing_rate,
                                         value.max_level, value.triangle_limit, value.adaptive, value.use_creases));
  else if constexpr (std::is_same_v<U, EdgeCrease>)
    mesh_shape_hash_value(hash, std::tie(value.a, value.b, value.sharpness));
  else if constexpr (std::is_same_v<U, VertexCrease>)
    mesh_shape_hash_value(hash, std::tie(value.point, value.sharpness));
  else if constexpr (std::is_enum_v<U>) {
    const auto scalar = static_cast<std::underlying_type_t<U>>(value);
    mesh_shape_hash_value(hash, scalar);
  } else
    static_assert(sizeof(U) == 0, "Unsupported exact shape hash field");
}
inline std::uint64_t mesh_shape_hash(const MeshData &m) {
  std::uint64_t result = 0x6a09e667f3bcc909ull;
  mesh_shape_hash_value(result, mesh_shape_members(m));
  const auto slots = [&](const auto &values) {
    const bool zero = std::all_of(values.begin(), values.end(), [](int v) { return v == 0; });
    mesh_shape_hash_value(result, zero);
    if (!zero)
      mesh_shape_hash_value(result, values);
  };
  slots(m.triangle_material);
  mesh_shape_hash_value(result, bool(m.subdivision));
  if (m.subdivision) {
    mesh_shape_hash_value(result, subdivision_payload_members(*m.subdivision));
    slots(m.subdivision->face_material);
  }
  mesh_shape_hash_value(result, bool(m.nominal_pose));
  if (m.nominal_pose)
    mesh_shape_hash_value(result, std::tie(m.nominal_pose->positions, m.nominal_pose->corner_normals));
  return result;
}

template <class T> bool compatible_mesh_field(const T &a, const T &b) {
  return a.empty() || b.empty() || mesh_payload_equal(a, b);
}
inline bool compatible_named_mesh_fields(const std::vector<NamedAttribute> &a, const std::vector<NamedAttribute> &b) {
  for (const auto &field : b) {
    const auto found = std::find_if(a.begin(), a.end(), [&](const auto &value) { return value.name == field.name; });
    if (found != a.end() && !mesh_payload_equal(*found, field))
      return false;
  }
  return true;
}
// Missing fields do not replace their logical binding context. The caller keeps
// those contexts and their recovered shaders separate from this shared view.
inline bool compatible_mesh_shading_fields(const MeshData &a, const MeshData &b) {
  if (!a.corner_uv.empty() && !b.corner_uv.empty() &&
      !mesh_payload_equal(std::tie(a.corner_uv, a.motion_corner_uv, a.motion_uv_complete),
                          std::tie(b.corner_uv, b.motion_corner_uv, b.motion_uv_complete)))
    return false;
  if (!a.corner_colors.empty() && !b.corner_colors.empty() &&
      !mesh_payload_equal(std::tie(a.corner_colors, a.motion_corner_colors, a.motion_color_complete),
                          std::tie(b.corner_colors, b.motion_corner_colors, b.motion_color_complete)))
    return false;
  if (!compatible_mesh_field(a.pointiness, b.pointiness) || !compatible_named_mesh_fields(a.attributes, b.attributes))
    return false;
  if (a.nominal_pose) {
    const auto &x = *a.nominal_pose;
    const auto &y = *b.nominal_pose;
    if (!compatible_mesh_field(x.corner_uv, y.corner_uv) || !compatible_mesh_field(x.corner_colors, y.corner_colors) ||
        !compatible_named_mesh_fields(x.attributes, y.attributes))
      return false;
  }
  return true;
}
inline bool adds_mesh_shading_fields(const MeshData &a, const MeshData &b) {
  const auto adds = [](const auto &x, const auto &y) {
    return std::any_of(y.begin(), y.end(), [&](const auto &field) {
      return std::none_of(x.begin(), x.end(), [&](const auto &value) { return value.name == field.name; });
    });
  };
  return (a.corner_uv.empty() && !b.corner_uv.empty()) || (a.corner_colors.empty() && !b.corner_colors.empty()) ||
         (a.pointiness.empty() && !b.pointiness.empty()) || adds(a.attributes, b.attributes) ||
         (a.nominal_pose && ((a.nominal_pose->corner_uv.empty() && !b.nominal_pose->corner_uv.empty()) ||
                             (a.nominal_pose->corner_colors.empty() && !b.nominal_pose->corner_colors.empty()) ||
                             adds(a.nominal_pose->attributes, b.nominal_pose->attributes)));
}
inline void merge_mesh_shading_fields(MeshData &target, const MeshData &source) {
  if (!compatible_mesh_shading_fields(target, source))
    throw std::logic_error("Incompatible owned mesh shading union");
  const auto merge = [](auto &a, const auto &b) {
    if (a.empty())
      a = b;
  };
  const auto named = [](auto &a, const auto &b) {
    for (const auto &field : b)
      if (std::none_of(a.begin(), a.end(), [&](const auto &value) { return value.name == field.name; }))
        a.push_back(field);
  };
  if (target.corner_uv.empty() && !source.corner_uv.empty()) {
    target.corner_uv = source.corner_uv;
    target.motion_corner_uv = source.motion_corner_uv;
    target.motion_uv_complete = source.motion_uv_complete;
  }
  if (target.corner_colors.empty() && !source.corner_colors.empty()) {
    target.corner_colors = source.corner_colors;
    target.motion_corner_colors = source.motion_corner_colors;
    target.motion_color_complete = source.motion_color_complete;
  }
  merge(target.pointiness, source.pointiness);
  named(target.attributes, source.attributes);
  if (target.nominal_pose) {
    auto &a = *target.nominal_pose;
    const auto &b = *source.nominal_pose;
    merge(a.corner_uv, b.corner_uv);
    merge(a.corner_colors, b.corner_colors);
    named(a.attributes, b.attributes);
  }
}
} // namespace cycles_houdini
