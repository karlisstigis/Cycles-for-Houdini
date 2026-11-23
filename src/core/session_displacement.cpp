// SPDX-License-Identifier: Apache-2.0
#include "session_internal.h"

namespace cycles_houdini {
bool RenderSession::Impl::camera_displacement(const MeshRecord &record, bool matrix_changed,
                                              bool projection_changed) const {
  const auto affected = [&](const ShaderGraphRequirements &required) {
    return (matrix_changed && required.displacement_camera_transform_dependent) ||
           (projection_changed && required.displacement_camera_projection_dependent);
  };
  for (const auto &key : record.materials) {
    const auto found = recovered_requirements.find(
        binding_key(key, record.has_uv, record.has_color, false, record.attributes, record.instance_attributes));
    const auto original = material_requirements.find(key);
    const auto *required = found != recovered_requirements.end()     ? &found->second
                           : original != material_requirements.end() ? &original->second
                                                                     : nullptr;
    if (!required || !required->displacement ||
        binding_shader(key, record.has_uv, record.has_color, false, record.attributes, record.instance_attributes)
                ->get_displacement_method() == ccl::DISPLACE_BUMP)
      continue;
    if (affected(*required))
      return true;
    for (std::size_t step = 0; step < material_definitions.at(key).displacement_samples.size(); ++step)
      if (affected(material_requirements.at(temporal_material_key(key, step))))
        return true;
  }
  return false;
}

void RenderSession::Impl::sync_mesh_variants(const std::set<std::string> &changed, bool refresh) {
  if (changed.empty())
    return;
  const bool adaptive_update = !config.interactive || dicing_settings.update || refresh;
  auto *scene = session->scene.get();
  std::set<std::string> leaders;
  for (const auto &key : changed)
    if (auto found = meshes.find(key); found != meshes.end() && found->second.source)
      leaders.insert(found->second.sharing_family ? found->second.sharing_family->leader : key);
  // Index object owners once, rather than scanning the complete scene for
  // each distinct physical family (including private displacement).
  std::unordered_map<std::string, std::vector<ObjectRecord *>> family_users;
  for (auto &[object_key, object] : objects) {
    const auto &logical = meshes.at(object.geometry_key);
    if (!logical.source)
      continue;
    const auto &leader = logical.sharing_family ? logical.sharing_family->leader : object.geometry_key;
    if (leaders.contains(leader))
      family_users[leader].push_back(&object);
  }
  for (const auto &key : leaders) {
    auto found = meshes.find(key);
    if (found == meshes.end() || !found->second.source)
      continue;
    auto &record = found->second;
    const bool pixel = record.source->subdivision && record.source->subdivision->settings.adaptive &&
                       record.source->subdivision->settings.space == SubdivisionSpace::Pixel;
    bool unfinished = false;
    for (const auto &[node, metric] : record.render->subdivision_metrics) {
      std::lock_guard lock(metric->mutex);
      unfinished |=
          !metric->ready || node->position_is_modified() || node->triangles_is_modified() || node->need_tessellation();
    }
    const auto displacement =
        mesh_displacement_state(*record.source, record.sampled_displacement, record.frozen_temporal_materials);
    const bool displaced = displacement.active, transform_dependent = displacement.transform,
               identity_dependent = displacement.identity;
    const bool shader_changed =
        !same_displacement_assignments(*record.source, record.displacement_slots, *record.source, displacement.slots);
    const auto classes = displacement_slot_classes(displacement.slots);
    std::map<std::string, std::vector<ccl::Object *>> groups;
    std::unordered_map<ccl::Object *, ObjectRecord *> owner_records;
    owner_records.reserve(family_users[key].size());
    for (auto *owner : family_users[key]) {
      auto &object = *owner;
      owner_records.emplace(object.node, owner);
      // Keep the transform that produced this object's current
      // variant. Rays and displacement still use its live transform.
      if (pixel) {
        if (adaptive_update || !object.dicing_matrix)
          object.dicing_matrix = object.render_matrix;
      } else
        object.dicing_matrix.reset();
      std::string variant = "shared";
      const auto append = [&](std::uint32_t value) {
        for (int shift = 0; shift < 32; shift += 8)
          variant.push_back(char(value >> shift));
      };
      if (pixel) {
        variant += "/pixel";
        for (float value : *object.dicing_matrix)
          append(std::bit_cast<std::uint32_t>(value));
      }
      if (displaced) {
        const auto &m = object.render_matrix;
        const auto det = m[0] * (m[5] * m[10] - m[6] * m[9]) - m[1] * (m[4] * m[10] - m[6] * m[8]) +
                         m[2] * (m[4] * m[9] - m[5] * m[8]);
        variant += det < 0 ? "/negative" : "/positive";
        if (transform_dependent) {
          for (float value : m)
            append(std::bit_cast<std::uint32_t>(value));
          if (scene->integrator->get_motion_blur() && !object.omit_transform)
            for (const auto &step : object.motion)
              for (float value : step)
                append(std::bit_cast<std::uint32_t>(value));
        }
        if (identity_dependent)
          append(object.random_id);
        if (displacement.object_index)
          append(unsigned(object.properties.pass_index));
        if (record.sampled_displacement) {
          append(unsigned(object.node->get_motion().size()));
          append(unsigned(object.uniform_displacement_steps));
        }
        for (const auto &field : object.attributes)
          if (displacement.fields.named.contains(field.name) && record.instance_attributes.contains(field.name)) {
            variant += "/field:" + std::to_string(field.name.size()) + ":" + field.name;
            append(unsigned(field.components));
            append(std::bit_cast<std::uint32_t>(field.value.x));
            append(std::bit_cast<std::uint32_t>(field.value.y));
            append(std::bit_cast<std::uint32_t>(field.value.z));
            const bool sampled =
                record.sampled_displacement && !object.displacement_attribute_holds.contains(field.name);
            append(sampled ? unsigned(field.motion_values.size()) : 0);
            if (sampled)
              for (const auto value : field.motion_values) {
                append(std::bit_cast<std::uint32_t>(value.x));
                append(std::bit_cast<std::uint32_t>(value.y));
                append(std::bit_cast<std::uint32_t>(value.z));
              }
            append(object.displacement_attribute_holds.contains(field.name));
          }
      }
      groups[variant].push_back(object.node);
    }
    // A disconnected resource must not evaluate displacement with the
    // engine's OBJECT_NONE. Rebind its shader palette when used again.
    if (groups.empty())
      groups.emplace("unbound", std::vector<ccl::Object *>{});
    const auto &render_source = record.sharing_source   ? record.sharing_source
                                : record.nominal_source ? record.nominal_source
                                                        : record.source;
    const auto *shared = record.sharing_source ? &record.sharing_palette : nullptr;
    const bool need_generated = shared ? record.sharing_generated : palette_generated(record.materials);
    const bool need_islands = shared ? record.sharing_islands : palette_islands(record.materials);
    record.shading_dirty |= record.generated != palette_generated(record.materials) ||
                            record.islands != palette_islands(record.materials) ||
                            record.has_pointiness != palette_pointiness(record.materials);
    if (need_islands) {
      if (record.island_random.empty()) {
        record.island_random = mesh_island_random(*record.source);
        mesh_update_profile(generation, "island_topology");
      }
    } else
      std::vector<float>().swap(record.island_random);
    const bool need_pointiness = shared ? record.sharing_pointiness : palette_pointiness(record.materials);
    if (need_pointiness && !render_source->pointiness.empty() &&
        !mesh_payload_equal(record.pointiness, render_source->pointiness)) {
      record.pointiness = render_source->pointiness;
      record.shading_dirty = true;
    }
    if (need_pointiness && record.pointiness.empty()) {
      record.pointiness = render_source->pointiness.empty()
                              ? mesh_pointiness(*render_source)
                              : static_cast<const std::vector<float> &>(render_source->pointiness);
      record.shading_dirty = true;
      mesh_update_profile(generation, "pointiness");
    } else if (!need_pointiness && !record.pointiness.empty()) {
      std::vector<float>().swap(record.pointiness);
      record.shading_dirty = true;
    }
    bool seams_prepared = false;
    const auto prepare_seams = [&]() {
      if (seams_prepared)
        return;
      seams_prepared = true;
      record.seam_source = displacement_seams(render_source, record.seam_vertices, displacement.fields, classes);
      record.seam_source_bytes = record.seam_vertices.size() * sizeof(int);
      if (record.seam_source != render_source) {
        const auto &m = *record.seam_source;
        record.seam_source_bytes += m.positions.size() * sizeof(Float3) + m.triangles.size() * sizeof(Triangle) +
                                    m.corner_normals.size() * sizeof(Float3) + m.corner_uv.size() * sizeof(Float2) +
                                    m.corner_colors.size() * sizeof(Float3) + m.triangle_material.size() * sizeof(int) +
                                    geometry_motion_bytes(m.motion);
        for (const auto &step : m.motion_corner_normals)
          record.seam_source_bytes += step.size() * sizeof(Float3);
        record.seam_source_bytes += shading_motion_bytes(m) + named_attribute_bytes(m.attributes);
      }
    };
    if (!displaced) {
      record.seam_source.reset();
      record.seam_vertices.clear();
      record.seam_source_bytes = 0;
    }
    const bool rebuild = record.dirty || unfinished || shader_changed || displaced != record.displaced;
    auto primary =
        groups.contains(record.render->primary_variant) ? record.render->primary_variant : groups.begin()->first;
    ccl::set<ccl::Geometry *> retired;
    for (auto it = record.render->variants.begin(); it != record.render->variants.end();) {
      if (!groups.contains(it->first) || it->first == primary) {
        retired.insert(it->second);
        record.render->subdivision_metrics.erase(it->second);
        it = record.render->variants.erase(it);
      } else
        ++it;
    }
    for (const auto &[variant, users] : groups) {
      ccl::Mesh *mesh;
      bool fresh = false;
      if (variant == primary) {
        mesh = static_cast<ccl::Mesh *>(record.node);
        fresh = variant != record.render->primary_variant;
      } else {
        auto [it, inserted] = record.render->variants.try_emplace(variant, nullptr);
        fresh = inserted;
        if (inserted) {
          it->second = scene->create_node<ccl::Mesh>();
          it->second->name = ccl::ustring(key + "/render_variant");
        }
        mesh = it->second;
      }
      bool reuse = !fresh && !rebuild && !mesh->position_is_modified() && !mesh->triangles_is_modified() &&
                   !mesh->need_tessellation();
      mesh->validated_displacement_reuse = reuse;
      if (reuse && record.shading_dirty)
        reuse = refresh_mesh_shading(*mesh, *render_source, need_generated, record.seam_vertices, record.island_random,
                                     record.pointiness, !users.empty());
      mesh->validated_displacement_reuse = reuse;
      if (reuse) {
        mesh_update_profile(generation, record.shading_dirty ? "shading_fields_reuse" : "displacement_reuse");
        mesh_palette(mesh, *render_source, !users.empty(), record.sampled_displacement, mesh->get_motion_steps(),
                     record.frozen_temporal_materials, shared);
        if (record.shading_dirty) {
          mesh->tag_used_shaders_modified();
          mesh->tag_update(scene, false);
        }
        if (auto metric = record.render->subdivision_metrics.find(mesh);
            metric != record.render->subdivision_metrics.end()) {
          std::lock_guard lock(metric->second->mutex);
          metric->second->bytes = subdivision_engine_bytes(*mesh);
        }
      } else {
        mesh_update_profile(generation, "mesh_fill", fresh, record.dirty);
        mesh_update_profile(generation, "mesh_displacement", shader_changed, displaced != record.displaced);
        if (displaced && !users.empty())
          prepare_seams();
        const auto &m = displaced && !users.empty() ? *record.seam_source : *render_source;
        auto &metric = record.render->subdivision_metrics[mesh];
        if (!metric)
          metric = std::make_shared<SubdivisionMetrics>();
        {
          std::lock_guard lock(metric->mutex);
          metric->ready = false;
        }
        Matrix matrix = identity_matrix;
        if (pixel && !users.empty()) {
          matrix = *owner_records.at(users.front())->dicing_matrix;
        }
        std::size_t object_motion_steps = m.motion.positions.empty() ? 0 : m.motion.positions.size() + 1;
        for (const auto *object : users)
          object_motion_steps = std::max(object_motion_steps, object->get_motion().size());
        if (record.sampled_displacement && !users.empty()) {
          const auto *owner = owner_records.at(users.front());
          if (owner->uniform_displacement_steps >= 3)
            object_motion_steps = owner->uniform_displacement_steps;
        }
        if (record.sampled_displacement)
          for (const auto &material : m.materials)
            object_motion_steps =
                std::max(object_motion_steps, material_definitions.at(material).displacement_samples.size());
        fill_mesh(mesh, m, !users.empty(), matrix, metric, displaced && !users.empty(), record.sampled_displacement,
                  object_motion_steps, record.frozen_temporal_materials, render_source.get(), record.seam_vertices,
                  record.island_random, record.pointiness, displacement.fields, classes, shared, need_generated,
                  adaptive_update);
        if (displaced || record.displaced)
          ++displacement_rebuilds;
        {
          std::lock_guard lock(metric->mutex);
          metric->bytes = subdivision_engine_bytes(*mesh);
        }
      }
      if (variant == primary)
        record.bytes = subdivision_engine_bytes(*mesh);
      for (auto *object : users) {
        if (object->get_geometry() != mesh) {
          bind_object_geometry(object, mesh, scene);
          object->tag_update(scene);
        }
      }
    }
    // Rebind all users before retiring old private geometry nodes.
    pending_geometry_retirement.insert(retired.begin(), retired.end());
    record.render->primary_variant = primary;
    record.dirty = false;
    record.shading_dirty = false;
    record.displaced = displaced;
    record.displacement_slots = displacement.slots;
    record.render->source = render_source;
    record.generated = palette_generated(record.materials);
    record.islands = palette_islands(record.materials);
    record.has_pointiness = palette_pointiness(record.materials);
    for (const auto &member : record.sharing_family->members)
      if (member != key) {
        auto &logical = meshes.at(member);
        logical.node = record.node;
        logical.render = record.render;
        logical.bytes = record.bytes;
        logical.dirty = logical.shading_dirty = false;
        logical.displaced = record.displaced;
        logical.generated = palette_generated(logical.materials);
        logical.islands = palette_islands(logical.materials);
        logical.has_pointiness = palette_pointiness(logical.materials);
      }
  }
  for (auto &[key, object] : objects) {
    ccl::array<ccl::Node *> palette;
    const auto &record = meshes.at(object.geometry_key);
    if (record.source && record.sharing_source) {
      palette.reserve(record.surface_palette.size());
      for (auto *shader : record.surface_palette)
        palette.push_back_reserved(shader);
    }
    if (object.node->get_shader_palette() != palette) {
      object.node->set_shader_palette(palette);
      object.node->tag_update(scene);
    }
  }
}

void RenderSession::Impl::recover_displacement_motion(std::set<std::string> &changed) {
  auto *scene = session->scene.get();
  const bool blur = scene->integrator->get_motion_blur();
  // The requirements of a material bound with true (not bump) displacement;
  // nullptr when it does not displace. Only such bindings drive recovery.
  const auto true_displacement = [&](const MeshRecord &record,
                                     const std::string &material) -> const ShaderGraphRequirements * {
    const auto found = recovered_requirements.find(
        binding_key(material, record.has_uv, record.has_color, false, record.attributes, record.instance_attributes));
    const auto original = material_requirements.find(material);
    const auto *r = found != recovered_requirements.end()
                        ? &found->second
                        : (original != material_requirements.end() ? &original->second : nullptr);
    if (!r || !r->displacement ||
        binding_shader(material, record.has_uv, record.has_color, false, record.attributes, record.instance_attributes)
                ->get_displacement_method() == ccl::DISPLACE_BUMP)
      return nullptr;
    return r;
  };
  // Nothing to recover: no source mesh binds true displacement and no record
  // holds state from an earlier recovery. Every decision below is then "no
  // recovery", and each record already holds that result: object transforms
  // and motion come from sync_object_pose_values with the same rules, and a
  // blur or vector-pass change rewrites all object motion before this call.
  // Without sampled transforms the motion is checked to be empty; viewport
  // (DLSS) motion history always takes the full path.
  if (!interactive_denoising_motion() && displacement_motion_diagnostics.empty() &&
      temporal_diagnostic_materials.empty()) {
    const bool sampled = sampled_transforms();
    const bool unchanged =
        std::none_of(meshes.begin(), meshes.end(),
                     [&](const auto &item) {
                       const auto &record = item.second;
                       if (!record.source)
                         return false;
                       return record.sampled_displacement || record.omit_deformation || record.nominal_source ||
                              !record.frozen_temporal_materials.empty() ||
                              std::any_of(record.materials.begin(), record.materials.end(),
                                          [&](const auto &material) { return true_displacement(record, material); });
                     }) &&
        std::none_of(objects.begin(), objects.end(), [&](const auto &item) {
          const auto &record = item.second;
          return record.omit_transform || record.uniform_displacement_steps ||
                 !record.displacement_attribute_holds.empty() || (!sampled && !record.node->get_motion().empty());
        });
    if (unchanged) {
      displacement_recovery_idle = true;
      return;
    }
  }
  displacement_recovery_idle = false;
  displacement_motion_diagnostics.clear();
  temporal_diagnostic_materials.clear();
  std::set<std::string> transform_restricted;
  std::map<std::string, std::string> source_names;
  std::set<std::string> moving_objects;
  std::map<std::string, std::vector<ObjectRecord *>> owners;
  for (auto &[key, record] : objects) {
    source_names.try_emplace(record.geometry_key,
                             record.properties.cryptomatte_object.empty() ? key : record.properties.cryptomatte_object);
    if (std::any_of(record.motion.begin(), record.motion.end(),
                    [&](const auto &sample) { return sample != record.matrix; }))
      moving_objects.insert(record.geometry_key);
    owners[record.geometry_key].push_back(&record);
  }
  for (auto &[key, record] : meshes) {
    if (!record.source)
      continue;
    const bool geometry_blur = blur && record.source->motion_blur;
    bool deformation_needed = false, transform_needed = false, motion_capable = true, time_dependent = false,
         temporal = false;
    std::set<std::string> uniform_fields;
    std::size_t graph_steps = 0;
    bool displacement_deforming = record.deforming;
    const auto field_motion = [&](const ShaderGraphRequirements &required) {
      displacement_deforming |= required.displacement_requires_uv &&
                                (!record.source->motion_uv_complete || !record.source->motion_corner_uv.empty());
      displacement_deforming |= required.displacement_requires_color &&
                                (!record.source->motion_color_complete || !record.source->motion_corner_colors.empty());
      for (const auto &name : required.displacement_named_attributes)
        if (const auto *field = named_mesh_field(record.source->attributes, name))
          displacement_deforming |= !field->motion_complete || !field->motion_values.empty();
      for (const auto &name : required.displacement_named_attributes)
        if (record.source->instance_attributes.contains(name))
          uniform_fields.insert(name);
    };
    std::set<std::string> frozen_temporal;
    for (const auto &material : record.materials) {
      const auto *r = true_displacement(record, material);
      if (!r)
        continue;
      field_motion(*r);
      deformation_needed |= r->displacement_deformation_dependent;
      transform_needed |= r->displacement_transform_dependent;
      const auto capable = [&](const auto &required) {
        return required.displacement_motion_supported &&
               std::none_of(required.named_tangent_bases.begin(), required.named_tangent_bases.end(),
                            [&](const auto &basis) { return record.instance_attributes.contains(basis.first); }) &&
               (!required.displacement_requires_uv || record.source->motion_uv_complete) &&
               (!required.displacement_requires_color || record.source->motion_color_complete) &&
               std::all_of(required.displacement_named_attributes.begin(), required.displacement_named_attributes.end(),
                           [&](const auto &name) {
                             if (record.source->instance_attributes.contains(name))
                               return true;
                             const auto &fields = record.source->attributes;
                             const auto found = std::find_if(fields.begin(), fields.end(),
                                                             [&](const auto &f) { return f.name == name; });
                             return found != fields.end() && found->motion_complete;
                           });
      };
      const bool nominal_capable = capable(*r);
      const auto &definition = material_definitions.at(material);
      const bool animated = definition.graph && definition.graph->displacement_time_dependent;
      time_dependent |= animated;
      bool samples_capable = true, sample_deformation = false, sample_transform = false;
      std::string sample_problem, problem_attribute;
      for (std::size_t step = 0; step < definition.displacement_samples.size(); ++step) {
        if (geometry_blur)
          temporal_diagnostic_materials.insert(temporal_material_key(material, step));
        const auto &sample = material_requirements.at(temporal_material_key(material, step));
        field_motion(sample);
        const bool sample_capable = sample.displacement && capable(sample) && sample.source_diagnostics.empty();
        samples_capable &= sample_capable;
        if (!sample_capable && sample_problem.empty()) {
          if (!sample.source_diagnostics.empty())
            sample_problem = sample.source_diagnostics.front().message;
          else
            for (const auto &name : sample.displacement_named_attributes) {
              if (record.source->instance_attributes.contains(name))
                continue;
              const auto &fields = record.source->attributes;
              const auto field =
                  std::find_if(fields.begin(), fields.end(), [&](const auto &value) { return value.name == name; });
              if (field == fields.end() || !field->motion_complete) {
                problem_attribute = name;
                sample_problem = "Attribute '" + name + "' is missing or incomplete";
                break;
              }
            }
          if (sample_problem.empty())
            sample_problem = "Unavailable fields or unsupported displacement evaluation";
          sample_problem += " at shutter sample " + std::to_string(step);
        }
        sample_deformation |= sample.displacement_deformation_dependent;
        sample_transform |= sample.displacement_transform_dependent;
      }
      const bool freeze = !definition.displacement_samples.empty() && nominal_capable && !samples_capable;
      if (freeze) {
        frozen_temporal.insert(material);
        if (geometry_blur)
          displacement_motion_diagnostics.push_back({"motion.displacement_samples",
                                                     source_names.contains(key) ? source_names.at(key) : key, key,
                                                     sample_problem,
                                                     "Evaluating requested-frame displacement values across valid "
                                                     "geometry motion; retaining object and camera motion",
                                                     "warning", problem_attribute});
      } else {
        temporal |= !definition.displacement_samples.empty();
        graph_steps = std::max(graph_steps, definition.displacement_samples.size());
        deformation_needed |= sample_deformation;
        transform_needed |= sample_transform;
      }
      motion_capable &=
          nominal_capable && (freeze || samples_capable) && (!animated || !definition.displacement_samples.empty());
    }
    bool uniform_deforming = false;
    for (auto *owner : owners[key]) {
      std::set<std::string> held;
      std::size_t grid = graph_steps                               ? graph_steps
                         : record.source->motion.positions.empty() ? 1
                                                                   : record.source->motion.positions.size() + 1;
      bool base_grid_complete = true;
      if (!graph_steps && transform_needed && !owner->motion.empty()) {
        const auto merged = instance_motion_grid(grid, owner->motion.size());
        base_grid_complete = merged.has_value();
        if (merged)
          grid = *merged;
      }
      bool sampled_uniform = false;
      if (geometry_blur && motion_capable)
        for (const auto &field : owner->attributes) {
          if (!uniform_fields.contains(field.name) || field.motion_values.empty())
            continue;
          std::string reason;
          const auto count = field.motion_values.size();
          if (!base_grid_complete)
            reason = "Independent geometry and object motion leave no exact displacement grid within 129 steps";
          if (graph_steps && (graph_steps - 1) % (count - 1))
            reason = "Frozen displacement shader samples cannot represent this field's shutter breakpoints";
          const auto next = instance_motion_grid(grid, count);
          if (reason.empty() && !next)
            reason = "The combined displacement shutter grid exceeds the 129-step engine limit";
          if (reason.empty()) {
            grid = *next;
            sampled_uniform = true;
            continue;
          }
          held.insert(field.name);
          displacement_motion_diagnostics.push_back(
              {"motion.instance_attribute_displacement",
               owner->properties.cryptomatte_object.empty() ? owner->node->name.string()
                                                            : owner->properties.cryptomatte_object,
               key, "Attribute '" + field.name + "': " + reason,
               "Using the requested-frame field only for true displacement; retaining temporal surface data and "
               "independent geometry, object and camera motion",
               "warning", field.name});
        }
      const auto steps = sampled_uniform ? grid : std::size_t{};
      if (owner->uniform_displacement_steps != steps || owner->displacement_attribute_holds != held) {
        owner->uniform_displacement_steps = steps;
        owner->displacement_attribute_holds = std::move(held);
        changed.insert(key);
        owner->node->attribute_motion_displacement_hold.clear();
        for (const auto &name : owner->displacement_attribute_holds)
          owner->node->attribute_motion_displacement_hold.push_back(ccl::ustring(engine_attribute_name(name)));
        scene->geometry_manager->tag_update(scene, ccl::GeometryManager::OBJECT_MANAGER);
        scene->object_manager->tag_update(scene, ccl::ObjectManager::OBJECT_MODIFIED);
      }
      uniform_deforming |= sampled_uniform;
    }
    if (record.frozen_temporal_materials != frozen_temporal) {
      record.frozen_temporal_materials = std::move(frozen_temporal);
      record.dirty = true;
      changed.insert(key);
    }
    const bool sampled = geometry_blur && motion_capable &&
                         (temporal || uniform_deforming || (deformation_needed && displacement_deforming) ||
                          (transform_needed && moving_objects.contains(key)));
    if (record.sampled_displacement != sampled) {
      record.sampled_displacement = sampled;
      record.dirty = true;
      changed.insert(key);
    }
    if (geometry_blur && !motion_capable && transform_needed)
      transform_restricted.insert(key);
    const bool omit =
        geometry_blur && !motion_capable && ((deformation_needed && displacement_deforming) || time_dependent);
    if (record.omit_deformation != omit) {
      record.omit_deformation = omit;
      record.dirty = true;
      changed.insert(key);
      record.seam_source.reset();
      record.seam_source_bytes = 0;
    }
    const bool nominal = omit && record.source->nominal_pose.has_value();
    if (nominal && !record.nominal_source) {
      update_nominal_source(record);
      record.seam_source.reset();
      record.seam_source_bytes = 0;
      record.dirty = true;
      changed.insert(key);
    } else if (!nominal && record.nominal_source) {
      record.nominal_source.reset();
      record.nominal_source_bytes = 0;
      record.seam_source.reset();
      record.seam_source_bytes = 0;
      record.dirty = true;
      changed.insert(key);
    }
    if (omit)
      displacement_motion_diagnostics.push_back(
          {"motion.displacement_deformation", source_names.contains(key) ? source_names.at(key) : key, key,
           time_dependent ? (temporal ? "A displacement shutter input is unavailable or needs unsupported sampling"
                                      : "Animated displacement parameters require separately owned shutter graphs")
                          : "This displacement source needs additional per-shutter attribute or basis support",
           "Rendering requested-frame displaced geometry without deformation blur; retaining independent object and "
           "camera motion"});
  }
  for (auto &[key, record] : objects) {
    const bool omit = blur && transform_restricted.contains(record.geometry_key) &&
                      std::any_of(record.motion.begin(), record.motion.end(),
                                  [&](const auto &sample) { return sample != record.matrix; });
    if (omit)
      displacement_motion_diagnostics.push_back(
          {"motion.displacement_transform",
           record.properties.cryptomatte_object.empty() ? key : record.properties.cryptomatte_object, key,
           "Displacement requires evaluation at each object-transform shutter sample",
           "Rendering the displaced object at its requested-frame transform without object motion blur; retaining "
           "independent camera motion"});
    const auto matrix = omit ? record.nominal_transform.value_or(record.matrix) : record.matrix;
    if (record.render_matrix != matrix || record.omit_transform != omit) {
      record.render_matrix = matrix;
      record.omit_transform = omit;
      record.node->set_tfm(transform(matrix));
      record.node->tag_update(scene);
      changed.insert(record.geometry_key);
    }
    auto desired = interactive_denoising_motion()
                       ? denoising_motion(record.node->get_motion(), record.node->get_tfm())
                       : transform_motion(sampled_transforms() && !omit ? record.motion : std::vector<Matrix>{});
    if (!(record.node->get_motion() == desired)) {
      record.node->set_motion(desired);
      record.node->tag_update(scene);
    }
  }
}

void RenderSession::Impl::finish_geometry_sync(const SceneUpdate &u, PreparedUpdate &prepared,
                                               std::set<std::string> &variant_updates, const RenderUpdateFlags &flags,
                                               const std::vector<ccl::Shader *> &retired_binding_shaders,
                                               const std::vector<ccl::Shader *> &retired_material_shaders) {
  auto &i = *this;
  auto *scene = i.session->scene.get();
  const auto previous_vectors = flags.previous_vectors;
  const auto uv_pass_changed = flags.uv_pass_changed;
  const auto rendering_changed = flags.rendering_changed;
  const auto object_edits = prepared.object_edits;
  const auto force_dicing = prepared.force_dicing;
  auto &effective_materials = prepared.effective_materials;
  auto &prepared_bindings = prepared.prepared_bindings;
  auto &profile = *flags.profile;
  // Reconcile motion flags and physical mesh variants after camera/world sync.
  const bool motion_changed = u.motion_blur && *u.motion_blur != scene->integrator->get_motion_blur();
  if (motion_changed) {
    scene->integrator->set_motion_blur(*u.motion_blur);
    scene->integrator->tag_update(scene, ccl::Integrator::UPDATE_ALL);
  }
  // Stored motion and enabled motion are separate. In particular OptiX
  // must not retain a motion primitive/BVH while tracing without motion.
  // Keep owned samples for an inexpensive later re-enable; rebuild only
  // geometry whose actual motion-use flag changes.
  const bool blur_enabled = scene->integrator->get_motion_blur();
  if (motion_changed)
    for (auto &[key, record] : i.meshes) {
      if (record.kind == Impl::GeometryKind::Volume) {
        auto *volume = static_cast<ccl::Volume *>(record.node);
        const float scale = blur_enabled ? record.volume->velocity_seconds : 0;
        if (volume->get_velocity_scale() != scale) {
          volume->set_velocity_scale(scale);
          volume->tag_update(scene, true);
        }
        continue;
      }
      auto *geometry = record.node;
      const auto *positions = geometry->attributes.find(ccl::ATTR_STD_POSITION);
      const bool use_motion =
          blur_enabled && (!record.source || record.source->motion_blur) && positions && positions->has_motion();
      if (geometry->get_use_motion_blur() != use_motion) {
        geometry->set_use_motion_blur(use_motion);
        geometry->tag_update(scene, true);
      }
    }
  if (motion_changed || previous_vectors != i.motion_vectors()) {
    for (auto &[key, record] : i.objects) {
      auto motion = i.interactive_denoising_motion()
                        ? i.denoising_motion(record.node->get_motion(), record.node->get_tfm())
                        : transform_motion(i.sampled_transforms() ? record.motion : std::vector<Matrix>{});
      record.node->set_motion(motion);
      record.node->tag_update(scene);
    }
    auto motion = i.interactive_denoising_motion()
                      ? i.denoising_motion(scene->camera->get_motion(), scene->camera->get_matrix())
                      : transform_motion(i.sampled_transforms() ? i.camera_motion : std::vector<Matrix>{});
    scene->camera->set_motion(motion);
    scene->camera->tag_modified();
    scene->camera->need_device_update = true;
    for (auto &[key, record] : i.meshes)
      if (record.source) {
        record.dirty = true;
        variant_updates.insert(key);
      }
  }
  profile.mark("geometry_camera_flags");
  // Decide from the resolved bindings after shader/geometry updates. Keep
  // immutable shutter samples so a shader repair restores blur immediately.
  // A pose-only edit with blur already disabled updates its transform and
  // vector-pass samples above. It cannot change displacement-motion recovery.
  // Blur/pass transitions and resource/variant changes still take this path.
  // A value-only material edit (sync_material_bindings) cannot change displacement recovery or sharing.
  const bool material_work = (!effective_materials.empty() || !prepared_bindings.empty()) && !i.material_values_only;
  // Only object poses changed, in a scene without displacement-motion state: recovery has nothing to do and
  // mesh families cannot change (they depend on mesh data, materials and recovery flags, not on transforms).
  const bool pose_only = pose_fast_rule() && !u.object_poses.empty() && u.objects.empty() && u.meshes.empty() &&
                         u.points.empty() && u.curves.empty() && u.volumes.empty() && u.deleted_objects.empty() &&
                         u.deleted_geometry.empty() && u.object_attributes.empty() && !material_work &&
                         !motion_changed && previous_vectors == i.motion_vectors() && !uv_pass_changed &&
                         variant_updates.empty() && i.displacement_recovery_idle && !i.interactive_denoising_motion();
  const bool pose_edits = object_edits && blur_enabled && !pose_only;
  if (motion_changed || previous_vectors != i.motion_vectors() || !variant_updates.empty() || pose_edits ||
      !u.deleted_objects.empty() || !u.deleted_geometry.empty() || material_work)
    i.recover_displacement_motion(variant_updates);
  profile.mark("displacement_motion");
  if (uv_pass_changed)
    for (auto &[key, record] : i.meshes)
      if (record.source) {
        record.shading_dirty = true;
        variant_updates.insert(key);
      }
  if (!u.meshes.empty() || !u.objects.empty() || !u.deleted_objects.empty() || !u.deleted_geometry.empty() ||
      material_work || motion_changed || uv_pass_changed || previous_vectors != i.motion_vectors() || pose_edits)
    i.reconcile_mesh_sharing(variant_updates);
  profile.mark("mesh_sharing");
  i.sync_mesh_variants(variant_updates, force_dicing);
  profile.mark("mesh_variants");
  std::unordered_set<const Impl::MeshRenderState *> motion_render_states;
  for (const auto &key : variant_updates) {
    const auto found = i.meshes.find(key);
    if (found == i.meshes.end() || !found->second.source)
      continue;
    auto &record = found->second;
    if (!motion_render_states.insert(record.render.get()).second)
      continue;
    const auto apply = [&](ccl::Geometry *geometry) {
      const bool enabled = blur_enabled && record.source->motion_blur && !record.omit_deformation &&
                           (!record.source->motion.positions.empty() ||
                            (record.sampled_displacement && geometry->get_motion_steps() >= 3));
      if (geometry->get_use_motion_blur() != enabled) {
        geometry->set_use_motion_blur(enabled);
        geometry->tag_update(scene, true);
      }
    };
    apply(record.node);
    for (const auto &[variant, node] : record.render->variants)
      apply(node);
  }

  // Retire obsolete nodes only after every live binding is current.
  i.retire_unused_geometry();
  for (auto *shader : retired_binding_shaders)
    i.retire_shader(shader);
  for (auto *shader : retired_material_shaders)
    i.retire_shader(shader);
  auto deferred = std::move(i.deferred_retired_shaders);
  i.deferred_retired_shaders.clear();
  for (auto *shader : deferred)
    i.retire_shader(shader);
  // The pinned octree density extrema are evaluated at one fixed time.
  // They cannot bound advected fields. Use the upstream ray marcher for
  // scenes with active volume velocity until a motion-aware bound exists.
  if (rendering_changed || motion_changed || !u.volumes.empty() || !u.deleted_geometry.empty() ||
      !effective_materials.empty()) {
    const bool moving_volume = blur_enabled && std::any_of(i.meshes.begin(), i.meshes.end(), [](const auto &item) {
                                 return item.second.volume && item.second.volume->velocity_seconds > 0;
                               });
    // The pinned octree path did not reproduce scattering for the
    // diagnostic PrincipledVolume fixture. Use the verified density
    // ray-marching path while such a fallback is actually bound.
    const bool diagnostic_volume = std::any_of(i.meshes.begin(), i.meshes.end(), [&](const auto &item) {
      const auto &record = item.second;
      if (!record.volume)
        return false;
      const auto &material = i.material_definitions.at(record.volume->material);
      return material.kind == MaterialKind::PrincipledVolume && !material.source_diagnostics.empty();
    });
    const bool biased = moving_volume || diagnostic_volume || !i.volume_recovery.empty() || i.rendering.volume_biased;
    if (scene->integrator->get_volume_ray_marching() != biased) {
      scene->integrator->set_volume_ray_marching(biased);
      scene->integrator->tag_update(scene, ccl::Integrator::UPDATE_ALL);
    }
  }
  profile.mark("geometry_finish");
}

std::set<std::string> RenderSession::Impl::resolve_geometry_variants(const SceneUpdate &u,
                                                                     const PreparedUpdate &prepared,
                                                                     UpdateProfile &profile) {
  auto &i = *this;
  auto *scene = i.session->scene.get();
  const auto recalculate_dicing = prepared.recalculate_dicing;
  const auto hold_dicing = prepared.hold_dicing;
  const auto &pose_records = prepared.pose_records;
  std::set<std::string> variant_updates;
  // Explicit refresh includes object-space meshes and unchanged views.
  if (recalculate_dicing)
    for (auto &[key, record] : i.meshes)
      if (record.source && record.source->subdivision && record.source->subdivision->settings.adaptive) {
        record.dirty = true;
        variant_updates.insert(key);
      }
  std::unordered_set<const Impl::MeshRenderState *> checked_render_states;
  for (auto &[key, record] : i.meshes)
    if (record.render && checked_render_states.insert(record.render.get()).second)
      for (const auto &[node, metric] : record.render->subdivision_metrics) {
        std::lock_guard lock(metric->mutex);
        if (!metric->ready || node->position_is_modified() || node->triangles_is_modified() ||
            node->need_tessellation()) {
          record.dirty = true;
          variant_updates.insert(key);
          break;
        }
      }
  for (const auto &mesh : u.meshes)
    variant_updates.insert(mesh->key);
  for (const auto &object : u.objects) {
    variant_updates.insert(object.geometry_key);
    if (auto old = i.objects.find(object.key); old != i.objects.end())
      variant_updates.insert(old->second.geometry_key);
  }
  struct PoseVariantPolicy {
    bool matrix{}, orientation{};
  };
  profile.mark("transaction_setup");
  std::map<std::string, PoseVariantPolicy> pose_policies;
  // A pose can change a variant only for pixel-diced subdivision (unless dicing is held) or displaced meshes.
  // Without such a mesh the per-pose policy below inserts nothing: skip the walk over every pose.
  // With no ordinary poses, the loop below cannot use this prototype proof.
  const bool pose_policy_needed =
      !pose_fast_rule() ||
      (!u.object_poses.empty() && std::any_of(i.meshes.begin(), i.meshes.end(), [&](const auto &item) {
        const auto &record = item.second;
        return record.source &&
               (record.displaced ||
                (!hold_dicing && record.source->subdivision && record.source->subdivision->settings.adaptive &&
                 record.source->subdivision->settings.space == SubdivisionSpace::Pixel));
      }));
  const auto reversed = [](const Matrix &m) {
    return m[0] * (m[5] * m[10] - m[6] * m[9]) - m[1] * (m[4] * m[10] - m[6] * m[8]) +
               m[2] * (m[4] * m[9] - m[5] * m[8]) <
           0;
  };
  for (std::size_t n = 0; pose_policy_needed && n < u.object_poses.size(); ++n) {
    const auto &object = *pose_records[n];
    auto [policy, inserted] = pose_policies.try_emplace(object.geometry_key);
    if (inserted) {
      const auto &record = i.meshes.at(object.geometry_key);
      if (record.source) {
        policy->second.matrix = !hold_dicing && record.source->subdivision &&
                                record.source->subdivision->settings.adaptive &&
                                record.source->subdivision->settings.space == SubdivisionSpace::Pixel;
        policy->second.orientation = record.displaced;
        // Object-space displacement shares its existing variant
        // through ordinary motion. Pixel dicing, world-dependent
        // displacement and orientation changes still regroup users;
        // sampled displacement remains conservative.
        if (record.displaced)
          for (const auto &material : record.materials) {
            policy->second.matrix |= !i.material_definitions.at(material).displacement_samples.empty();
            const auto recovered = i.recovered_requirements.find(binding_key(
                material, record.has_uv, record.has_color, false, record.attributes, record.instance_attributes));
            const auto required = i.material_requirements.find(material);
            const auto *fields = recovered != i.recovered_requirements.end() ? &recovered->second
                                 : required != i.material_requirements.end() ? &required->second
                                                                             : nullptr;
            policy->second.matrix |= fields && fields->displacement_transform_dependent;
          }
      }
    }
    if (policy->second.matrix ||
        (policy->second.orientation && reversed(object.render_matrix) != reversed(u.object_poses[n].transform)))
      variant_updates.insert(object.geometry_key);
  }
  for (const auto &key : u.deleted_objects)
    if (auto old = i.objects.find(key); old != i.objects.end())
      variant_updates.insert(old->second.geometry_key);
  profile.mark("variant_policy");
  return variant_updates;
}

} // namespace cycles_houdini
