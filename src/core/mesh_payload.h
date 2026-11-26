// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
#include <algorithm>
#include <cstring>
#include <tuple>
#include <type_traits>
#include <unordered_set>

namespace cycles_houdini {
// Bindings have their own identity. These immutable values describe only the
// owned mesh payload, including shading fields and every shutter snapshot.
template <class T> auto mesh_payload_members(T &m) {
  return std::tie(m.positions, m.triangles, m.corner_normals, m.corner_uv, m.corner_colors, m.smooth, m.motion,
                  m.motion_corner_normals, m.motion_corner_uv, m.motion_corner_colors, m.motion_uv_complete,
                  m.motion_color_complete, m.nominal_pose, m.motion_blur, m.attributes, m.pointiness, m.texture_space,
                  m.blender_edges, m.blender_vertex_normals);
}
template <class T> auto subdivision_payload_members(T &s) {
  return std::tie(s.settings, s.face_sizes, s.face_corners, s.face_provenance, s.edge_creases, s.vertex_creases);
}
inline auto subdivision_render_settings(const SubdivisionSettings &s) {
  return std::tie(s.type, s.space, s.boundary, s.fvar, s.dicing_rate, s.max_level, s.triangle_limit, s.adaptive,
                  s.use_creases);
}
inline auto subdivision_shape_members(const SubdivisionData &s) {
  return std::tuple_cat(subdivision_render_settings(s.settings),
                        std::tie(s.face_sizes, s.face_corners, s.face_provenance, s.edge_creases, s.vertex_creases));
}
template <class> struct payload_vector : std::false_type {};
template <class T> struct payload_vector<std::vector<T>> : std::true_type {};
template <class T> struct payload_vector<SharedArray<T>> : std::true_type {};
template <class> struct payload_optional : std::false_type {};
template <class T> struct payload_optional<std::optional<T>> : std::true_type {};
template <class> struct payload_tuple : std::false_type {};
template <class... T> struct payload_tuple<std::tuple<T...>> : std::true_type {};
template <class T>
constexpr bool payload_bulk = std::is_arithmetic_v<T> || std::is_same_v<T, Float2> || std::is_same_v<T, Float3> ||
                              std::is_same_v<T, Triangle> || std::is_same_v<T, Matrix>;

template <class T> bool mesh_payload_equal(const T &a, const T &b) {
  using U = std::remove_cvref_t<T>;
  if constexpr (payload_bulk<U>)
    return std::memcmp(&a, &b, sizeof(U)) == 0;
  else if constexpr (payload_vector<U>::value) {
    if (a.size() != b.size())
      return false;
    if constexpr (requires { a.storage_identity(); })
      if (a.storage_identity() == b.storage_identity())
        return true;
    if constexpr (payload_bulk<typename U::value_type>)
      return a.empty() || std::memcmp(a.data(), b.data(), a.size() * sizeof(typename U::value_type)) == 0;
    else {
      for (std::size_t i = 0; i < a.size(); ++i)
        if (!mesh_payload_equal(a[i], b[i]))
          return false;
      return true;
    }
  } else if constexpr (payload_optional<U>::value)
    return bool(a) == bool(b) && (!a || mesh_payload_equal(*a, *b));
  else if constexpr (payload_tuple<U>::value)
    return [&]<std::size_t... I>(std::index_sequence<I...>) {
      return (mesh_payload_equal(std::get<I>(a), std::get<I>(b)) && ...);
    }(std::make_index_sequence<std::tuple_size_v<U>>{});
  else if constexpr (std::is_same_v<U, GaussianSplatData>)
    return mesh_payload_equal(std::tie(a.scene_linear_color, a.radiance, a.scales, a.rotations, a.harmonics,
                                       a.motion_scales, a.motion_rotations, a.motion_radiance),
                              std::tie(b.scene_linear_color, b.radiance, b.scales, b.rotations, b.harmonics,
                                       b.motion_scales, b.motion_rotations, b.motion_radiance));
  else if constexpr (std::is_same_v<U, GeometryMotion>)
    return mesh_payload_equal(std::tie(a.positions, a.radii), std::tie(b.positions, b.radii));
  else if constexpr (std::is_same_v<U, NamedAttribute>)
    return mesh_payload_equal(std::tie(a.name, a.components, a.values, a.motion_values, a.motion_complete),
                              std::tie(b.name, b.components, b.values, b.motion_values, b.motion_complete));
  else if constexpr (std::is_same_v<U, MeshNominalPose>)
    return mesh_payload_equal(std::tie(a.positions, a.corner_normals, a.corner_uv, a.corner_colors, a.attributes),
                              std::tie(b.positions, b.corner_normals, b.corner_uv, b.corner_colors, b.attributes));
  else if constexpr (std::is_same_v<U, SubdivisionSettings>)
    return a.cache_topology == b.cache_topology &&
           mesh_payload_equal(subdivision_render_settings(a), subdivision_render_settings(b));
  else if constexpr (std::is_same_v<U, EdgeCrease>)
    return mesh_payload_equal(std::tie(a.a, a.b, a.sharpness), std::tie(b.a, b.b, b.sharpness));
  else if constexpr (std::is_same_v<U, VertexCrease>)
    return mesh_payload_equal(std::tie(a.point, a.sharpness), std::tie(b.point, b.sharpness));
  else
    return a == b;
}
template <class T> bool mesh_payload_equal(const std::vector<T> &a, const SharedArray<T> &b) {
  return mesh_payload_equal(a, static_cast<const std::vector<T> &>(b));
}
template <class T> bool mesh_payload_equal(const SharedArray<T> &a, const std::vector<T> &b) {
  return mesh_payload_equal(static_cast<const std::vector<T> &>(a), b);
}
inline bool same_mesh_shape_payload(const MeshData &a, const MeshData &b) {
  return mesh_payload_equal(mesh_payload_members(a), mesh_payload_members(b)) &&
         bool(a.subdivision) == bool(b.subdivision) &&
         (!a.subdivision ||
          mesh_payload_equal(subdivision_shape_members(*a.subdivision), subdivision_shape_members(*b.subdivision)));
}
inline bool same_mesh_wire_payload(const MeshData &a, const MeshData &b) {
  return same_mesh_shape_payload(a, b) &&
         (!a.subdivision || a.subdivision->settings.cache_topology == b.subdivision->settings.cache_topology);
}
inline void copy_mesh_payload(MeshData &target, const MeshData &source) {
  mesh_payload_members(target) = mesh_payload_members(source);
  if (source.subdivision) {
    auto &data = target.subdivision.emplace();
    subdivision_payload_members(data) = subdivision_payload_members(*source.subdivision);
  } else
    target.subdivision.reset();
}

// Wire masks split optional poses and shutter samples into independently owned
// fields. Bindings are deliberately absent from all three groups.
template <class T> auto mesh_wire_members(T &m) {
  return std::tie(m.positions, m.triangles, m.corner_normals, m.corner_uv, m.corner_colors, m.pointiness, m.smooth,
                  m.motion.positions, m.motion.radii, m.motion_corner_normals, m.motion_corner_uv,
                  m.motion_corner_colors, m.motion_uv_complete, m.motion_color_complete, m.motion_blur, m.attributes,
                  m.texture_space, m.blender_edges, m.blender_vertex_normals);
}
template <class T> auto nominal_wire_members(T &m) {
  return std::tie(m.positions, m.corner_normals, m.corner_uv, m.corner_colors, m.attributes);
}
template <class A, class B> std::uint64_t mesh_changed_mask(const A &values, const B &base) {
  return [&]<std::size_t... I>(std::index_sequence<I...>) {
    return (std::uint64_t(0) | ... |
            (mesh_payload_equal(std::get<I>(values), std::get<I>(base)) ? 0 : (std::uint64_t(1) << I)));
  }(std::make_index_sequence<std::tuple_size_v<A>>{});
}

// A cheap, bounded candidate bucket; it never proves equality. Full bitwise
// field comparison above prevents collisions, signed-zero loss and wrong reuse.
inline std::uint64_t mesh_payload_bucket(const MeshData &m) {
  std::uint64_t hash = 0x9e3779b97f4a7c15ull;
  const auto add = [&](std::uint64_t value) { hash ^= value + 0x9e3779b97f4a7c15ull + (hash << 6) + (hash >> 2); };
  const auto sample = [&](const auto &values) {
    add(values.size());
    if (values.empty())
      return;
    for (const auto index : {std::size_t(0), values.size() / 2, values.size() - 1}) {
      const auto *bytes = reinterpret_cast<const unsigned char *>(&values[index]);
      for (std::size_t i = 0; i < sizeof(values[index]); ++i)
        add(bytes[i]);
    }
  };
  sample(m.positions);
  sample(m.triangles);
  add(bool(m.subdivision));
  return hash;
}

template <class T> std::uint64_t mesh_payload_heap(const T &value) {
  using U = std::remove_cvref_t<T>;
  if constexpr (std::is_same_v<U, std::string>)
    return value.size() + 1;
  else if constexpr (payload_vector<U>::value) {
    std::uint64_t bytes = std::uint64_t(value.size()) * sizeof(typename U::value_type);
    if constexpr (!payload_bulk<typename U::value_type>)
      for (const auto &entry : value)
        bytes += mesh_payload_heap(entry);
    return bytes;
  } else if constexpr (payload_optional<U>::value)
    return value ? mesh_payload_heap(*value) : 0;
  else if constexpr (payload_tuple<U>::value)
    return std::apply([](const auto &...entry) { return (std::uint64_t(0) + ... + mesh_payload_heap(entry)); }, value);
  else if constexpr (std::is_same_v<U, GaussianSplatData>)
    return mesh_payload_heap(std::tie(value.scene_linear_color, value.radiance, value.scales, value.rotations,
                                      value.harmonics, value.motion_scales, value.motion_rotations,
                                      value.motion_radiance));
  else if constexpr (std::is_same_v<U, GeometryMotion>)
    return mesh_payload_heap(std::tie(value.positions, value.radii));
  else if constexpr (std::is_same_v<U, NamedAttribute>)
    return mesh_payload_heap(std::tie(value.name, value.values, value.motion_values));
  else if constexpr (std::is_same_v<U, MeshNominalPose>)
    return mesh_payload_heap(
        std::tie(value.positions, value.corner_normals, value.corner_uv, value.corner_colors, value.attributes));
  else
    return 0;
}
// Account each managed allocation once across logical bindings, nominal/seam
// sources and retained physical-family representatives. Ordinary containers
// remain independently owned. Includes capacity, not a claim of allocator RSS.
template <class T> std::uint64_t mesh_payload_owned_heap(const T &value, std::unordered_set<const void *> &seen) {
  using U = std::remove_cvref_t<T>;
  if constexpr (std::is_same_v<U, std::string>)
    return value.capacity() + 1;
  else if constexpr (payload_vector<U>::value) {
    std::uint64_t overhead{};
    if constexpr (requires { value.storage_identity(); }) {
      const auto owner = value.storage_identity();
      if (!owner || !seen.insert(owner).second)
        return 0;
      overhead = sizeof(std::vector<typename U::value_type>) + 2 * sizeof(void *);
    }
    auto bytes = overhead + std::uint64_t(value.capacity()) * sizeof(typename U::value_type);
    if constexpr (!payload_bulk<typename U::value_type>)
      for (const auto &entry : value)
        bytes += mesh_payload_owned_heap(entry, seen);
    return bytes;
  } else if constexpr (payload_optional<U>::value)
    return value ? mesh_payload_owned_heap(*value, seen) : 0;
  else if constexpr (payload_tuple<U>::value)
    return std::apply(
        [&](const auto &...entry) { return (std::uint64_t(0) + ... + mesh_payload_owned_heap(entry, seen)); }, value);
  else if constexpr (std::is_same_v<U, GaussianSplatData>)
    return mesh_payload_owned_heap(std::tie(value.scene_linear_color, value.radiance, value.scales, value.rotations,
                                            value.harmonics, value.motion_scales, value.motion_rotations,
                                            value.motion_radiance),
                                   seen);
  else if constexpr (std::is_same_v<U, GeometryMotion>)
    return mesh_payload_owned_heap(std::tie(value.positions, value.radii), seen);
  else if constexpr (std::is_same_v<U, NamedAttribute>)
    return mesh_payload_owned_heap(std::tie(value.name, value.values, value.motion_values), seen);
  else if constexpr (std::is_same_v<U, MeshNominalPose>)
    return mesh_payload_owned_heap(nominal_wire_members(value), seen);
  else
    return 0;
}
inline std::uint64_t mesh_owned_heap_bytes(const MeshData &m, std::unordered_set<const void *> &seen) {
  return mesh_payload_owned_heap(mesh_payload_members(m), seen) + mesh_payload_owned_heap(m.triangle_material, seen) +
         (m.subdivision ? mesh_payload_owned_heap(subdivision_payload_members(*m.subdivision), seen) +
                              mesh_payload_owned_heap(m.subdivision->face_material, seen)
                        : 0);
}

inline std::uint64_t mesh_payload_heap_bytes(const MeshData &m) {
  return mesh_payload_heap(mesh_payload_members(m)) +
         (m.subdivision ? mesh_payload_heap(subdivision_payload_members(*m.subdivision)) : 0);
}
} // namespace cycles_houdini
