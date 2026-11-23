// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "named_attributes.h"
#include "mesh_shading.h"
#include "cycles_houdini/session.h"
#include "scene/mesh.h"
#include <atomic>
#include <bit>
#include <cstring>
#include <cmath>
#include <climits>
#include <map>
#include <mutex>
#include <set>
#include <stdexcept>

namespace cycles_houdini {
struct SubdivisionMetrics {
  std::mutex mutex;
  bool ready{};
  std::size_t vertices{}, triangles{}, bytes{};
};
inline std::size_t subdivision_source_bytes(const SubdivisionData &data) {
  return (data.face_sizes.size() + data.face_corners.size() + data.face_material.size()) * sizeof(int) +
         data.face_provenance.size() * sizeof(std::array<std::int64_t, 2>) +
         data.edge_creases.size() * sizeof(EdgeCrease) + data.vertex_creases.size() * sizeof(VertexCrease);
}
inline void validate_subdivision(const MeshData &mesh) {
  if (!mesh.subdivision)
    return;
  const auto &data = *mesh.subdivision;
  const auto &settings = data.settings;
  if (settings.type != SubdivisionType::Linear && settings.type != SubdivisionType::CatmullClark)
    throw std::invalid_argument("Coarse faces require Linear or Catmull-Clark subdivision");
  if ((settings.space != SubdivisionSpace::Pixel && settings.space != SubdivisionSpace::Object) ||
      int(settings.boundary) < 0 || int(settings.boundary) > 2 || int(settings.fvar) < 0 || int(settings.fvar) > 5 ||
      !std::isfinite(settings.dicing_rate) || settings.dicing_rate <= 0 || settings.max_level < 0 ||
      settings.max_level > 16)
    throw std::invalid_argument("Invalid subdivision settings: positive dicing rate and level 0-16 required; triangle "
                                "budget zero disables its optional limit");
  if (!mesh.triangles.empty() || !mesh.triangle_material.empty())
    throw std::invalid_argument("Subdivision requires original polygon faces, not render triangles");
  if (settings.type == SubdivisionType::CatmullClark && !settings.adaptive && !mesh.corner_normals.empty())
    throw std::invalid_argument(
        "Catmull-Clark uses limit-surface normals; authored corner normals require Linear subdivision");
  if ((!data.face_material.empty() && data.face_material.size() != data.face_sizes.size()) ||
      (!data.face_provenance.empty() && data.face_provenance.size() != data.face_sizes.size()))
    throw std::invalid_argument("Subdivision face metadata size mismatch");
  if (mesh.positions.size() > std::size_t(INT_MAX) || data.face_sizes.size() > std::size_t(INT_MAX) ||
      data.face_corners.size() > std::size_t(INT_MAX))
    throw std::invalid_argument("Subdivision control mesh exceeds Cycles indexing limits");
  std::size_t offset = 0;
  std::uint64_t patches = 0;
  std::map<std::pair<int, int>, int> edges, orientation;
  for (std::size_t face = 0; face < data.face_sizes.size(); ++face) {
    const auto size = data.face_sizes[face];
    if (size < 3 || std::size_t(size) > data.face_corners.size() - offset)
      throw std::invalid_argument("Invalid subdivision face size");
    patches += size == 4 ? 1 : size;
    std::set<int> vertices;
    for (int corner = 0; corner < size; ++corner) {
      const int a = data.face_corners[offset + corner], b = data.face_corners[offset + (corner + 1) % size];
      if (a < 0 || a >= int(mesh.positions.size()) || !vertices.insert(a).second)
        throw std::invalid_argument("Invalid or repeated subdivision face vertex");
      const auto edge = std::minmax(a, b);
      const std::pair<int, int> key{edge.first, edge.second};
      if (++edges[key] > 2)
        throw std::invalid_argument("Non-manifold subdivision edge requires a supported topology repair");
      const int direction = a < b ? 1 : -1;
      orientation[key] += direction;
      if (edges[key] == 2 && orientation[key] != 0)
        throw std::invalid_argument("Subdivision faces have inconsistent shared-edge orientation");
    }
    offset += size;
  }
  if (offset != data.face_corners.size())
    throw std::invalid_argument("Subdivision face corner count mismatch");
  if (settings.type == SubdivisionType::CatmullClark && settings.boundary == SubdivisionBoundary::None)
    for (const auto &[edge, count] : edges)
      if (count == 1)
        throw std::invalid_argument("No-boundary Catmull-Clark requires a closed control mesh; open boundary holes "
                                    "need a dedicated patch adapter");
  // DiagSplit counts vertices/triangles with signed ints. This conservative
  // bound prevents overflow before its incremental, actual triangle guard.
  if (!settings.adaptive && patches > std::uint64_t(INT_MAX) / (16ull << (2 * settings.max_level)))
    throw std::invalid_argument(
        "Subdivision level/control-mesh combination exceeds safe tessellation indexing; lower the maximum level");
  for (int slot : data.face_material)
    if (slot < 0 || slot >= int(mesh.materials.size()))
      throw std::invalid_argument("Invalid subdivision material slot");
  std::set<std::pair<int, int>> crease_edges;
  for (const auto &crease : data.edge_creases) {
    const auto edge = std::minmax(crease.a, crease.b);
    const std::pair<int, int> key{edge.first, edge.second};
    if (!std::isfinite(crease.sharpness) || crease.sharpness < 0 || crease.sharpness > 10 || !edges.contains(key) ||
        !crease_edges.insert(key).second)
      throw std::invalid_argument("Invalid or duplicate subdivision edge crease");
  }
  std::set<int> crease_vertices;
  for (const auto &crease : data.vertex_creases)
    if (crease.point < 0 || crease.point >= int(mesh.positions.size()) || !std::isfinite(crease.sharpness) ||
        crease.sharpness < 0 || crease.sharpness > 10 || !crease_vertices.insert(crease.point).second)
      throw std::invalid_argument("Invalid or duplicate subdivision vertex crease");
}
inline std::size_t subdivision_engine_bytes(const ccl::Mesh &mesh) {
  std::size_t bytes =
      mesh.get_triangles().size() * sizeof(int) + mesh.get_shader().size() * sizeof(int) +
      mesh.get_smooth().size() * sizeof(bool) +
      (mesh.get_subd_face_corners().size() + mesh.get_subd_start_corner().size() + mesh.get_subd_num_corners().size() +
       mesh.get_subd_shader().size() + mesh.get_subd_ptex_offset().size() + mesh.get_subd_creases_edge().size() +
       mesh.get_subd_vert_creases().size()) *
          sizeof(int) +
      mesh.get_subd_smooth().size() * sizeof(bool) +
      (mesh.get_subd_creases_weight().size() + mesh.get_subd_vert_creases_weight().size()) * sizeof(float);
  for (const auto *attrs : {&mesh.attributes, &mesh.subd_attributes})
    for (const auto &attr : attrs->attributes)
      bytes += attr.size * attr.data_sizeof() * attr.num_motion_steps();
  return bytes + mesh.subdivision_shading_bytes();
}
// Split only generated render vertices. Preserve the connected coarse mesh and
// remap every generated vertex attribute, including all position/normal steps.
inline void subdivision_displacement_seams(ccl::Mesh &mesh, const std::optional<DisplacementFields> &used = {},
                                           const std::vector<int> &classes = {}) {
  const auto *uv = !used || used->uv ? mesh.attributes.find(ccl::ATTR_STD_UV) : nullptr;
  const auto *color = !used || used->color ? mesh.attributes.find(ccl::ustring("Cd")) : nullptr;
  const auto *normal = mesh.attributes.find(ccl::ATTR_STD_CORNER_NORMAL);
  std::vector<const ccl::Attribute *> seam_attributes{uv, color, normal};
  if (used) {
    for (const auto &name : used->named)
      if (const auto *attr = mesh.attributes.find(ccl::ustring(engine_attribute_name(name)));
          attr && attr->element == ccl::ATTR_ELEMENT_CORNER)
        seam_attributes.push_back(attr);
  } else
    for (const auto &attr : mesh.attributes.attributes)
      if (attr.element == ccl::ATTR_ELEMENT_CORNER && owned_mesh_field_name(attr.name.string()))
        seam_attributes.push_back(&attr);
  const auto *triangles = mesh.get_triangles().data();
  const auto *shaders = mesh.get_shader().data();
  const auto old_vertices = mesh.num_verts();
  std::map<std::vector<std::uint32_t>, int> vertices;
  std::vector<int> remap, new_triangles;
  new_triangles.reserve(mesh.num_triangles() * 3);
  for (int face = 0; face < mesh.num_triangles(); ++face)
    for (int corner = 0; corner < 3; ++corner) {
      const int index = face * 3 + corner;
      const int slot = shaders[face];
      if (!classes.empty() && (slot < 0 || std::size_t(slot) >= classes.size()))
        throw std::logic_error("Subdivision displacement slot has no class");
      std::vector<std::uint32_t> key{std::uint32_t(triangles[index]),
                                     std::uint32_t(classes.empty() ? slot : classes[std::size_t(slot)])};
      for (const auto *attr : seam_attributes)
        if (attr) {
          const auto bytes = attr->data_sizeof();
          for (int step = 0; step < attr->num_motion_steps(); ++step) {
            const auto *data = static_cast<const unsigned char *>(attr->data(step)) + std::size_t(index) * bytes;
            for (std::size_t word = 0; word < bytes; word += sizeof(std::uint32_t)) {
              std::uint32_t value;
              std::memcpy(&value, data + word, sizeof(value));
              key.push_back(value);
            }
          }
        }
      const auto [found, inserted] = vertices.emplace(std::move(key), int(remap.size()));
      if (inserted)
        remap.push_back(triangles[index]);
      new_triangles.push_back(found->second);
    }
  // Keep the original vertex indexing, including unused control vertices. A
  // duplicate is appended; this also preserves the coarse/render base prefix.
  std::vector<int> first(old_vertices, -1), indices(remap.size()), expanded(old_vertices);
  for (int v = 0; v < old_vertices; ++v)
    expanded[v] = v;
  for (std::size_t i = 0; i < remap.size(); ++i) {
    const auto source = remap[i];
    if (first[source] < 0) {
      first[source] = int(i);
      indices[i] = source;
    } else {
      indices[i] = int(expanded.size());
      expanded.push_back(source);
    }
  }
  if (expanded.size() == std::size_t(old_vertices))
    return;
  struct Owned {
    ccl::Attribute *attr;
    std::vector<std::vector<unsigned char>> steps;
  };
  std::vector<Owned> owned;
  for (auto &attr : mesh.attributes.attributes)
    if (attr.element == ccl::ATTR_ELEMENT_VERTEX || attr.element == ccl::ATTR_ELEMENT_VERTEX_NORMAL) {
      Owned saved{&attr, {}};
      for (int step = 0; step < attr.num_motion_steps(); ++step) {
        const auto *data = static_cast<const unsigned char *>(attr.data(step));
        saved.steps.emplace_back(data, data + std::size_t(attr.size) * attr.data_sizeof());
      }
      owned.push_back(std::move(saved));
    }
  mesh.resize_subdivision_vertices(int(expanded.size()));
  for (auto &saved : owned)
    for (std::size_t step = 0; step < saved.steps.size(); ++step) {
      const auto bytes = saved.attr->data_sizeof();
      auto *target = static_cast<unsigned char *>(saved.attr->data_for_write(int(step)));
      for (std::size_t v = 0; v < expanded.size(); ++v)
        std::memcpy(target + v * bytes, saved.steps[step].data() + std::size_t(expanded[v]) * bytes, bytes);
    }
  auto *target = mesh.get_triangles().data();
  for (std::size_t i = 0; i < new_triangles.size(); ++i)
    target[i] = indices[new_triangles[i]];
  mesh.remap_subdivision_shading_vertices(expanded.data(), expanded.size());
  mesh.tag_position_modified();
  mesh.tag_triangles_modified();
}
inline void fill_subdivision(ccl::Mesh &mesh, const MeshData &data, bool bound, const ccl::Transform &objecttoworld,
                             const std::shared_ptr<SubdivisionMetrics> &metrics,
                             std::atomic<std::uint64_t> &tessellations, bool displaced,
                             const std::optional<DisplacementFields> &displacement_fields = {},
                             const std::vector<int> &displacement_classes = {}) {
  const auto &subd = *data.subdivision;
  const auto &settings = subd.settings;
  mesh.set_subdivision_type(settings.type == SubdivisionType::CatmullClark ? ccl::Mesh::SUBDIVISION_CATMULL_CLARK
                                                                           : ccl::Mesh::SUBDIVISION_LINEAR);
  mesh.set_subdivision_boundary_interpolation(
      static_cast<ccl::Mesh::SubdivisionBoundaryInterpolation>(settings.boundary));
  mesh.set_subdivision_fvar_interpolation(static_cast<ccl::Mesh::SubdivisionFVarInterpolation>(settings.fvar));
  mesh.set_subd_adaptive_space(settings.adaptive && settings.space == SubdivisionSpace::Pixel
                                   ? ccl::Mesh::SUBDIVISION_ADAPTIVE_SPACE_PIXEL
                                   : ccl::Mesh::SUBDIVISION_ADAPTIVE_SPACE_OBJECT);
  mesh.subdivision_fixed = !settings.adaptive;
  mesh.set_subd_dicing_rate(settings.dicing_rate);
  mesh.set_subd_max_level(settings.max_level);
  mesh.set_subd_objecttoworld(objecttoworld);
  mesh.subdivision_triangle_limit = settings.triangle_limit;
  mesh.resize_subd_faces(int(subd.face_sizes.size()), int(subd.face_corners.size()));
  std::copy(subd.face_corners.begin(), subd.face_corners.end(), mesh.get_subd_face_corners().data());
  int offset = 0, ptex = 0;
  for (std::size_t face = 0; face < subd.face_sizes.size(); ++face) {
    const int size = subd.face_sizes[face];
    mesh.get_subd_start_corner()[face] = offset;
    mesh.get_subd_num_corners()[face] = size;
    mesh.get_subd_shader()[face] = bound && !subd.face_material.empty() ? subd.face_material[face] : 0;
    mesh.get_subd_smooth()[face] =
        settings.type == SubdivisionType::CatmullClark || data.smooth || !data.corner_normals.empty();
    mesh.get_subd_ptex_offset()[face] = ptex;
    offset += size;
    ptex += size == 4 ? 1 : size;
  }
  auto *positions = mesh.subd_attributes.add(ccl::ATTR_STD_POSITION);
  positions->resize(data.positions.size());
  fill_named_attributes(mesh.subd_attributes, data.attributes, ccl::ATTR_ELEMENT_CORNER, true, &mesh);
  auto *p = positions->data_for_write<ccl::packed_float3>();
  for (const auto &v : data.positions)
    *p++ = ccl::packed_float3(ccl::make_float3(v.x, v.y, v.z));
  if (!data.motion.positions.empty()) {
    positions->add_motion(&mesh);
    for (std::size_t step = 0; step < data.motion.positions.size(); ++step) {
      p = positions->data_for_write<ccl::packed_float3>(int(step + 1));
      for (const auto &v : data.motion.positions[step])
        *p++ = ccl::packed_float3(ccl::make_float3(v.x, v.y, v.z));
    }
  }
  if (!data.corner_uv.empty()) {
    auto *attr = mesh.subd_attributes.add(ccl::ATTR_STD_UV);
    auto *uv = attr->data_for_write<ccl::float2>();
    for (const auto &v : data.corner_uv)
      *uv++ = ccl::make_float2(v.x, v.y);
    if (!data.motion_corner_uv.empty()) {
      attr->add_motion(&mesh);
      for (std::size_t step = 0; step < data.motion_corner_uv.size(); ++step) {
        uv = attr->data_for_write<ccl::float2>(int(step + 1));
        for (const auto &v : data.motion_corner_uv[step])
          *uv++ = ccl::make_float2(v.x, v.y);
      }
    }
  }
  if (!data.corner_colors.empty()) {
    auto *attr = mesh.subd_attributes.add(ccl::ustring("Cd"), ccl::TypeColor, ccl::ATTR_ELEMENT_CORNER);
    attr->flags |= ccl::ATTR_SUBDIVIDE_SMOOTH_FVAR;
    auto *cd = attr->data_for_write<ccl::packed_float3>();
    for (const auto &v : data.corner_colors)
      *cd++ = ccl::packed_float3(ccl::make_float3(v.x, v.y, v.z));
    if (!data.motion_corner_colors.empty()) {
      attr->add_motion(&mesh);
      for (std::size_t step = 0; step < data.motion_corner_colors.size(); ++step) {
        cd = attr->data_for_write<ccl::packed_float3>(int(step + 1));
        for (const auto &v : data.motion_corner_colors[step])
          *cd++ = ccl::packed_float3(ccl::make_float3(v.x, v.y, v.z));
      }
    }
  }
  // Authored control normals shade the original-polygon adaptive preview.
  // Catmull-Clark tessellation derives its normals from the limit surface.
  if (settings.type != SubdivisionType::CatmullClark && !data.corner_normals.empty()) {
    auto *attr = mesh.subd_attributes.add(ccl::ATTR_STD_CORNER_NORMAL);
    auto *n = attr->data_for_write<ccl::packed_normal>();
    for (const auto &v : data.corner_normals)
      *n++ = ccl::packed_normal(ccl::make_float3(v.x, v.y, v.z));
    if (!data.motion_corner_normals.empty()) {
      attr->add_motion(&mesh);
      for (std::size_t step = 0; step < data.motion_corner_normals.size(); ++step) {
        n = attr->data_for_write<ccl::packed_normal>(int(step + 1));
        for (const auto &v : data.motion_corner_normals[step])
          *n++ = ccl::packed_normal(ccl::make_float3(v.x, v.y, v.z));
      }
    }
  }
  mesh.reserve_subd_creases(subd.edge_creases.size());
  for (const auto &crease : subd.edge_creases)
    mesh.add_edge_crease(crease.a, crease.b, crease.sharpness / 10.f);
  // The pinned Mesh::clear does not clear vertex crease arrays.
  ccl::array<int> vertex_indices;
  ccl::array<float> vertex_weights;
  vertex_indices.reserve(subd.vertex_creases.size());
  vertex_weights.reserve(subd.vertex_creases.size());
  for (const auto &crease : subd.vertex_creases) {
    vertex_indices.push_back_reserved(crease.point);
    vertex_weights.push_back_reserved(crease.sharpness / 10.f);
  }
  mesh.set_subd_vert_creases(vertex_indices);
  mesh.set_subd_vert_creases_weight(vertex_weights);
  mesh.tag_subd_start_corner_modified();
  mesh.tag_subd_num_corners_modified();
  mesh.tag_subd_face_corners_modified();
  mesh.tag_subd_shader_modified();
  mesh.tag_subd_smooth_modified();
  mesh.tag_subd_ptex_offset_modified();
  {
    std::lock_guard lock(metrics->mutex);
    metrics->ready = false;
    metrics->vertices = metrics->triangles = 0;
    metrics->bytes = subdivision_engine_bytes(mesh);
  }
  mesh.subdivision_post_tessellate = [metrics, &tessellations, displaced, displacement_fields,
                                      displacement_classes](ccl::Mesh &result) {
    if (displaced)
      subdivision_displacement_seams(result, displacement_fields, displacement_classes);
    std::lock_guard lock(metrics->mutex);
    metrics->ready = true;
    metrics->vertices = result.num_verts();
    metrics->triangles = result.num_triangles();
    metrics->bytes = subdivision_engine_bytes(result);
    ++tessellations;
  };
}
} // namespace cycles_houdini
