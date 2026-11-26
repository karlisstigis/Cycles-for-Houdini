// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "generated_coordinates.h"
#include "named_attributes.h"
#include "mesh_payload.h"
#include "scene/mesh.h"
#include <charconv>
#include <set>
#include <span>
#include <tuple>

namespace cycles_houdini {
struct DisplacementFields {
  bool uv{}, color{}, pointiness{};
  std::set<std::string> named;
};
inline std::vector<int> displacement_slot_classes(const std::vector<std::string> &slots) {
  std::map<std::string, int> classes;
  std::vector<int> result;
  result.reserve(slots.size());
  for (const auto &slot : slots)
    result.push_back(classes.try_emplace(slot, int(classes.size())).first->second);
  return result;
}
// Surface shader indices and shading fields are independent of geometric
// correspondence. Real control, normal, shutter and dicing edits still rebuild.
inline bool same_mesh_geometry(const MeshData &a, const MeshData &b) {
  const auto values = [](const MeshData &m) {
    return std::tie(m.positions, m.triangles, m.corner_normals, m.smooth, m.motion, m.motion_corner_normals,
                    m.motion_blur, m.texture_space);
  };
  if (!mesh_payload_equal(values(a), values(b)) || bool(a.subdivision) != bool(b.subdivision) ||
      bool(a.nominal_pose) != bool(b.nominal_pose))
    return false;
  if (a.subdivision) {
    const auto &x = *a.subdivision;
    const auto &y = *b.subdivision;
    if (!mesh_payload_equal(subdivision_render_settings(x.settings), subdivision_render_settings(y.settings)) ||
        !mesh_payload_equal(std::tie(x.face_sizes, x.face_corners, x.edge_creases, x.vertex_creases),
                            std::tie(y.face_sizes, y.face_corners, y.edge_creases, y.vertex_creases)))
      return false;
  }
  return !a.nominal_pose || mesh_payload_equal(std::tie(a.nominal_pose->positions, a.nominal_pose->corner_normals),
                                               std::tie(b.nominal_pose->positions, b.nominal_pose->corner_normals));
}
inline const NamedAttribute *named_mesh_field(const std::vector<NamedAttribute> &fields, const std::string &name) {
  const auto found = std::find_if(fields.begin(), fields.end(), [&](const auto &field) { return field.name == name; });
  return found == fields.end() ? nullptr : &*found;
}
inline bool same_displacement_fields(const MeshData &a, const MeshData &b, const DisplacementFields &used) {
  if (used.uv && !mesh_payload_equal(std::tie(a.corner_uv, a.motion_corner_uv, a.motion_uv_complete),
                                     std::tie(b.corner_uv, b.motion_corner_uv, b.motion_uv_complete)))
    return false;
  if (used.color && !mesh_payload_equal(std::tie(a.corner_colors, a.motion_corner_colors, a.motion_color_complete),
                                        std::tie(b.corner_colors, b.motion_corner_colors, b.motion_color_complete)))
    return false;
  if (used.pointiness && !mesh_payload_equal(a.pointiness, b.pointiness))
    return false;
  for (const auto &name : used.named) {
    const auto *x = named_mesh_field(a.attributes, name);
    const auto *y = named_mesh_field(b.attributes, name);
    if (bool(x) != bool(y) || (x && !mesh_payload_equal(*x, *y)))
      return false;
    if (a.nominal_pose && b.nominal_pose) {
      x = named_mesh_field(a.nominal_pose->attributes, name);
      y = named_mesh_field(b.nominal_pose->attributes, name);
      if (bool(x) != bool(y) || (x && !mesh_payload_equal(*x, *y)))
        return false;
    }
  }
  if (a.nominal_pose && b.nominal_pose) {
    if (used.uv && !mesh_payload_equal(a.nominal_pose->corner_uv, b.nominal_pose->corner_uv))
      return false;
    if (used.color && !mesh_payload_equal(a.nominal_pose->corner_colors, b.nominal_pose->corner_colors))
      return false;
  }
  return true;
}
inline bool same_displacement_assignments(const MeshData &a, const std::vector<std::string> &old_slots,
                                          const MeshData &b, const std::vector<std::string> &new_slots) {
  const auto &before = a.subdivision ? a.subdivision->face_material : a.triangle_material;
  const auto &after = b.subdivision ? b.subdivision->face_material : b.triangle_material;
  const auto count = a.subdivision ? a.subdivision->face_sizes.size() : a.triangles.size();
  if (count != (b.subdivision ? b.subdivision->face_sizes.size() : b.triangles.size()))
    return false;
  const std::string empty;
  const auto slot = [&](const auto &palette, int index) -> const std::string & {
    if (palette.empty())
      return empty;
    if (index < 0 || std::size_t(index) >= palette.size())
      throw std::logic_error("Displacement palette ownership mismatch");
    return palette[std::size_t(index)];
  };
  for (std::size_t face = 0; face < count; ++face)
    if (slot(old_slots, before.empty() ? 0 : before[face]) != slot(new_slots, after.empty() ? 0 : after[face]))
      return false;
  return true;
}
inline bool owned_mesh_field_name(std::string_view name) {
  constexpr std::string_view prefix = "houdini:";
  if (!name.starts_with(prefix))
    return false;
  const auto begin = name.data() + prefix.size(), end = name.data() + name.size();
  std::size_t length{};
  const auto parsed = std::from_chars(begin, end, length);
  return parsed.ec == std::errc{} && parsed.ptr != end && *parsed.ptr == ':' &&
         std::size_t(end - parsed.ptr - 1) == length;
}
inline bool managed_mesh_shading(const ccl::Attribute &attr) {
  return attr.std == ccl::ATTR_STD_UV || attr.std == ccl::ATTR_STD_GENERATED || attr.std == ccl::ATTR_STD_POINTINESS ||
         attr.std == ccl::ATTR_STD_RANDOM_PER_ISLAND ||
         (attr.std == ccl::ATTR_STD_NONE &&
          (attr.name == ccl::ustring("Cd") || owned_mesh_field_name(attr.name.string())));
}
inline void write_generated_transform(ccl::AttributeSet &target, const GeneratedCoordinates &coordinates) {
  *target.add(ccl::ATTR_STD_GENERATED_TRANSFORM)->data_for_write<ccl::Transform>() = coordinates.volume_transform();
}
inline void fill_mesh_shading(ccl::AttributeSet &target, ccl::Mesh &mesh, const MeshData &source, bool generated,
                              std::span<const int> vertices, std::span<const float> islands,
                              std::span<const float> pointiness) {
  const bool subd = bool(source.subdivision);
  fill_named_attributes(target, source.attributes, ccl::ATTR_ELEMENT_CORNER, subd, &mesh);
  if (!source.corner_uv.empty()) {
    auto *attr = target.add(ccl::ATTR_STD_UV);
    const auto write = [&](const auto &values, int step) {
      auto *out = attr->data_for_write<ccl::float2>(step);
      for (const auto value : values)
        *out++ = ccl::make_float2(value.x, value.y);
    };
    write(source.corner_uv, 0);
    if (!source.motion_corner_uv.empty()) {
      attr->add_motion(&mesh);
      for (std::size_t step = 0; step < source.motion_corner_uv.size(); ++step)
        write(source.motion_corner_uv[step], int(step + 1));
    }
  }
  if (!source.corner_colors.empty()) {
    auto *attr = target.add(ccl::ustring("Cd"), ccl::TypeColor, ccl::ATTR_ELEMENT_CORNER);
    if (subd)
      attr->flags |= ccl::ATTR_SUBDIVIDE_SMOOTH_FVAR;
    const auto write = [&](const auto &values, int step) {
      auto *out = attr->data_for_write<ccl::packed_float3>(step);
      for (const auto value : values)
        *out++ = ccl::packed_float3(ccl::make_float3(value.x, value.y, value.z));
    };
    write(source.corner_colors, 0);
    if (!source.motion_corner_colors.empty()) {
      attr->add_motion(&mesh);
      for (std::size_t step = 0; step < source.motion_corner_colors.size(); ++step)
        write(source.motion_corner_colors[step], int(step + 1));
    }
  }
  const auto vertex_count = subd ? mesh.get_num_subd_base_verts() : mesh.num_verts();
  if (subd)
    vertices = {};
  const auto owned_vertex = [&](std::size_t vertex) {
    const auto index = vertices.empty() ? vertex : std::size_t(vertices[vertex]);
    if (index >= source.positions.size())
      throw std::logic_error("Retained shading references an unowned vertex");
    return index;
  };
  if (generated || !pointiness.empty()) {
    if ((vertices.empty() ? source.positions.size() : vertices.size()) != vertex_count)
      throw std::logic_error("Retained shading vertex correspondence mismatch");
  }
  if (generated) {
    const auto &positions = source.nominal_pose ? source.nominal_pose->positions : source.positions;
    const GeneratedCoordinates bounds(positions, source.texture_space);
    write_generated_transform(target, bounds);
    auto *out = target.add(ccl::ATTR_STD_GENERATED)->data_for_write<ccl::packed_float3>();
    for (std::size_t vertex = 0; vertex < vertex_count; ++vertex) {
      const auto value = bounds(positions.at(owned_vertex(vertex)));
      *out++ = ccl::packed_float3(ccl::make_float3(value.x, value.y, value.z));
    }
  }
  if (!pointiness.empty()) {
    if (pointiness.size() != source.positions.size())
      throw std::logic_error("Retained Pointiness control ownership mismatch");
    auto *out = target.add(ccl::ATTR_STD_POINTINESS)->data_for_write<float>();
    for (std::size_t vertex = 0; vertex < vertex_count; ++vertex)
      *out++ = pointiness[owned_vertex(vertex)];
  }
  if (!islands.empty()) {
    if (islands.size() != (subd ? source.subdivision->face_sizes.size() : mesh.num_triangles()))
      throw std::logic_error("Retained island face correspondence mismatch");
    std::copy(islands.begin(), islands.end(), target.add(ccl::ATTR_STD_RANDOM_PER_ISLAND)->data_for_write<float>());
  }
}
inline void invalidate_mesh_tangents(ccl::AttributeSet &attributes, const std::set<std::string> &changed) {
  for (const auto &name : changed)
    for (const char *suffix : {".tangent", ".tangent_sign", ".undisplaced_tangent", ".undisplaced_tangent_sign"})
      attributes.remove(ccl::ustring(name + suffix));
}
inline bool refresh_mesh_shading(ccl::Mesh &mesh, const MeshData &source, bool generated, std::span<const int> vertices,
                                 std::span<const float> islands, std::span<const float> pointiness, bool bound) {
  if (mesh.position_is_modified() || mesh.triangles_is_modified() || mesh.need_tessellation())
    return false;
  const auto *samples = mesh.subdivision_shading_samples.get();
  const bool replay = samples && samples->ready && samples->vertices.size() == mesh.num_verts() &&
                      samples->triangles.size() == mesh.num_triangles();
  const auto keep_derived_field = [&](ccl::AttributeStandard field) {
    // These three bounded fields depend on the retained mesh. Keep their
    // completed interpolation if the current surface stops requesting it.
    return source.subdivision && bound && !replay &&
           (field == ccl::ATTR_STD_GENERATED || field == ccl::ATTR_STD_POINTINESS ||
            field == ccl::ATTR_STD_RANDOM_PER_ISLAND);
  };
  auto &target = source.subdivision ? mesh.subd_attributes : mesh.attributes;
  ccl::AttributeSet candidate(&mesh, source.subdivision ? ccl::ATTR_PRIM_SUBD : ccl::ATTR_PRIM_GEOMETRY);
  fill_mesh_shading(candidate, mesh, source, bound && generated, vertices, bound ? islands : std::span<const float>{},
                    bound ? pointiness : std::span<const float>{});
  std::set<std::string> changed;
  bool interpolate = false;
  for (auto it = target.attributes.begin(); it != target.attributes.end();) {
    const auto *replacement = candidate.find_matching(*it);
    if (managed_mesh_shading(*it) &&
        (replacement ? replacement->num_motion_steps() != it->num_motion_steps() : !keep_derived_field(it->std))) {
      changed.insert(it->name.string());
      if (source.subdivision)
        mesh.attributes.remove(it->name);
      auto *removed = &*it++;
      target.remove(removed);
    } else
      ++it;
  }
  if ((!bound || !generated) && !keep_derived_field(ccl::ATTR_STD_GENERATED))
    mesh.attributes.remove(ccl::ATTR_STD_GENERATED_TRANSFORM);
  for (auto &field : candidate.attributes) {
    // Volume samples use the base geometry attribute map, even with subdivision.
    if (field.std == ccl::ATTR_STD_GENERATED_TRANSFORM) {
      mesh.attributes.add_from(std::move(field));
      continue;
    }
    const auto *old = target.find_matching(field);
    const bool flags_changed = old && old->flags != field.flags;
    auto *resolved = target.add_from(std::move(field));
    resolved->modified |= flags_changed || (source.subdivision && !mesh.attributes.find(resolved->name));
    if (resolved->modified)
      changed.insert(resolved->name.string());
    interpolate |= resolved->modified;
  }
  if (source.subdivision) {
    if (interpolate && !mesh.interpolate_subdivision_shading())
      return false;
    const auto &materials = source.subdivision->face_material;
    auto &coarse = mesh.get_subd_shader();
    bool modified = false;
    for (std::size_t face = 0; face < coarse.size(); ++face) {
      const int slot = bound && !materials.empty() ? materials[face] : 0;
      modified |= coarse[face] != slot;
      coarse[face] = slot;
    }
    if (modified) {
      if (!replay)
        return false;
      mesh.tag_subd_shader_modified();
      for (std::size_t triangle = 0; triangle < mesh.num_triangles(); ++triangle) {
        const auto face = samples->triangles[triangle].face;
        if (face < 0 || std::size_t(face) >= coarse.size())
          throw std::logic_error("Retained triangle has no owned control face");
        mesh.get_shader()[triangle] = coarse[std::size_t(face)];
      }
      mesh.tag_shader_modified();
    }
  } else {
    bool modified = false;
    auto &shaders = mesh.get_shader();
    for (std::size_t face = 0; face < shaders.size(); ++face) {
      const int slot = bound && !source.triangle_material.empty() ? source.triangle_material[face] : 0;
      modified |= shaders[face] != slot;
      shaders[face] = slot;
    }
    if (modified)
      mesh.tag_shader_modified();
  }
  invalidate_mesh_tangents(mesh.attributes, changed);
  return true;
}
} // namespace cycles_houdini
