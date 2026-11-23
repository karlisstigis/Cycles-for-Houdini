// SPDX-License-Identifier: Apache-2.0
#include "session_internal.h"
#include "util/tbb.h"
#include <atomic>
#include <cstdlib>
#include <string_view>

namespace cycles_houdini {
void RenderSession::Impl::update_mesh(const MeshData &m) {
  auto &record = meshes[m.key];
  if (record.node && record.kind != GeometryKind::Mesh)
    throw std::invalid_argument("Use a distinct resource key when changing geometry type");
  if (record.node && !record.force_source_update && record.revision == m.revision && record.materials == m.materials)
    return;
  record.force_source_update = false;
  mesh_update_profile(generation, "source_mesh", bool(record.node), record.materials == m.materials);
  if (!record.node) {
    record.node = session->scene->create_node<ccl::Mesh>();
    record.node->name = ccl::ustring(m.key);
    record.render = std::make_shared<MeshRenderState>();
  }
  if (record.source && !record.island_random.empty() && !same_island_topology(*record.source, m))
    std::vector<float>().swap(record.island_random);
  if (record.source && !record.pointiness.empty()) {
    const auto &old = record.source->nominal_pose ? record.source->nominal_pose->positions : record.source->positions;
    const auto &next = m.nominal_pose ? m.nominal_pose->positions : m.positions;
    if (!mesh_payload_equal(old, next) || !same_island_topology(*record.source, m) ||
        !mesh_payload_equal(record.source->pointiness, m.pointiness) ||
        !mesh_payload_equal(record.source->blender_vertex_normals, m.blender_vertex_normals))
      std::vector<float>().swap(record.pointiness);
  }
  const bool payload_changed = !record.source || !same_mesh_payload(*record.source, m);
  const auto displacement = mesh_displacement_state(m, record.sampled_displacement, record.frozen_temporal_materials);
  const bool retained =
      record.source && same_mesh_geometry(*record.source, m) &&
      same_displacement_assignments(*record.source, record.displacement_slots, m, displacement.slots) &&
      same_displacement_fields(*record.source, m, displacement.fields);
  record.dirty |= !retained;
  record.shading_dirty |= payload_changed;
  // Stamps and face indices describe one source version. A real palette
  // expansion can force a rebuild, but must not leave old stamps paired
  // with the incoming source before sync_mesh_variants examines it.
  // record.dirty retains the decision to rebuild the physical mesh.
  record.displacement_slots = displacement.slots;
  record.source = std::make_shared<MeshData>(m);
  record.seam_source.reset();
  // Keep canonical correspondence for Generated/Pointiness on retained
  // displaced triangles. Rebuild the seam source only on a real fill.
  record.seam_source_bytes = record.seam_vertices.size() * sizeof(int);
  if (retained && record.nominal_source && m.nominal_pose)
    update_nominal_source(record);
  else {
    record.nominal_source.reset();
    record.nominal_source_bytes = 0;
  }
  record.deforming = std::any_of(m.motion.positions.begin(), m.motion.positions.end(),
                                 [&](const auto &sample) { return sample != m.positions; });
  record.deforming |= std::any_of(m.motion_corner_normals.begin(), m.motion_corner_normals.end(),
                                  [&](const auto &sample) { return sample != m.corner_normals; });
  record.revision = m.revision;
  record.materials = m.materials;
  record.attributes = attribute_layout(m.attributes, m.instance_attributes);
  record.instance_attributes = m.instance_attributes;
  record.has_uv = !m.corner_uv.empty();
  record.has_color = !m.corner_colors.empty();
  record.source_bytes = m.positions.size() * sizeof(Float3) + m.triangles.size() * sizeof(Triangle) +
                        m.pointiness.size() * sizeof(float) + m.corner_normals.size() * sizeof(Float3) +
                        m.corner_uv.size() * sizeof(Float2) + m.corner_colors.size() * sizeof(Float3) +
                        m.triangle_material.size() * sizeof(int) + geometry_motion_bytes(m.motion);
  if (m.subdivision)
    record.source_bytes += subdivision_source_bytes(*m.subdivision);
  if (m.nominal_pose) {
    const auto &p = *m.nominal_pose;
    record.source_bytes += named_attribute_bytes(p.attributes);
    record.source_bytes += (p.positions.size() + p.corner_normals.size() + p.corner_colors.size()) * sizeof(Float3) +
                           p.corner_uv.size() * sizeof(Float2);
  }
  for (const auto &sample : m.motion_corner_normals)
    record.source_bytes += sample.size() * sizeof(Float3);
  record.source_bytes += shading_motion_bytes(m) + named_attribute_bytes(m.attributes);
  ++geometry_conversions;
}

void RenderSession::Impl::update_nominal_source(MeshRecord &record) {
  auto data = std::make_shared<MeshData>(*record.source);
  const auto &pose = *data->nominal_pose;
  data->positions = pose.positions;
  data->corner_normals = pose.corner_normals;
  data->attributes = pose.attributes;
  data->corner_uv = pose.corner_uv;
  data->corner_colors = pose.corner_colors;
  data->nominal_pose.reset();
  data->motion = {};
  data->motion_corner_normals.clear();
  data->motion_corner_uv.clear();
  data->motion_corner_colors.clear();
  record.nominal_source_bytes =
      (data->positions.size() + data->corner_normals.size() + data->corner_colors.size()) * sizeof(Float3) +
      data->triangles.size() * sizeof(Triangle) + data->corner_uv.size() * sizeof(Float2) +
      data->triangle_material.size() * sizeof(int) + named_attribute_bytes(data->attributes) +
      data->pointiness.size() * sizeof(float);
  if (data->subdivision)
    record.nominal_source_bytes += subdivision_source_bytes(*data->subdivision);
  record.nominal_source = std::move(data);
}

MeshSharingFieldOwners RenderSession::Impl::field_owners(const MeshRecord &record) {
  MeshSharingFieldOwners result;
  const auto &source = record.nominal_source ? *record.nominal_source : *record.source;
  for (const auto &field : source.attributes)
    result.fields.insert(field.name);
  for (const auto &[name, components] : record.instance_attributes)
    if (std::none_of(record.source->attributes.begin(), record.source->attributes.end(),
                     [&](const auto &own) { return own.name == name; }))
      result.uniform_only.insert(name);
  result.corner_color = !source.corner_colors.empty();
  result.uniform_color_only = record.instance_attributes.contains("Cd") && !record.has_color;
  return result;
}

void RenderSession::Impl::reconcile_mesh_sharing(std::set<std::string> &changed) {
  struct Physical {
    ccl::Geometry *node{};
    std::shared_ptr<MeshRenderState> render;
    std::size_t bytes{};
    bool displaced{};
  };
  struct Family {
    std::vector<std::string> members;
    std::unordered_set<std::string_view> member_keys;
    std::shared_ptr<const MeshData> source;
    std::shared_ptr<MeshData> merged;
    MeshSharingFieldOwners owners;
    bool eligible{}, generated{}, islands{}, pointiness{}, omit_deformation{}, retained{};
    bool same_physical_source(const MeshData &previous) const {
      // Shared surface bodies need one common face-slot stream.
      // A private family has one logical owner: update_mesh already
      // compared its per-face displacement functions and used fields.
      // Its dirty flag keeps that decision; a surface-only slot edit
      // refreshes shader indices without discarding displaced points.
      return eligible ? same_mesh_shape(previous, *source) : same_mesh_geometry(previous, *source);
    }
  };
  std::map<ccl::Geometry *, Physical> physical;
  for (const auto &[key, record] : meshes)
    if (record.source) {
      if (!record.render)
        throw std::logic_error("Owned mesh has no physical render state");
      if (physical.contains(record.node))
        continue;
      physical.emplace(record.node, Physical{record.node, record.render, record.bytes, record.displaced});
      pending_geometry_retirement.insert(record.node);
      for (const auto &[variant, node] : record.render->variants)
        pending_geometry_retirement.insert(node);
    }
  std::vector<Family> families;
  std::map<std::uint64_t, std::vector<std::size_t>> buckets;
  const bool uv_pass = std::find(config.passes.begin(), config.passes.end(), "uv") != config.passes.end();
  for (auto &[key, record] : meshes)
    if (record.source) {
      record.surface_palette = resolved_mesh_palette(record);
      const bool eligible = surface_mesh_sharing_allowed(record);
      const auto owners = field_owners(record);
      auto source = record.nominal_source ? record.nominal_source : record.source;
      if (record.hashed_source != source) {
        record.shape_hash = mesh_shape_hash(*source);
        record.hashed_source = source;
      }
      // Automatic and authored Pointiness are both actual field values.
      // An authored field must not silently replace a sibling's automatic one.
      if (eligible && palette_pointiness(record.materials) && source->pointiness.empty()) {
        auto values = std::make_shared<MeshData>(*source);
        values->pointiness = mesh_pointiness(*source);
        source = std::move(values);
      }
      std::optional<std::size_t> match;
      if (eligible)
        for (const auto index : buckets[record.shape_hash]) {
          auto &family = families[index];
          if ((uv_pass && family.source->corner_uv.empty() != source->corner_uv.empty()) ||
              family.omit_deformation != record.omit_deformation || !same_mesh_shape(*family.source, *source) ||
              !compatible_mesh_shading_fields(*family.source, *source) || !family.owners.compatible(owners))
            continue;
          match = index;
          break;
        }
      if (!match) {
        match = families.size();
        families.emplace_back();
        auto &family = families.back();
        family.source = source;
        family.eligible = eligible;
        family.omit_deformation = record.omit_deformation;
        if (eligible)
          buckets[record.shape_hash].push_back(*match);
      }
      auto &family = families[*match];
      if (adds_mesh_shading_fields(*family.source, *source)) {
        if (!family.merged)
          family.merged = std::make_shared<MeshData>(*family.source);
        merge_mesh_shading_fields(*family.merged, *source);
        family.source = family.merged;
      }
      family.members.push_back(key);
      family.member_keys.insert(key);
      family.owners.include(owners);
      const auto &old = physical.at(record.node);
      if (!family.retained && old.render->source)
        family.retained = family.same_physical_source(*old.render->source);
      family.generated |= palette_generated(record.materials);
      family.islands |= palette_islands(record.materials);
      family.pointiness |= palette_pointiness(record.materials);
    }
  // Keep an unchanged family on its current mesh before assigning a
  // former sibling that now needs different geometry. No node can be
  // claimed by two incompatible families during a split.
  std::stable_sort(families.begin(), families.end(), [&](const auto &a, const auto &b) {
    if (a.retained != b.retained)
      return a.retained > b.retained;
    return a.members.size() > b.members.size();
  });
  std::set<ccl::Geometry *> claimed;
  for (auto &family : families) {
    std::string leader = family.members.front();
    for (const auto &member : family.members) {
      const auto &previous = meshes.at(member).sharing_family;
      if (previous && family.member_keys.contains(previous->leader)) {
        leader = previous->leader;
        break;
      }
    }
    Physical chosen;
    for (int pass = 0; pass < 2 && !chosen.node; ++pass)
      for (const auto &member : family.members) {
        const auto &old = physical.at(meshes.at(member).node);
        if (claimed.contains(old.node))
          continue;
        if (pass == 0 && (!old.render->source || !family.same_physical_source(*old.render->source)))
          continue;
        chosen = old;
        break;
      }
    if (!chosen.node) {
      chosen.node = session->scene->create_node<ccl::Mesh>();
      chosen.node->name = ccl::ustring(leader);
      chosen.render = std::make_shared<MeshRenderState>();
    }
    claimed.insert(chosen.node);
    auto &record = meshes.at(leader);
    const auto old_source = record.sharing_source ? record.sharing_source : record.render->source;
    const bool shared = family.eligible && family.members.size() > 1;
    std::vector<ccl::Shader *> shaders;
    if (shared) {
      shaders = record.surface_palette; // Preserve duplicate source slots in this prefix.
      std::unordered_set<ccl::Shader *> seen(shaders.begin(), shaders.end());
      for (const auto &member : family.members)
        for (auto *shader : meshes.at(member).surface_palette)
          if (seen.insert(shader).second)
            shaders.push_back(shader);
    }
    const bool state_changed = !record.sharing_family || record.sharing_family->members != family.members ||
                               record.sharing_palette != shaders || record.sharing_generated != family.generated ||
                               record.sharing_islands != family.islands ||
                               record.sharing_pointiness != family.pointiness;
    bool dirty = !chosen.render->source || !family.same_physical_source(*chosen.render->source);
    bool shading = state_changed || !old_source || !same_mesh_wire_payload(*old_source, *family.source);
    for (const auto &member : family.members) {
      dirty |= meshes.at(member).dirty;
      shading |= meshes.at(member).shading_dirty;
    }
    record.node = chosen.node;
    record.render = chosen.render;
    record.bytes = chosen.bytes;
    record.displaced = chosen.displaced;
    record.dirty = dirty;
    record.shading_dirty = shading;
    record.sharing_source = shared ? family.source : nullptr;
    record.sharing_palette = std::move(shaders);
    record.sharing_generated = family.generated;
    record.sharing_islands = family.islands;
    record.sharing_pointiness = family.pointiness;
    std::shared_ptr<const MeshFamilyMembers> membership;
    if (record.sharing_family && record.sharing_family->leader == leader &&
        record.sharing_family->members == family.members)
      membership = record.sharing_family;
    else {
      auto next = std::make_shared<MeshFamilyMembers>();
      next->leader = leader;
      next->members = std::move(family.members);
      membership = std::move(next);
    }
    for (const auto &member : membership->members) {
      auto &logical = meshes.at(member);
      logical.sharing_family = membership;
      logical.render = chosen.render;
      logical.node = chosen.node;
      if (member != leader) {
        logical.sharing_source = record.sharing_source;
        logical.sharing_palette.clear();
      }
      changed.insert(member);
    }
  }
}

void RenderSession::Impl::retire_unused_geometry() {
  if (pending_geometry_retirement.empty())
    return;
  ccl::set<ccl::Geometry *> live;
  std::unordered_set<const MeshRenderState *> visited;
  for (const auto &[key, record] : meshes) {
    live.insert(record.node);
    if (record.render && visited.insert(record.render.get()).second)
      for (const auto &[variant, node] : record.render->variants)
        live.insert(node);
  }
  for (const auto &[key, object] : objects)
    live.insert(object.node->get_geometry());
  for (auto it = pending_geometry_retirement.begin(); it != pending_geometry_retirement.end();)
    if (live.contains(*it))
      it = pending_geometry_retirement.erase(it);
    else
      ++it;
  for (auto *geometry : pending_geometry_retirement)
    mesh_refit_streak.erase(geometry);
  if (!pending_geometry_retirement.empty())
    session->scene->delete_nodes(pending_geometry_retirement);
  pending_geometry_retirement.clear();
}

void RenderSession::Impl::mesh_palette(ccl::Mesh *mesh, const MeshData &source, bool bound, bool sampled,
                                       std::size_t steps, const std::set<std::string> &frozen,
                                       const std::vector<ccl::Shader *> *shared) {
  ccl::array<ccl::Node *> shaders;
  if (shared && bound) {
    shaders.reserve(shared->size());
    for (auto *shader : *shared) {
      shader->tag_used(session->scene.get());
      shaders.push_back_reserved(shader);
    }
  } else if (source.materials.empty() || !bound) {
    shaders.reserve(1);
    shaders.push_back_reserved(session->scene->default_surface);
  } else {
    shaders.reserve(source.materials.size());
    const auto layout = attribute_layout(source.attributes, source.instance_attributes);
    for (const auto &material : source.materials) {
      auto *shader = binding_shader(material, !source.corner_uv.empty(), !source.corner_colors.empty(), false, layout,
                                    source.instance_attributes);
      shader->tag_used(session->scene.get());
      shaders.push_back_reserved(shader);
    }
  }
  ccl::array<int> temporal;
  if (bound && sampled && steps >= 3) {
    const bool animated = std::any_of(source.materials.begin(), source.materials.end(), [&](const auto &key) {
      return !frozen.contains(key) && !material_definitions.at(key).displacement_samples.empty();
    });
    if (animated) {
      temporal.reserve(steps * source.materials.size());
      shaders.reserve(shaders.size() + steps * source.materials.size());
      for (std::size_t step = 0; step < steps; ++step)
        for (std::size_t slot = 0; slot < source.materials.size(); ++slot) {
          const auto &key = source.materials[slot];
          const auto &samples = material_definitions.at(key).displacement_samples;
          auto *shader = samples.empty() || frozen.contains(key) ? static_cast<ccl::Shader *>(shaders[slot])
                                                                 : materials.at(temporal_material_key(key, step));
          shader->tag_used(session->scene.get());
          const auto found = std::find(shaders.begin(), shaders.end(), shader);
          if (found == shaders.end()) {
            temporal.push_back_reserved(int(shaders.size()));
            shaders.push_back_reserved(shader);
          } else
            temporal.push_back_reserved(int(found - shaders.begin()));
        }
    }
  }
  if (mesh->get_used_shaders() != shaders) {
    mesh->set_used_shaders(shaders);
    mesh->tag_shader_modified();
    mesh->tag_update(session->scene.get(), false);
  }
  mesh->set_motion_displacement_shader_slots(temporal);
}

void RenderSession::Impl::generated_attribute(ccl::AttributeSet &attributes, const MeshData &source,
                                              std::span<const int> vertices, std::size_t count) {
  const auto &positions = source.nominal_pose ? source.nominal_pose->positions : source.positions;
  if ((vertices.empty() ? positions.size() : vertices.size()) != count)
    throw std::logic_error("Generated coordinate ownership does not match render vertices");
  const GeneratedCoordinates coordinates(positions, source.texture_space);
  auto *target = attributes.add(ccl::ATTR_STD_GENERATED)->data_for_write<ccl::packed_float3>();
  if (vertices.empty())
    for (const auto p : positions)
      *target++ = ccl::packed_float3(vec(coordinates(p)));
  else
    for (const auto point : vertices)
      *target++ = ccl::packed_float3(vec(coordinates(positions.at(std::size_t(point)))));
}

void RenderSession::Impl::pointiness_attribute(ccl::AttributeSet &attributes, std::span<const float> values,
                                               std::span<const int> vertices, std::size_t count) {
  if (values.empty())
    return;
  if ((vertices.empty() ? values.size() : vertices.size()) != count)
    throw std::logic_error("Pointiness ownership does not match render vertices");
  auto *target = attributes.add(ccl::ATTR_STD_POINTINESS)->data_for_write<float>();
  if (vertices.empty())
    std::copy(values.begin(), values.end(), target);
  else
    for (int point : vertices) {
      if (point < 0 || std::size_t(point) >= values.size())
        throw std::logic_error("Pointiness render seam index outside source");
      *target++ = values[point];
    }
}

namespace {
// Element-wise copies into engine arrays on all cores. Each element is
// converted on its own (no reduction), so the result is the serial one.
// CYCLES_HOUDINI_SERIAL_CONVERSION=1 keeps them on the calling thread.
template <class F> void mesh_fill_ranges(std::size_t count, const F &f) {
  constexpr std::size_t grain = 65536;
  const auto *serial = std::getenv("CYCLES_HOUDINI_SERIAL_CONVERSION");
  if (count <= grain || (serial && std::string_view(serial) == "1"))
    f(std::size_t(0), count);
  else
    tbb::parallel_for(tbb::blocked_range<std::size_t>(0, count, grain),
                      [&](const tbb::blocked_range<std::size_t> &range) { f(range.begin(), range.end()); });
}
} // namespace

void RenderSession::Impl::fill_mesh(ccl::Mesh *mesh, const MeshData &source, bool bound, const Matrix &matrix,
                                    const std::shared_ptr<SubdivisionMetrics> &metrics, bool displaced,
                                    bool sampled_displacement, std::size_t object_motion_steps,
                                    const std::set<std::string> &frozen_temporal, const MeshData *generated_source,
                                    std::span<const int> generated_vertices, std::span<const float> islands,
                                    std::span<const float> pointiness, const DisplacementFields &displacement_fields,
                                    const std::vector<int> &displacement_classes,
                                    const std::vector<ccl::Shader *> *shared, bool shared_generated,
                                    bool adaptive_update) {
  if (source.subdivision && source.subdivision->settings.adaptive && !adaptive_update) {
    std::vector<int> faces, vertices;
    auto coarse = std::make_shared<MeshData>(coarse_subdivision_mesh(source, faces));
    std::vector<float> coarse_islands;
    if (!islands.empty()) {
      coarse_islands.reserve(faces.size());
      for (int face : faces)
        coarse_islands.push_back(islands[face]);
    }
    const auto render =
        displaced ? displacement_seams(coarse, vertices, displacement_fields, displacement_classes) : coarse;
    fill_mesh(mesh, *render, bound, matrix, metrics, displaced, sampled_displacement, object_motion_steps,
              frozen_temporal, generated_source ? generated_source : &source, vertices, coarse_islands, pointiness,
              displacement_fields, displacement_classes, shared, shared_generated);
    return;
  }
  std::optional<MeshData> repeated;
  if (sampled_displacement)
    for (const auto &key : source.materials)
      object_motion_steps = std::max(object_motion_steps, material_definitions.at(key).displacement_samples.size());
  if (sampled_displacement && object_motion_steps >= 3 && source.motion.positions.size() + 1 != object_motion_steps)
    repeated = instance_resample_mesh(source, object_motion_steps);
  const auto &m = repeated ? *repeated : source;
  // Refit Meshes While Interacting: Cycles refits the structure of a mesh tagged without a rebuild.
  // Allowed only for a plain mesh whose vertex count and triangles are unchanged (and, below, whose
  // motion steps are unchanged), at most mesh_refit_limit times in a row. The triangle comparison
  // runs on all cores; a mismatch anywhere is the same answer in any schedule.
  bool refit_topology = false;
  if (mesh_refit_enabled() && !m.subdivision && !displaced && mesh->get_num_subd_faces() == 0 &&
      mesh->num_verts() == m.positions.size() && mesh->num_triangles() == m.triangles.size() &&
      mesh_refit_streak[mesh] < mesh_refit_limit) {
    const int *old = mesh->get_triangles().data();
    const auto *next = m.triangles.data();
    std::atomic<bool> differs{false};
    mesh_fill_ranges(m.triangles.size(), [&, old, next](std::size_t begin, std::size_t end) {
      for (auto i = begin; i < end && !differs.load(std::memory_order_relaxed); ++i)
        if (old[3 * i] != next[i].a || old[3 * i + 1] != next[i].b || old[3 * i + 2] != next[i].c) {
          differs = true;
          break;
        }
    });
    refit_topology = !differs.load();
  }
  const auto old_motion_steps = mesh->get_motion_steps();
  const bool old_motion_blur = mesh->get_use_motion_blur();
  // Geometry::clear(false) drops its raw shader array without socket
  // dereferences. Keep the palette until mesh_palette replaces it.
  mesh->clear(true);
  mesh->retain_shading_basis = displaced;
  mesh->subdivision_shading_budget = m.subdivision ? subdivision_shading_budget : nullptr;
  if (metrics)
    mesh->geometry_update_completed = [metrics](ccl::Mesh &result) {
      std::lock_guard lock(metrics->mutex);
      metrics->ready = true;
      metrics->vertices = result.num_verts();
      metrics->triangles = result.num_triangles();
      metrics->bytes = subdivision_engine_bytes(result);
    };
  mesh->set_motion_displacement(sampled_displacement);
  mesh->subdivision_post_tessellate = {};
  mesh->subdivision_triangle_limit = 0;
  mesh->subdivision_fixed = false;
  ccl::array<int> no_creases;
  ccl::array<float> no_weights;
  mesh->set_subd_vert_creases(no_creases);
  mesh->set_subd_vert_creases_weight(no_weights);
  mesh->resize_mesh(static_cast<int>(m.positions.size()), static_cast<int>(m.triangles.size()));
  {
    auto *p = mesh->get_position_for_write();
    const auto *points = m.positions.data();
    mesh_fill_ranges(m.positions.size(), [=](std::size_t begin, std::size_t end) {
      for (auto i = begin; i < end; ++i)
        p[i] = ccl::packed_float3(vec(points[i]));
    });
    auto *triangles = mesh->get_triangles().data();
    auto *smooth = mesh->get_smooth().data();
    auto *shader = mesh->get_shader().data();
    const auto *source_triangles = m.triangles.data();
    const bool smoothed = m.smooth || !m.corner_normals.empty();
    const int *slots = m.triangle_material.data();
    const std::size_t slot_count = bound ? m.triangle_material.size() : 0;
    mesh_fill_ranges(m.triangles.size(), [=](std::size_t begin, std::size_t end) {
      for (auto i = begin; i < end; ++i) {
        const auto &t = source_triangles[i];
        triangles[3 * i] = t.a;
        triangles[3 * i + 1] = t.b;
        triangles[3 * i + 2] = t.c;
        smooth[i] = smoothed;
        shader[i] = i < slot_count ? slots[i] : 0;
      }
    });
  }
  mesh_palette(mesh, m, bound, sampled_displacement, object_motion_steps, frozen_temporal, shared);
  const bool generated = shared ? shared_generated : palette_generated(m.materials);
  if (m.subdivision) {
    mesh->set_motion_steps(m.motion.positions.empty() ? 0 : unsigned(m.motion.positions.size() + 1));
    mesh->set_use_motion_blur(session->scene->integrator->get_motion_blur() && m.motion_blur &&
                              !m.motion.positions.empty());
    fill_subdivision(*mesh, m, bound, transform(matrix), metrics, subdivision_tessellations, displaced,
                     displacement_fields, displacement_classes);
    if (bound && !islands.empty()) {
      if (islands.size() != m.subdivision->face_sizes.size())
        throw std::logic_error("Island control-face ownership mismatch");
      std::copy(islands.begin(), islands.end(),
                mesh->subd_attributes.add(ccl::ATTR_STD_RANDOM_PER_ISLAND)->data_for_write<float>());
    }
    if (bound && generated)
      generated_attribute(mesh->subd_attributes, generated_source ? *generated_source : source, generated_vertices,
                          m.positions.size());
    if (bound)
      pointiness_attribute(mesh->subd_attributes, pointiness, generated_vertices, m.positions.size());
    mesh->tag_position_modified();
    mesh->tag_update(session->scene.get(), true);
    return;
  }
  update_geometry_motion(mesh, m.motion, session->scene->integrator->get_motion_blur() && m.motion_blur);
  if (bound && !islands.empty()) {
    if (islands.size() != m.triangles.size())
      throw std::logic_error("Island triangle ownership mismatch");
    std::copy(islands.begin(), islands.end(),
              mesh->attributes.add(ccl::ATTR_STD_RANDOM_PER_ISLAND)->data_for_write<float>());
  }
  if (bound && generated)
    generated_attribute(mesh->attributes, generated_source ? *generated_source : source, generated_vertices,
                        m.positions.size());
  if (bound)
    pointiness_attribute(mesh->attributes, pointiness, generated_vertices, m.positions.size());
  fill_named_attributes(mesh->attributes, m.attributes, ccl::ATTR_ELEMENT_CORNER, false, mesh);
  if (!m.corner_normals.empty()) {
    auto *n = mesh->attributes.add(ccl::ATTR_STD_CORNER_NORMAL)->data_for_write<ccl::packed_normal>();
    const auto *normals = m.corner_normals.data();
    mesh_fill_ranges(m.corner_normals.size(), [=](std::size_t begin, std::size_t end) {
      for (auto i = begin; i < end; ++i)
        n[i] = ccl::packed_normal(vec(normals[i]));
    });
  }
  if (!m.corner_uv.empty()) {
    auto *attr = mesh->attributes.add(ccl::ATTR_STD_UV);
    auto *uv = attr->data_for_write<ccl::float2>();
    const auto *values = m.corner_uv.data();
    mesh_fill_ranges(m.corner_uv.size(), [=](std::size_t begin, std::size_t end) {
      for (auto i = begin; i < end; ++i)
        uv[i] = ccl::make_float2(values[i].x, values[i].y);
    });
    if (!m.motion_corner_uv.empty()) {
      attr->add_motion(mesh);
      for (std::size_t step = 0; step < m.motion_corner_uv.size(); ++step) {
        uv = attr->data_for_write<ccl::float2>(int(step + 1));
        for (const auto &value : m.motion_corner_uv[step])
          *uv++ = ccl::make_float2(value.x, value.y);
      }
    }
  }
  if (!m.corner_colors.empty()) {
    auto *attr = mesh->attributes.add(ccl::ustring("Cd"), ccl::TypeColor, ccl::ATTR_ELEMENT_CORNER);
    auto *color = attr->data_for_write<ccl::packed_float3>();
    const auto *values = m.corner_colors.data();
    mesh_fill_ranges(m.corner_colors.size(), [=](std::size_t begin, std::size_t end) {
      for (auto i = begin; i < end; ++i)
        color[i] = ccl::packed_float3(vec(values[i]));
    });
    if (!m.motion_corner_colors.empty()) {
      attr->add_motion(mesh);
      for (std::size_t step = 0; step < m.motion_corner_colors.size(); ++step) {
        color = attr->data_for_write<ccl::packed_float3>(int(step + 1));
        for (const auto &value : m.motion_corner_colors[step])
          *color++ = ccl::packed_float3(vec(value));
      }
    }
  }
  if (!m.motion_corner_normals.empty()) {
    auto *normals = mesh->attributes.find(ccl::ATTR_STD_CORNER_NORMAL);
    normals->add_motion(mesh);
    for (std::size_t step = 0; step < m.motion_corner_normals.size(); ++step) {
      auto *target = normals->data_for_write<ccl::packed_normal>(int(step + 1));
      for (const auto &value : m.motion_corner_normals[step])
        *target++ = ccl::packed_normal(vec(value));
    }
  }
  mesh->tag_position_modified();
  mesh->tag_triangles_modified();
  mesh->tag_shader_modified();
  mesh->tag_smooth_modified();
  const bool refit =
      refit_topology && mesh->get_motion_steps() == old_motion_steps && mesh->get_use_motion_blur() == old_motion_blur;
  if (refit) {
    ++mesh_refit_streak[mesh];
    ++mesh_refits;
  } else
    mesh_refit_streak.erase(mesh);
  mesh->tag_update(session->scene.get(), !refit);
}

std::shared_ptr<const MeshData> RenderSession::Impl::displacement_seams(const std::shared_ptr<const MeshData> &source,
                                                                        std::vector<int> &generated_vertices,
                                                                        const DisplacementFields &used,
                                                                        const std::vector<int> &classes) {
  generated_vertices.clear();
  const auto &m = *source;
  if (m.subdivision)
    return source; // Generated seams are prepared after tessellation.
  if (m.triangle_material.empty() && m.corner_normals.empty() && m.corner_uv.empty() && m.corner_colors.empty() &&
      m.attributes.empty())
    return source;
  std::map<std::vector<std::uint32_t>, int> vertices;
  std::vector<int> remap;
  std::vector<Triangle> triangles;
  triangles.reserve(m.triangles.size());
  for (std::size_t face = 0; face < m.triangles.size(); ++face) {
    const auto &t = m.triangles[face];
    Triangle result;
    const std::array<int, 3> points = {t.a, t.b, t.c};
    std::array<int, 3> indices;
    for (int corner = 0; corner < 3; ++corner) {
      const auto index = face * 3 + corner;
      const int slot = m.triangle_material.empty() ? 0 : m.triangle_material[face];
      if (!classes.empty() && (slot < 0 || std::size_t(slot) >= classes.size()))
        throw std::logic_error("Displacement slot has no owned class");
      std::vector<std::uint32_t> key = {std::uint32_t(points[corner]),
                                        std::uint32_t(classes.empty() ? slot : classes[std::size_t(slot)])};
      const auto value = [&](float component) { key.push_back(std::bit_cast<std::uint32_t>(component)); };
      for (const auto *attribute : {&m.corner_normals, used.color ? &m.corner_colors : nullptr})
        if (attribute && !attribute->empty()) {
          const auto &vector = (*attribute)[index];
          value(vector.x);
          value(vector.y);
          value(vector.z);
        }
      for (const auto &attr : m.attributes)
        if (used.named.contains(attr.name)) {
          const auto v = attr.values[index];
          value(v.x);
          value(v.y);
          value(v.z);
          for (const auto &sample : attr.motion_values) {
            const auto v = sample[index];
            value(v.x);
            value(v.y);
            value(v.z);
          }
        }
      if (used.uv && !m.corner_uv.empty()) {
        value(m.corner_uv[index].x);
        value(m.corner_uv[index].y);
      }
      if (used.uv)
        for (const auto &sample : m.motion_corner_uv) {
          value(sample[index].x);
          value(sample[index].y);
        }
      if (used.color)
        for (const auto &sample : m.motion_corner_colors) {
          const auto &color = sample[index];
          value(color.x);
          value(color.y);
          value(color.z);
        }
      const auto [found, inserted] = vertices.try_emplace(std::move(key), int(remap.size()));
      if (inserted)
        remap.push_back(points[corner]);
      indices[corner] = found->second;
    }
    result = {indices[0], indices[1], indices[2]};
    triangles.push_back(result);
  }
  // Preserve unused points as well. Source topology and stable host
  // identities never depend on which faces have a displacement shader.
  std::vector<bool> referenced(m.positions.size(), false);
  for (int point : remap)
    referenced[point] = true;
  for (std::size_t point = 0; point < referenced.size(); ++point)
    if (!referenced[point])
      remap.push_back(int(point));
  if (remap.size() == m.positions.size())
    return source;
  auto data = std::make_shared<MeshData>(m);
  data->triangles = std::move(triangles);
  data->pointiness.clear();   // Canonical intrinsic values use the explicit render seam map.
  data->nominal_pose.reset(); // Recovery metadata belongs to the canonical source, not render seams.
  data->positions.clear();
  for (int point : remap)
    data->positions.push_back(m.positions[point]);
  for (std::size_t step = 0; step < m.motion.positions.size(); ++step) {
    data->motion.positions[step].clear();
    for (int point : remap)
      data->motion.positions[step].push_back(m.motion.positions[step][point]);
  }
  // Splitting material/UV seams must not change smooth source normals.
  // Keep the original per-corner normal field before separating vertices.
  if (m.smooth && m.corner_normals.empty()) {
    const auto normals = [&](const std::vector<Float3> &positions) {
      std::vector<ccl::float3> sums(positions.size(), ccl::zero_float3());
      for (const auto &t : m.triangles) {
        auto n = ccl::cross(vec(positions[t.b]) - vec(positions[t.a]), vec(positions[t.c]) - vec(positions[t.a]));
        n = ccl::is_zero(n) ? ccl::make_float3(1, 0, 0) : ccl::safe_normalize(n);
        sums[t.a] += n;
        sums[t.b] += n;
        sums[t.c] += n;
      }
      std::vector<Float3> result;
      result.reserve(m.triangles.size() * 3);
      for (const auto &t : m.triangles)
        for (int point : {t.a, t.b, t.c}) {
          const auto n = ccl::safe_normalize(sums[point]);
          result.push_back({n.x, n.y, n.z});
        }
      return result;
    };
    data->corner_normals = normals(m.positions);
    for (const auto &step : m.motion.positions)
      data->motion_corner_normals.push_back(normals(step));
  }
  generated_vertices = std::move(remap);
  return data;
}

std::vector<ccl::Shader *>
RenderSession::Impl::stage_resource_retirement(const SceneUpdate &u,
                                               const std::set<ccl::Shader *> &retained_handoff_shaders) {
  auto &i = *this;
  auto *scene = i.session->scene.get();
  // Defer geometry/shader retirement until all owners and variants rebind.
  std::unordered_set<const Impl::MeshRenderState *> deleted_render_states;
  for (const auto &k : u.deleted_geometry)
    if (auto it = i.meshes.find(k); it != i.meshes.end()) {
      i.pending_geometry_retirement.insert(it->second.node);
      if (it->second.render && deleted_render_states.insert(it->second.render.get()).second)
        for (const auto &[variant, node] : it->second.render->variants)
          i.pending_geometry_retirement.insert(node);
      i.meshes.erase(it);
    }
  std::vector<ccl::Shader *> retired_material_shaders;
  for (const auto &k : u.deleted_materials)
    if (auto it = i.materials.find(k); it != i.materials.end()) {
      auto *shader = it->second;
      i.materials.erase(it);
      i.material_definitions.erase(k);
      if (retained_handoff_shaders.contains(shader))
        continue;
      // Retained mesh variants still hold their old palette until
      // sync_mesh_variants below. Clearing the reference count here
      // makes its later socket dereference negative; reusing that
      // shader can then reach zero and skip SVM compilation.
      retired_material_shaders.push_back(shader);
    }
  if (!u.materials.empty() || !u.deleted_materials.empty())
    i.has_temporal_materials =
        std::any_of(i.material_definitions.begin(), i.material_definitions.end(),
                    [](const auto &value) { return !value.second.displacement_samples.empty(); });
  return retired_material_shaders;
}

void RenderSession::Impl::sync_geometry(const SceneUpdate &u, PreparedUpdate &prepared) {
  auto &i = *this;
  auto *scene = i.session->scene.get();
  auto &volume_bounds = prepared.volume_bounds;
  auto &prepared_volume_vectors = prepared.prepared_volume_vectors;
  for (const auto &mesh : u.meshes)
    i.update_mesh(*mesh);
  for (const auto &volume : u.volumes)
    i.update_volume(volume, volume_bounds.at(volume->key), std::move(prepared_volume_vectors[volume->key]));
  for (const auto &points : u.points)
    i.update_points(*points);
  for (const auto &curves : u.curves)
    i.update_curves(*curves);
  for (const auto &o : u.objects) {
    auto &record = i.objects[o.key];
    auto *&object = record.node;
    if (!object) {
      object = scene->create_node<ccl::Object>();
      object->name = ccl::ustring(o.key);
    }
    const auto name = ccl::ustring(o.properties.cryptomatte_object.empty() ? o.key : o.properties.cryptomatte_object);
    if (object->name != name) {
      object->name = name;
      object->tag_asset_name_modified();
    }
    object->set_asset_name(
        ccl::ustring(o.properties.cryptomatte_asset.empty() ? name.string() : o.properties.cryptomatte_asset));
    bind_object_geometry(object, i.meshes.at(o.geometry_key).node, scene);
    i.sync_object_pose_values(record, o.transform, o.motion, o.nominal_transform);
    record.geometry_key = o.geometry_key;
    record.random_id = o.random_id;
    record.visibility = o.visibility;
    record.properties = o.properties;
    i.sync_attribute_values(record, o.attributes);
    object->set_use_holdout(o.properties.holdout);
    object->set_pass_id(o.properties.pass_index);
    object->set_color(vec(o.properties.color));
    object->set_alpha(o.properties.alpha);
    object->set_is_shadow_catcher(o.properties.shadow_catcher && !object->get_geometry()->is_volume());
    object->set_lightgroup(ccl::ustring(o.properties.lightgroup));
    object->set_is_caustics_caster(o.properties.caustics_caster);
    object->set_is_caustics_receiver(o.properties.caustics_receiver);
    object->set_shadow_terminator_shading_offset(o.properties.shadow_terminator_shading);
    object->set_shadow_terminator_geometry_offset(o.properties.shadow_terminator_geometry);
    object->set_ao_distance(o.properties.ao_distance);
    object->set_visibility(o.visibility);
    object->set_random_id(o.random_id);
    object->tag_update(scene);
    ++i.object_updates;
    i.apply_linking(object, o.properties.linking_id.empty() ? o.key : o.properties.linking_id);
  }
}

void RenderSession::Impl::sync_attribute_values(ObjectRecord &record, const std::vector<UniformAttribute> &attributes) {
  auto *scene = session->scene.get();
  auto *object = record.node;
  if (record.attributes != attributes) {
    record.attributes = attributes;
    object_attribute_bytes -= record.attribute_bytes;
    record.attribute_bytes = 0;
    for (const auto &field : record.attributes)
      record.attribute_bytes +=
          sizeof(UniformAttribute) + field.name.size() + field.motion_values.size() * sizeof(Float3);
    object_attribute_bytes += record.attribute_bytes;
    fill_uniform_attributes(*object, attributes);
    // Object user data is not a geometry socket. Refresh the attribute map
    // without marking prototype positions/topology, including emissive users.
    scene->geometry_manager->tag_update(scene, ccl::GeometryManager::OBJECT_MANAGER);
    scene->object_manager->tag_update(scene, ccl::ObjectManager::OBJECT_MODIFIED);
  }
  // Nested dielectrics (engine patch 0074): an instance field overrides the
  // retained OBJ property; removing the field restores that property.
  int priority = record.properties.dielectric_priority;
  for (const auto &field : attributes)
    if (field.name == dielectric_priority_attribute) {
      const double value =
          std::clamp(double(field.value.x), -double(dielectric_priority_limit), double(dielectric_priority_limit));
      priority = int(std::lround(value));
      break;
    }
  object->set_dielectric_priority(priority);
}

void RenderSession::Impl::sync_object_attributes(const SceneUpdate &u,
                                                 const std::vector<ObjectRecord *> &attribute_records) {
  auto *scene = session->scene.get();
  const bool viewport_motion = interactive_denoising_motion();
  for (std::size_t index = 0; index < u.object_attributes.size(); ++index) {
    auto &record = *attribute_records[index];
    const auto &update = u.object_attributes[index];
    // A complete attached pose advances viewport motion through the same
    // helper as full/pose sync. Field-only edits also refresh its current
    // sample; Cycles advances the previous sample between evaluations.
    if (update.pose)
      sync_object_pose_values(record, update.pose->transform, update.pose->motion, update.pose->nominal_transform);
    else if (viewport_motion) {
      auto motion = denoising_motion(record.node->get_motion(), record.node->get_tfm());
      record.node->set_motion(motion);
    }
    sync_attribute_values(record, update.attributes);
    record.node->tag_update(scene);
    ++object_updates;
  }
}

void RenderSession::Impl::delete_objects(const SceneUpdate &u) {
  auto &i = *this;
  auto *scene = i.session->scene.get();
  ccl::set<ccl::Object *> retired;
  for (const auto &k : u.deleted_objects)
    if (auto it = i.objects.find(k); it != i.objects.end()) {
      retired.insert(it->second.node);
      i.object_attribute_bytes -= it->second.attribute_bytes;
      i.objects.erase(it);
    }
  // Cycles scans and compacts its full object array for each delete_nodes
  // call. Remove one transaction's instances together, including when an
  // animated source replaces thousands of old instance identities at once.
  if (!retired.empty())
    scene->delete_nodes(retired);
}

} // namespace cycles_houdini
