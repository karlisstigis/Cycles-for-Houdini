// SPDX-License-Identifier: Apache-2.0
#include "session_internal.h"
#include "util/tbb.h"
#include <exception>

namespace cycles_houdini {
std::optional<std::vector<RenderSession::Impl::ObjectRecord *>>
RenderSession::Impl::direct_attribute_records(const SceneUpdate &incoming, std::string &fallback_reason) {
  // The accepted model is already validated. Only unsampled values in its
  // exact existing schema may bypass complete object/binding preparation.
  // With steady motion blur, ordinary mesh transforms can retain or replace
  // their shutter samples independently of those values.
  // Every complex case retains the ordinary normalization and validation path.
  const auto note_fallback = [&](const char *reason) {
    if (!fallback_reason.empty())
      return;
    fallback_reason = reason;
    static const bool profile = []() {
      const char *value = std::getenv("CYCLES_HOUDINI_PROFILE_UPDATES");
      return value && std::string_view(value) == "1";
    }();
    if (profile && !incoming.object_attributes.empty())
      std::fprintf(stderr, "CH_ATTRIBUTE_FALLBACK reason=%s\n", reason);
  };
  const auto reject = [&](const char *reason) -> std::optional<std::vector<ObjectRecord *>> {
    note_fallback(reason);
    return {};
  };
#define CH_ATTRIBUTE_GUARD(test, reason)                                                                               \
  if (test)                                                                                                            \
  return reject(reason)
  CH_ATTRIBUTE_GUARD(incoming.object_attributes.empty(), "no_attributes");
  CH_ATTRIBUTE_GUARD(!incoming.materials.empty() || !incoming.deleted_materials.empty(), "material_transaction");
  CH_ATTRIBUTE_GUARD(incoming.world || incoming.background || incoming.linking || incoming.background_lightgroup ||
                         incoming.background_shadow_catcher || incoming.transparent_background ||
                         incoming.transparent_glass || incoming.transparent_roughness_threshold,
                     "world_transaction");
  CH_ATTRIBUTE_GUARD(has_temporal_materials, "temporal_materials");
  CH_ATTRIBUTE_GUARD(color_update_pending, "color_pending");
  const bool sampled = sampled_transforms();
  const char *sampled_reference = std::getenv("CYCLES_HOUDINI_SAMPLED_ATTRIBUTES_REFERENCE");
  CH_ATTRIBUTE_GUARD(sampled && (motion_vectors() || (sampled_reference && std::string_view(sampled_reference) == "1")),
                     "sampled_transforms");
  CH_ATTRIBUTE_GUARD(incoming.passes && *incoming.passes != config.passes, "passes");
  CH_ATTRIBUTE_GUARD(incoming.aovs && *incoming.aovs != config.aovs, "aovs");
  CH_ATTRIBUTE_GUARD(incoming.lightgroups && *incoming.lightgroups != config.lightgroups, "lightgroups");
  CH_ATTRIBUTE_GUARD(incoming.cryptomatte && *incoming.cryptomatte != config.cryptomatte, "cryptomatte");
  CH_ATTRIBUTE_GUARD(incoming.denoising && *incoming.denoising != config.denoising, "denoising_settings");
  CH_ATTRIBUTE_GUARD(incoming.motion_blur && *incoming.motion_blur != session->scene->integrator->get_motion_blur(),
                     "motion_blur");
  CH_ATTRIBUTE_GUARD(incoming.dicing && *incoming.dicing != dicing_settings, "dicing");
  if (incoming.rendering) {
    auto desired = *incoming.rendering;
    if (!desired.animate_seed && !rendering.animate_seed)
      desired.frame = rendering.frame; // An inert frame tag does not change the accepted rendering policy.
    CH_ATTRIBUTE_GUARD(desired != rendering, "rendering_settings");
  }
#undef CH_ATTRIBUTE_GUARD
  std::vector<std::size_t> order;
  order.reserve(incoming.object_attributes.size());
  for (std::size_t index = 0; index < incoming.object_attributes.size(); ++index)
    order.push_back(index);
  const auto less = [&](auto a, auto b) {
    return incoming.object_attributes[a].key < incoming.object_attributes[b].key;
  };
  if (!std::is_sorted(order.begin(), order.end(), less))
    std::sort(order.begin(), order.end(), less);
  // Bounded, opt-in evidence for mixed transactions: identify the actual
  // source replacement instead of inferring it from the fallback label.
  if (const auto *flag = std::getenv("CYCLES_HOUDINI_PROFILE_UPDATES"); flag && std::string_view(flag) == "1") {
    std::fprintf(stderr,
                 "CH_ATTRIBUTE_CANDIDATE fields=%zu meshes=%zu points=%zu curves=%zu volumes=%zu deleted_geometry=%zu "
                 "deleted_objects=%zu\n",
                 incoming.object_attributes.size(), incoming.meshes.size(), incoming.points.size(),
                 incoming.curves.size(), incoming.volumes.size(), incoming.deleted_geometry.size(),
                 incoming.deleted_objects.size());
    const auto trace = [&](const char *kind, const auto &values) {
      std::size_t shown = 0;
      for (const auto &value : values) {
        if (!value || shown++ == 32)
          break;
        const auto accepted = meshes.find(value->key);
        std::fprintf(stderr, "CH_ATTRIBUTE_RESOURCE kind=%s key=%s revision=%llu accepted_revision=%llu\n", kind,
                     value->key.c_str(), static_cast<unsigned long long>(value->revision),
                     static_cast<unsigned long long>(accepted == meshes.end() ? 0 : accepted->second.revision));
      }
    };
    trace("mesh", incoming.meshes);
    trace("point", incoming.points);
    trace("curve", incoming.curves);
    trace("volume", incoming.volumes);
  }
  // Only edits to this record's resource invalidate its accepted layout.
  // Shader preparation below still expands all direct records if repairs can
  // change binding demands. Disjoint geometry and object map edits do not
  // invalidate std::map record addresses; same-key object commands are blocked.
  std::unordered_set<std::string_view> changed_geometry;
  for (const auto &key : incoming.deleted_geometry)
    changed_geometry.insert(key);
  const auto collect_geometry = [&](const auto &values) {
    for (const auto &value : values) {
      if (!value)
        throw std::invalid_argument("Null geometry data");
      changed_geometry.insert(value->key);
    }
  };
  collect_geometry(incoming.meshes);
  collect_geometry(incoming.points);
  collect_geometry(incoming.curves);
  collect_geometry(incoming.volumes);
  // Disjoint object operations and lights leave retained bindings unchanged.
  // Reject command conflicts before borrowing any accepted object owner.
  std::unordered_set<std::string_view> blocked;
  const bool ordered_poses = std::is_sorted(incoming.object_poses.begin(), incoming.object_poses.end(),
                                            [](const auto &a, const auto &b) { return a.key < b.key; });
  // Attributes are visited in key order. Merge the ordinary ordered pose
  // stream instead of allocating and hashing a node for every moving object.
  // Unordered callers retain the complete conflict set; pose uniqueness and
  // validity are still checked by validate before the engine boundary.
  blocked.reserve(incoming.objects.size() + incoming.deleted_objects.size() +
                  (ordered_poses ? 0 : incoming.object_poses.size()));
  for (const auto &value : incoming.objects)
    blocked.insert(value.key);
  if (!ordered_poses)
    for (const auto &value : incoming.object_poses)
      blocked.insert(value.key);
  for (const auto &key : incoming.deleted_objects)
    blocked.insert(key);
  std::vector<ObjectRecord *> records(order.size());
  struct EligibilityOutcome {
    std::size_t position{};
    const char *reason{};
    std::exception_ptr error;
  };
  // The accepted model is immutable under the session control lock. Each chunk
  // owns its prototype-policy cache and output slots; no engine API is called.
  // Defer diagnostics until joining so the earliest sorted fallback is stable.
  const auto scan = [&](std::size_t begin, std::size_t end, bool deferred_fallback, EligibilityOutcome &outcome) {
    std::unordered_map<std::string_view, std::pair<bool, const char *>> eligible_geometry;
    const char *mesh_reason = "mesh_unknown";
    const auto eligible_mesh = [&](const std::string &key) {
      auto [cached, inserted] = eligible_geometry.try_emplace(key, false, "mesh_unknown");
      if (!inserted) {
        mesh_reason = cached->second.second;
        return cached->second.first;
      }
      if (changed_geometry.contains(key)) {
        mesh_reason = cached->second.second = "geometry_transaction";
        return false;
      }
      const auto found = meshes.find(key);
      if (found == meshes.end()) {
        mesh_reason = cached->second.second = "mesh_missing";
        return false;
      }
      const auto &mesh = found->second;
      if (mesh.kind != GeometryKind::Mesh || !mesh.node || !mesh.source || mesh.gaussian_splats || mesh.displaced ||
          mesh.deforming || mesh.omit_deformation || mesh.sampled_displacement || mesh.source->subdivision ||
          !mesh.source->motion.positions.empty() || mesh.source->nominal_pose) {
        mesh_reason = cached->second.second = "mesh_complex";
        return false;
      }
      for (const auto &material_key : mesh.materials) {
        const auto definition = material_definitions.find(material_key);
        if (definition == material_definitions.end()) {
          mesh_reason = cached->second.second = "material_missing";
          return false;
        }
        const auto &material = definition->second;
        if (material.kind == MaterialKind::VolumeAbsorption || material.kind == MaterialKind::PrincipledVolume ||
            !material.displacement_samples.empty() ||
            (material.graph && material.graph->displacement &&
             material.graph->displacement_method != DisplacementMethod::Bump)) {
          mesh_reason = cached->second.second = "material_complex";
          return false;
        }
        if (const auto required = material_requirements.find(material_key); required != material_requirements.end() &&
                                                                            required->second.volume &&
                                                                            !required->second.volume_fields.empty()) {
          // A closed triangle mesh may carry an ordinary volume shader without
          // sparse-grid fields. Match the existing mesh validation/binding rule;
          // grid-domain recovery still requires complete preparation.
          mesh_reason = cached->second.second = "material_volume_grid_requirement";
          return false;
        }
      }
      cached->second.first = true;
      return true;
    };
    auto pose = ordered_poses && deferred_fallback
                    ? std::lower_bound(incoming.object_poses.begin(), incoming.object_poses.end(),
                                       incoming.object_attributes[order[begin]].key,
                                       [](const auto &value, const auto &key) { return value.key < key; })
                    : incoming.object_poses.begin();
    auto accepted = objects.lower_bound(incoming.object_attributes[order[begin]].key);
    for (std::size_t sorted = begin; sorted < end; ++sorted) {
      const auto index = order[sorted];
      const auto &value = incoming.object_attributes[index];
      if (value.key.empty() || (sorted && incoming.object_attributes[order[sorted - 1]].key == value.key))
        throw std::invalid_argument("Invalid or duplicate object attribute key");
      if (ordered_poses)
        while (pose != incoming.object_poses.end() && pose->key < value.key)
          ++pose;
      if ((!blocked.empty() && blocked.contains(value.key)) ||
          (ordered_poses && pose != incoming.object_poses.end() && pose->key == value.key))
        throw std::invalid_argument("Object attributes conflict with an upsert, pose or deletion: " + value.key);
      if (value.pose) {
        if (value.pose->key != value.key)
          throw std::invalid_argument("Attached object pose key differs from attribute key: " + value.key);
        validate_matrix(value.pose->transform);
        if (value.pose->nominal_transform)
          validate_matrix(*value.pose->nominal_transform);
        validate_transform_motion(value.pose->motion, value.pose->transform);
      }
      while (accepted != objects.end() && accepted->first < value.key)
        ++accepted;
      if (accepted == objects.end() || accepted->first != value.key)
        throw std::invalid_argument("Object attributes reference a missing object: " + value.key);
      auto &record = accepted->second;
      const auto eligibility = [&]() -> const char * {
        if (!sampled && value.pose && (!value.pose->motion.empty() || value.pose->nominal_transform))
          return "attribute_pose_motion_or_nominal";
        if (!record.node)
          return "object_node";
        if (record.omit_transform || record.render_matrix != record.matrix)
          return "derived_object_transform";
        if ((!sampled && !record.motion.empty()) || record.uniform_displacement_steps ||
            !record.displacement_attribute_holds.empty())
          return "object_motion_or_displacement";
        if (!eligible_mesh(record.geometry_key))
          return mesh_reason;
        if (record.attributes.size() != value.attributes.size())
          return "attribute_count";
        for (std::size_t field = 0; field < value.attributes.size(); ++field) {
          const auto &before = record.attributes[field], &after = value.attributes[field];
          if (before.name != after.name || before.components != after.components ||
              before.motion_complete != after.motion_complete || !before.motion_values.empty() ||
              !after.motion_values.empty())
            return "attribute_schema_or_motion";
          // Names, uniqueness and component ranges are unchanged from the accepted
          // definition, so finite values are the only new per-field validation.
          if (!std::isfinite(after.value.x) || !std::isfinite(after.value.y) || !std::isfinite(after.value.z))
            throw std::invalid_argument("Invalid owned instance attribute value");
        }
        return nullptr;
      };
      if (const auto *reason = eligibility()) {
        if (deferred_fallback) {
          if (!outcome.reason) {
            outcome.position = sorted;
            outcome.reason = reason;
          }
        } else
          note_fallback(reason);
        continue;
      }
      records[index] = &record;
    }
  };
  const char *reference = std::getenv("CYCLES_HOUDINI_ATTRIBUTE_ELIGIBILITY_REFERENCE");
  const bool parallel = incoming.object_attributes.size() >= 8192 && !(reference && std::string_view(reference) == "1");
  constexpr std::size_t chunks = 64;
  if (const char *profile = std::getenv("CYCLES_HOUDINI_PROFILE_UPDATES"); profile && std::string_view(profile) == "1")
    std::fprintf(stderr, "CH_ATTRIBUTE_ELIGIBILITY mode=%s records=%zu chunks=%zu\n", parallel ? "parallel" : "serial",
                 order.size(), parallel ? chunks : std::size_t(1));
  if (!parallel) {
    EligibilityOutcome outcome;
    scan(0, order.size(), false, outcome);
    return records;
  }
  const std::size_t grain = (order.size() + chunks - 1) / chunks;
  std::vector<EligibilityOutcome> outcomes(chunks);
  tbb::parallel_for(tbb::blocked_range<std::size_t>(0, chunks, 1), [&](const tbb::blocked_range<std::size_t> &range) {
    for (std::size_t chunk = range.begin(); chunk != range.end(); ++chunk) {
      const std::size_t begin = chunk * grain, end = std::min(order.size(), begin + grain);
      if (begin >= end)
        continue;
      try {
        scan(begin, end, true, outcomes[chunk]);
      } catch (...) {
        outcomes[chunk].error = std::current_exception();
      }
    }
  });
  if (std::any_of(outcomes.begin(), outcomes.end(), [](const auto &outcome) { return bool(outcome.error); })) {
    // Rare invalid input: replay the original sorted traversal with fresh local
    // caches/slots. This preserves exception type/message, first error and any
    // fallback that precedes it, without stderr from worker threads.
    std::fill(records.begin(), records.end(), nullptr);
    EligibilityOutcome outcome;
    scan(0, order.size(), false, outcome);
  } else {
    const EligibilityOutcome *first = nullptr;
    for (const auto &outcome : outcomes)
      if (outcome.reason && (!first || outcome.position < first->position))
        first = &outcome;
    if (first)
      note_fallback(first->reason);
  }
  return records;
}

std::optional<SceneUpdate>
RenderSession::Impl::expand_object_attributes(const SceneUpdate &incoming,
                                              const std::vector<ObjectRecord *> *direct_records) const {
  if (incoming.object_attributes.empty() ||
      (direct_records && std::all_of(direct_records->begin(), direct_records->end(), [](auto *r) { return r; })))
    return {};
  // Resolve against accepted logical definitions under session control, never
  // derived render matrices/socket values. No accepted record is changed here.
  SceneUpdate result = incoming;
  std::vector<ObjectAttributeData *> ordered;
  ordered.reserve(result.object_attributes.size());
  for (std::size_t index = 0; index < result.object_attributes.size(); ++index)
    if (!direct_records || !(*direct_records)[index])
      ordered.push_back(&result.object_attributes[index]);
  const auto less = [](const auto *a, const auto *b) { return a->key < b->key; };
  if (!std::is_sorted(ordered.begin(), ordered.end(), less))
    std::sort(ordered.begin(), ordered.end(), less);
  std::unordered_set<std::string_view> blocked;
  blocked.reserve(incoming.objects.size() + incoming.object_poses.size() + incoming.deleted_objects.size());
  for (const auto &value : incoming.objects)
    blocked.insert(value.key);
  for (const auto &value : incoming.object_poses)
    blocked.insert(value.key);
  for (const auto &key : incoming.deleted_objects)
    blocked.insert(key);
  auto accepted = objects.begin();
  result.objects.reserve(result.objects.size() + ordered.size());
  for (std::size_t index = 0; index < ordered.size(); ++index) {
    auto &value = *ordered[index];
    if (value.key.empty() || (index && ordered[index - 1]->key == value.key))
      throw std::invalid_argument("Invalid or duplicate object attribute key");
    if (!blocked.empty() && blocked.contains(value.key))
      throw std::invalid_argument("Object attributes conflict with an upsert, pose or deletion: " + value.key);
    if (value.pose && value.pose->key != value.key)
      throw std::invalid_argument("Attached object pose key differs from attribute key: " + value.key);
    while (accepted != objects.end() && accepted->first < value.key)
      ++accepted;
    if (accepted == objects.end() || accepted->first != value.key)
      throw std::invalid_argument("Object attributes reference a missing object: " + value.key);
    const auto &record = accepted->second;
    ObjectData object;
    object.key = value.key;
    object.geometry_key = record.geometry_key;
    object.transform = value.pose ? value.pose->transform : record.matrix;
    object.visibility = record.visibility;
    object.random_id = record.random_id;
    object.motion = value.pose ? std::move(value.pose->motion) : record.motion;
    object.properties = record.properties;
    object.nominal_transform = value.pose ? value.pose->nominal_transform : record.nominal_transform;
    object.attributes = std::move(value.attributes);
    result.objects.push_back(std::move(object));
  }
  if (direct_records) {
    std::size_t retained = 0;
    for (std::size_t index = 0; index < result.object_attributes.size(); ++index)
      if ((*direct_records)[index]) {
        if (retained != index)
          result.object_attributes[retained] = std::move(result.object_attributes[index]);
        ++retained;
      }
    result.object_attributes.resize(retained);
  } else
    std::vector<ObjectAttributeData>{}.swap(result.object_attributes);
  return result;
}
std::optional<SceneUpdate> RenderSession::Impl::expand_displacement_materials(const SceneUpdate &incoming) const {
  const auto &i = *this;
  std::optional<SceneUpdate> expanded;
  if (!incoming.materials.empty() || !incoming.deleted_materials.empty())
    expanded = incoming;
  if (expanded) {
    auto &prepared = *expanded;
    std::vector<MaterialData> auxiliary_materials;
    // Auxiliary shaders stay private to this session. The host/worker owns one
    // material with flat shutter snapshots, never renderer pointers or keys.
    for (auto &m : prepared.materials) {
      if (temporal_material(m.key))
        throw std::invalid_argument("Reserved displacement shader key");
      const auto count = m.displacement_samples.size();
      if (count && (count < 3 || count > 129 || count % 2 != 1 || !m.graph || !m.graph->displacement ||
                    m.graph->displacement_method == DisplacementMethod::Bump))
        throw std::invalid_argument("Displacement snapshots require a true/both graph and odd 3..129 samples");
      for (std::size_t step = 0; step < count; ++step) {
        auto &graph = m.displacement_samples[step];
        if (!graph.displacement || graph.displacement_method != m.graph->displacement_method)
          throw std::invalid_argument("Displacement shutter graphs must preserve the output method");
        graph = displacement_snapshot(std::move(graph));
        MaterialData sample;
        sample.kind = MaterialKind::Graph;
        sample.key = temporal_material_key(m.key, step);
        sample.graph = graph;
        sample.graph->displacement_time_dependent = false;
        sample.pass_index = m.pass_index;
        sample.cryptomatte_name = m.cryptomatte_name.empty() ? m.key : m.cryptomatte_name;
        auxiliary_materials.push_back(std::move(sample));
      }
    }
    prepared.materials.insert(prepared.materials.end(), std::make_move_iterator(auxiliary_materials.begin()),
                              std::make_move_iterator(auxiliary_materials.end()));
    for (const auto &key : incoming.deleted_materials)
      if (temporal_material(key))
        throw std::invalid_argument("Reserved displacement shader key");
    for (const auto &[key, old] : i.material_definitions)
      if (!old.displacement_samples.empty()) {
        const auto found = std::find_if(incoming.materials.begin(), incoming.materials.end(),
                                        [&](const auto &m) { return m.key == key; });
        const bool deleted = std::find(incoming.deleted_materials.begin(), incoming.deleted_materials.end(), key) !=
                             incoming.deleted_materials.end();
        if (found != incoming.materials.end() || deleted) {
          const auto retained = deleted ? 0 : found->displacement_samples.size();
          for (std::size_t step = retained; step < old.displacement_samples.size(); ++step)
            prepared.deleted_materials.push_back(temporal_material_key(key, step));
        }
      }
  }
  return expanded;
}

RenderSession::Impl::PreparedUpdate RenderSession::Impl::prepare_update(const SceneUpdate &incoming,
                                                                        const SceneUpdate &u, UpdateProfile &profile) {
  auto &i = *this;
  std::map<std::string, Float3> volume_bounds;
  const auto desired_camera = u.camera ? u.camera : i.camera_definition;
  const auto dicing = resolve_dicing(i.dicing_settings, u.dicing, desired_camera, i.dicing_camera_definition,
                                     i.applied_offscreen_scale, i.config.interactive);
  const auto &desired_dicing = dicing.desired;
  const auto &desired_dicing_camera = dicing.camera;
  const bool force_dicing = dicing.force;
  const bool hold_dicing = dicing.hold;
  const bool recalculate_dicing = dicing.recalculate;
  const bool dicing_projection_changed = dicing.projection_changed;
  // Watch rejected candidates as well as accepted files. Repairs trigger an
  // ordinary IPR retry while the last accepted scene keeps rendering.
  const auto iris_path = desired_camera ? desired_camera->image_iris : std::string{};
  const int iris_method = desired_camera ? desired_camera->image_iris_method : 0;
  i.image_iris_watch_path = iris_path;
  i.image_iris_watch_revision = image_iris_revision(iris_path);
  if (iris_method < 0 || iris_method > 2 || (iris_path.empty() != (iris_method == 0)))
    throw std::invalid_argument("Image Iris requires an image and CDF or Hierarchical sampling");
  if (desired_camera && desired_camera->image_iris_color && iris_method == 0)
    throw std::invalid_argument("Use Image Color requires an active Image Iris");
  const auto prepared_iris = prepare_image_iris(iris_path, i.image_iris);
  const bool iris_changed = prepared_iris != i.image_iris;
  // Backend validation happens before cancelling or modifying the current scene.
  const auto denoise_policy = (u.denoising || u.passes || u.motion_blur)
                                  ? i.resolve_session_denoising(u.denoising.value_or(i.config.denoising),
                                                                u.passes ? &*u.passes : nullptr, u.motion_blur)
                                  : i.denoise_policy;
  if (u.rendering)
    validate_render_settings(*u.rendering);
  if (u.passes)
    validate_passes(*u.passes);
  if (u.aovs)
    validate_aovs(*u.aovs);
  const auto &effective_aovs = u.aovs ? *u.aovs : i.config.aovs;
  const auto &effective_groups = u.lightgroups ? *u.lightgroups : i.config.lightgroups;
  validate_pass_outputs(effective_aovs, effective_groups);
  const bool aovs_changed = effective_aovs != i.config.aovs;
  const bool lightgroups_changed = effective_groups != i.config.lightgroups;
  const auto crypto = u.cryptomatte.value_or(i.config.cryptomatte);
  validate_cryptomatte(crypto);
  const bool crypto_changed = crypto != i.config.cryptomatte;
  const auto components = std::uint64_t((u.passes ? u.passes->size() : i.config.passes.size()) + effective_aovs.size() +
                                        effective_groups.size() + cryptomatte_descriptors(crypto).size()) *
                              4 +
                          32;
  if (std::uint64_t(i.config.width) * i.config.height > std::uint64_t(std::numeric_limits<int>::max()) / components)
    throw std::invalid_argument("Pass changes exceed Cycles' safe pass-buffer indexing range");
  const auto has_aov = [](const MaterialData &material) {
    return material.graph && std::any_of(material.graph->nodes.begin(), material.graph->nodes.end(),
                                         [](const auto &node) { return node.definition == "cycles/aov_output/1"; });
  };
  const auto preparation_started = Clock::now();
  // This notification ledger includes failed candidate paths. A file that
  // appears or is repaired can trigger a host retry without committing the
  // failed material or replacing the previous complete image.
  for (const auto &material : u.materials) {
    auto &paths = i.texture_watch_paths[material.key];
    paths.clear();
    i.texture_watch_auto[material.key] = false;
    if (material.graph)
      for (const auto &node : material.graph->nodes)
        if (node.definition == "cycles/image_texture/1" || node.definition == "cycles/environment_texture/1" ||
            node.definition == "cycles/ies_light/1") {
          if (node.definition == "cycles/ies_light/1") {
            const auto text = node.parameters.find("ies");
            if (text != node.parameters.end())
              if (const auto *value = std::get_if<std::string>(&text->second); value && !value->empty())
                continue;
          }
          const auto found = node.parameters.find("filename");
          if (found != node.parameters.end())
            if (const auto *value = std::get_if<std::string>(&found->second))
              paths.push_back(texture_watch_path(*value));
          const auto input = node.parameters.find("colorspace");
          if (node.definition == "cycles/ies_light/1")
            continue;
          if (input == node.parameters.end())
            i.texture_watch_auto[material.key] = true;
          else if (const auto *value = std::get_if<std::string>(&input->second);
                   value && (value->empty() || *value == "auto"))
            i.texture_watch_auto[material.key] = true;
        }
  }
  i.texture_watch_revisions.clear();
  const auto input_policy =
      i.config.texture_input_policy.identity ? i.config.texture_input_policy.identity() : std::string{};
  if (u.world) {
    i.world_watch_paths.clear();
    i.world_watch_auto = false;
    if (u.world->graph)
      for (const auto &node : u.world->graph->nodes)
        if (node.definition == "cycles/image_texture/1" || node.definition == "cycles/environment_texture/1" ||
            node.definition == "cycles/ies_light/1") {
          if (node.definition == "cycles/ies_light/1") {
            const auto text = node.parameters.find("ies");
            if (text != node.parameters.end())
              if (const auto *value = std::get_if<std::string>(&text->second); value && !value->empty())
                continue;
          }
          const auto file = node.parameters.find("filename");
          if (file != node.parameters.end())
            if (const auto *value = std::get_if<std::string>(&file->second))
              i.world_watch_paths.push_back(texture_watch_path(*value));
          const auto input = node.parameters.find("colorspace");
          if (node.definition == "cycles/ies_light/1")
            continue;
          if (input == node.parameters.end())
            i.world_watch_auto = true;
          else if (const auto *value = std::get_if<std::string>(&input->second);
                   value && (value->empty() || *value == "auto"))
            i.world_watch_auto = true;
        }
  }
  i.world_watch_revision =
      texture_watch_revision(i.world_watch_paths, i.world_watch_auto ? input_policy : std::string{});
  for (const auto &[key, paths] : i.texture_watch_paths)
    i.texture_watch_revisions[key] =
        texture_watch_revision(paths, i.texture_watch_auto.at(key) ? input_policy : std::string{});
  for (const auto &light : u.lights) {
    auto &paths = i.light_watch_paths[light.key];
    paths.clear();
    i.light_watch_auto[light.key] = false;
    if (light.graph)
      for (const auto &node : light.graph->nodes) {
        const bool ies = node.definition == "cycles/ies_light/1";
        if (!ies && node.definition != "cycles/image_texture/1" && node.definition != "cycles/environment_texture/1")
          continue;
        if (ies) {
          const auto text = node.parameters.find("ies");
          if (text != node.parameters.end())
            if (const auto *value = std::get_if<std::string>(&text->second); value && !value->empty())
              continue;
        }
        const auto file = node.parameters.find("filename");
        if (file != node.parameters.end())
          if (const auto *value = std::get_if<std::string>(&file->second))
            paths.push_back(texture_watch_path(*value));
        if (!ies) {
          const auto input = node.parameters.find("colorspace");
          if (input == node.parameters.end())
            i.light_watch_auto[light.key] = true;
          else if (const auto *value = std::get_if<std::string>(&input->second);
                   value && (value->empty() || *value == "auto"))
            i.light_watch_auto[light.key] = true;
        }
      }
  }
  i.light_watch_revisions.clear();
  for (const auto &[key, paths] : i.light_watch_paths)
    i.light_watch_revisions[key] =
        texture_watch_revision(paths, i.light_watch_auto.at(key) ? input_policy : std::string{});
  const auto asset_changed = [&](const TextureAsset &asset) {
    return texture_asset_changed(asset, i.config.texture_input_policy);
  };
  const auto resources_changed = [&](const ShaderGraphRequirements &required) {
    return std::any_of(required.textures.begin(), required.textures.end(), asset_changed) ||
           std::any_of(required.ies.begin(), required.ies.end(), ies_asset_changed);
  };
  const auto effective_world = u.world ? *u.world : i.world_definition.value_or(WorldData{});
  const bool world_changed = u.world && i.world_definition != u.world;
  const bool world_graph_changed = u.world && (!i.world_definition || i.world_definition->graph != u.world->graph);
  auto world_requirements = i.world_requirements;
  ccl::unique_ptr<ccl::ShaderGraph> prepared_world;
  if (effective_world.graph &&
      (world_graph_changed || i.color_update_pending ||
       (!world_requirements.source_diagnostics.empty() && u.background) || resources_changed(world_requirements))) {
    const auto recover_world = [&](const std::string &reason) {
      world_requirements = {};
      prepared_world = ccl::make_unique<ccl::ShaderGraph>();
      auto *background = prepared_world->create_node<ccl::BackgroundNode>();
      background->set_color(vec(u.background.value_or(i.background_definition.value_or(Float3{}))));
      background->set_strength(1);
      prepared_world->connect(background->output("Background"), prepared_world->output()->input("Surface"));
      for (const auto &node : effective_world.graph->nodes) {
        if (node.definition != "cycles/image_texture/1" && node.definition != "cycles/environment_texture/1")
          continue;
        const auto found = node.parameters.find("filename");
        if (found == node.parameters.end())
          continue;
        if (const auto *file = std::get_if<std::string>(&found->second)) {
          TextureAsset failure;
          failure.filename = *file;
          failure.image_time = effective_world.graph->image_time.value_or(0.);
          if (file->starts_with("op:") && i.config.texture_input_policy.resolve_image)
            failure.memory_image = i.config.texture_input_policy.resolve_image(*file, failure.image_time);
          failure.failure_revision = texture_watch_revision({*file}, input_policy);
          failure.revision = failure.failure_revision;
          world_requirements.textures.push_back(std::move(failure));
        }
      }
      world_requirements.source_diagnostics.push_back(
          {"world.unavailable", "World shader", "world", reason, "Using the configured constant background"});
    };
    try {
      prepared_world = prepare_shader_graph(*effective_world.graph, world_requirements, i.config.texture_input_policy,
                                            {}, ShaderContext::World);
    } catch (const TextureSourceError &error) {
      recover_world(error.what());
    } catch (const WorldSourceError &error) {
      recover_world(error.what());
    }
  }
  auto effective_lights = u.lights;
  auto light_requirements = i.light_requirements;
  std::map<std::string, ccl::unique_ptr<ccl::ShaderGraph>> prepared_lights;
  for (const auto &[key, record] : i.lights)
    if (record.definition && record.definition->graph &&
        std::none_of(u.lights.begin(), u.lights.end(), [&](const auto &light) { return light.key == key; }) &&
        std::find(u.deleted_lights.begin(), u.deleted_lights.end(), key) == u.deleted_lights.end() &&
        (i.color_update_pending || resources_changed(light_requirements.at(key))))
      effective_lights.push_back(*record.definition);
  for (const auto &light : effective_lights) {
    if (!light.graph) {
      light_requirements.erase(light.key);
      continue;
    }
    const auto old = i.lights.find(light.key);
    if (old != i.lights.end() && old->second.definition && old->second.definition->graph == light.graph &&
        old->second.definition->cone == light.cone && !i.color_update_pending &&
        !resources_changed(light_requirements.at(light.key)))
      continue;
    auto &required = light_requirements[light.key];
    try {
      prepared_lights.emplace(light.key, prepare_shader_graph(*light.graph, required, i.config.texture_input_policy, {},
                                                              ShaderContext::Light));
    } catch (const LightSourceError &error) {
      required = {};
      required.surface = true;
      auto graph = ccl::make_unique<ccl::ShaderGraph>();
      auto *emission = graph->create_node<ccl::EmissionNode>();
      emission->set_color(ccl::make_float3(1));
      emission->set_strength(1);
      graph->connect(emission->output("Emission"), graph->output()->input("Surface"));
      prepared_lights.emplace(light.key, std::move(graph));
      required.source_diagnostics.push_back(
          {"light.shader_unavailable", light.source_path, light.key, error.what(),
           "Rendering the supported analytic light with its color and power; omitted unsupported shader"});
    }
  }
  for (const auto &key : u.deleted_lights)
    light_requirements.erase(key);
  std::map<std::string, ccl::unique_ptr<ccl::ShaderGraph>> prepared_graphs;
  auto requirements = i.material_requirements;
  auto effective_materials = u.materials;
  for (const auto &key : u.deleted_materials)
    requirements.erase(key);
  for (const auto &[key, material] : i.material_definitions)
    if (material.graph &&
        std::none_of(u.materials.begin(), u.materials.end(),
                     [&](const auto &incoming) { return incoming.key == key; }) &&
        std::find(u.deleted_materials.begin(), u.deleted_materials.end(), key) == u.deleted_materials.end()) {
      const auto &old = requirements.at(key);
      if (i.color_update_pending || (aovs_changed && has_aov(material)) || resources_changed(old)) {
        prepared_graphs.emplace(key, prepare_shader_graph(*material.graph, requirements[key],
                                                          i.config.texture_input_policy, effective_aovs));
        effective_materials.push_back(material);
      }
    }
  for (const auto &material : u.materials)
    if (material.kind == MaterialKind::Graph) {
      if (!material.graph)
        throw std::invalid_argument("Graph materials require a graph");
      const auto previous = i.material_definitions.find(material.key);
      if (i.color_update_pending || (aovs_changed && has_aov(material)) || previous == i.material_definitions.end() ||
          !same_material_shading(previous->second, material) || resources_changed(requirements[material.key]))
        prepared_graphs.emplace(material.key, prepare_shader_graph(*material.graph, requirements[material.key],
                                                                   i.config.texture_input_policy, effective_aovs));
    } else {
      requirements.erase(material.key);
      if (material.color_attribute == "Cd" && material.kind != MaterialKind::VolumeAbsorption &&
          material.kind != MaterialKind::PrincipledVolume) {
        auto &needed = requirements[material.key];
        needed.surface = true;
        needed.requires_color = true;
      }
    }
  for (const auto &material : effective_materials)
    if (material.graph)
      requirements.at(material.key).retain_generated = shader_generated_request(*material.graph);
  std::set<BindingKey> bindings_needed;
  // A shader binding can fail on one volume while remaining valid on another.
  // Keep the source definition/identity and all retained grid data intact.
  // Native capture explicitly opts into this; low-level malformed inputs fail.
  Impl::VolumeRecovery volume_recovery;
  std::map<std::string, std::map<std::string, VolumeVectorSources>> volume_vector_sources_needed;
  std::map<std::string, const VolumeData *> effective_volumes;
  for (const auto &[key, record] : i.meshes)
    if (record.volume &&
        std::find(u.deleted_geometry.begin(), u.deleted_geometry.end(), key) == u.deleted_geometry.end())
      effective_volumes[key] = record.volume.get();
  for (const auto &volume : u.volumes) {
    if (!volume)
      throw std::invalid_argument("Null volume data");
    effective_volumes[volume->key] = volume.get();
  }
  for (const auto &[key, volume] : effective_volumes) {
    const auto incoming = std::find_if(effective_materials.begin(), effective_materials.end(),
                                       [&](const auto &m) { return m.key == volume->material; });
    const MaterialData *material =
        incoming == effective_materials.end()
            ? (i.material_definitions.contains(volume->material) ? &i.material_definitions.at(volume->material)
                                                                 : nullptr)
            : &*incoming;
    if (!material)
      continue; // Missing engine resources remain contract failures.
    const auto vector_request = [&](const std::string &name, bool scalar) {
      if (scalar || name.empty() ||
          std::any_of(volume->fields.begin(), volume->fields.end(), [&](const auto &f) { return f.name == name; }))
        return;
      auto sources = volume_vector_sources(*volume, name);
      if (sources.usable())
        volume_vector_sources_needed[key].emplace(name, std::move(sources));
    };
    if (material->kind == MaterialKind::Graph) {
      const auto &needed = requirements.at(material->key);
      for (const auto &name : needed.volume_fields)
        vector_request(name, needed.scalar_volume_fields.contains(name));
    } else if (material->kind == MaterialKind::PrincipledVolume && material->volume_emission_strength > 0)
      vector_request(material->emission_field, false);
    if (!volume->recover_material)
      continue;
    const auto incompatible_domain = [&] {
      volume_recovery[key].push_back({"volume.shader_domain_unavailable",
                                      material->cryptomatte_name.empty() ? material->key : material->cryptomatte_name,
                                      key, "Sparse volume requires a usable volume-only shader",
                                      "Rendering this volume with density-based magenta scattering and no emission"});
    };
    const auto issue = [&](const std::string &name, bool scalar, bool vector) {
      const auto reason = volume_shader_field_error(*volume, name, scalar, vector);
      if (!reason.empty())
        volume_recovery[key].push_back({"volume.shader_field_unavailable",
                                        material->cryptomatte_name.empty() ? material->key : material->cryptomatte_name,
                                        key, reason,
                                        "Rendering this volume with density-based magenta scattering and no emission"});
    };
    if (material->kind == MaterialKind::PrincipledVolume) {
      if (material->blackbody_intensity > 0)
        issue("temperature", true, false);
      if (material->volume_emission_strength > 0 && !material->emission_field.empty())
        issue(material->emission_field, false, false);
    } else if (material->kind == MaterialKind::Graph) {
      const auto &required = requirements.at(material->key);
      if (required.surface || !required.volume || required.volume_diagnostic) {
        incompatible_domain();
      }
      for (const auto &name : required.volume_fields)
        issue(name, required.scalar_volume_fields.contains(name), required.vector_volume_fields.contains(name));
    } else if (material->kind != MaterialKind::VolumeAbsorption)
      incompatible_domain();
    if (volume_recovery.contains(key))
      bindings_needed.insert({volume->material, volume_fallback_binding});
  }
  std::set<std::string> geometry_replaced;
  const auto inspect_binding = [&](const auto &palette, bool has_uv, bool has_color, bool strands = false,
                                   const AttributeLayout &attributes = AttributeLayout{},
                                   const AttributeLayout &instances = AttributeLayout{}) {
    for (const auto &key : palette) {
      const auto found = requirements.find(key);
      const auto incoming = std::find_if(effective_materials.begin(), effective_materials.end(),
                                         [&](const auto &m) { return m.key == key; });
      const auto *material = incoming != effective_materials.end()
                                 ? &*incoming
                                 : (i.material_definitions.contains(key) ? &i.material_definitions.at(key) : nullptr);
      const bool implicit_volume = material && (material->kind == MaterialKind::VolumeAbsorption ||
                                                material->kind == MaterialKind::PrincipledVolume);
      bool needs = implicit_volume;
      if (found != requirements.end()) {
        const auto &r = found->second;
        needs |=
            !r.named_attributes.empty() || (!has_uv && (r.requires_uv || r.displacement_requires_uv)) ||
            (!has_color && r.requires_color) ||
            (strands ? (r.volume || r.requires_mesh_islands || r.requires_mesh_pointiness || r.requires_mesh_tangents ||
                        (r.displacement && material && material->graph &&
                         material->graph->displacement_method != DisplacementMethod::Bump))
                     : (r.volume && !r.volume_fields.empty()));
      }
      if (needs)
        bindings_needed.insert(binding_key(key, has_uv, has_color, strands, attributes, instances));
    }
  };
  for (const auto &m : u.meshes) {
    if (!m)
      throw std::invalid_argument("Null mesh data");
    geometry_replaced.insert(m->key);
    inspect_binding(m->materials, !m->corner_uv.empty(), !m->corner_colors.empty(), false,
                    attribute_layout(m->attributes, m->instance_attributes), m->instance_attributes);
  }
  for (const auto &p : u.points) {
    if (!p)
      throw std::invalid_argument("Null point data");
    geometry_replaced.insert(p->key);
    inspect_binding(strand_materials(*p), !p->uv.empty(), !p->colors.empty(), true,
                    attribute_layout(p->attributes, p->instance_attributes), p->instance_attributes);
  }
  for (const auto &c : u.curves) {
    if (!c)
      throw std::invalid_argument("Null curve data");
    geometry_replaced.insert(c->key);
    inspect_binding(strand_materials(*c), !c->uv.empty(), !c->colors.empty(), true,
                    attribute_layout(c->attributes, c->instance_attributes), c->instance_attributes);
  }
  for (const auto &[key, record] : i.meshes)
    if (record.kind != Impl::GeometryKind::Volume && !geometry_replaced.contains(key) &&
        std::find(u.deleted_geometry.begin(), u.deleted_geometry.end(), key) == u.deleted_geometry.end())
      inspect_binding(record.materials, record.has_uv, record.has_color, record.kind != Impl::GeometryKind::Mesh,
                      record.attributes, record.instance_attributes);
  auto recovered_requirements = i.recovered_requirements;
  std::erase_if(recovered_requirements, [&](const auto &entry) { return !bindings_needed.contains(entry.first); });
  std::map<BindingKey, ccl::unique_ptr<ccl::ShaderGraph>> prepared_bindings;
  for (const auto &key : bindings_needed) {
    const auto incoming = std::find_if(effective_materials.begin(), effective_materials.end(),
                                       [&](const auto &m) { return m.key == key.first; });
    if (!i.binding_materials.contains(key) || incoming != effective_materials.end()) {
      const auto &material = incoming == effective_materials.end() ? i.material_definitions.at(key.first) : *incoming;
      if (key.second == volume_fallback_binding) {
        ShaderGraphData fallback;
        fallback.surface = {};
        fallback.nodes = {{"marker",
                           "cycles/principled_volume/1",
                           {{"color", std::array<float, 3>{1, 0, 1}},
                            {"density", 1.0f},
                            {"density_attribute", std::string("density")},
                            {"color_attribute", std::string{}},
                            {"blackbody_intensity", 0.0f},
                            {"emission_strength", 0.0f}}}};
        fallback.volume = ShaderEndpoint{"marker", "volume"};
        prepared_bindings.emplace(
            key, prepare_shader_graph(fallback, recovered_requirements[key], i.config.texture_input_policy));
        continue;
      }
      if (material.kind == MaterialKind::VolumeAbsorption || material.kind == MaterialKind::PrincipledVolume) {
        prepared_bindings.emplace(key, prepare_shader_graph(diagnostic_surface_graph(), recovered_requirements[key],
                                                            i.config.texture_input_policy));
        recovered_requirements[key].source_diagnostics.push_back(
            {"material.geometry_binding", "", "", "Implicit sparse-grid material cannot shade this geometry",
             "Using diagnostic surface shading; geometry remains visible"});
        continue;
      }
      ShaderGraphData legacy;
      if (!material.graph) {
        // The ROP fallback has the same Cd multiplication and surface
        // model as create_shader, including usable instance bindings.
        // Missing Cd still uses graph branch recovery below.
        ShaderNodeData surface{"surface", "cycles/diffuse_bsdf/1", {{"roughness", material.roughness}}};
        std::string color_input = "color", output = "BSDF";
        if (material.kind == MaterialKind::Emission) {
          surface.definition = "cycles/emission/1";
          surface.parameters = {{"strength", material.emission_strength}};
          output = "emission";
        } else if (material.kind == MaterialKind::Principled) {
          surface.definition = "cycles/principled_bsdf/1";
          surface.parameters.emplace("metallic", material.metallic);
          color_input = "base_color";
        }
        legacy.nodes = {{"color", "cycles/attribute/1", {{"attribute", std::string("Cd")}}},
                        {"tint",
                         "cycles/vector_math/1",
                         {{"math_type", std::string("multiply")},
                          {"vector2", std::array<float, 3>{material.color.x, material.color.y, material.color.z}}}},
                        std::move(surface)};
        legacy.links = {{{"color", "color"}, {"tint", "vector1"}}, {{"tint", "vector"}, {"surface", color_input}}};
        legacy.surface = {"surface", output};
      }
      prepared_bindings.emplace(
          key, prepare_shader_graph(material.graph ? *material.graph : legacy, recovered_requirements[key],
                                    i.config.texture_input_policy, effective_aovs, ShaderContext::Surface,
                                    !(key.second & 1), !(key.second & 2),
                                    (key.second & strand_binding) ? ShaderGeometry::Strands : ShaderGeometry::Mesh,
                                    &key.attributes, &key.instance_attributes));
    }
  }
  std::vector<Impl::ObjectRecord *> pose_records;
  profile.mark("source_and_bindings");
  i.validate(u, volume_bounds, requirements, recovered_requirements, volume_recovery, pose_records);
  profile.mark("validation");
  // Prepare demanded vector aliases from exclusively owned grids while the
  // old render can continue. Never merge inside the engine mutation boundary.
  const auto vectors_started = Clock::now();
  std::map<std::string, Impl::VolumeVectors> prepared_volume_vectors;
  std::uint64_t prepared_vector_merges = 0;
  for (const auto &[key, requests] : volume_vector_sources_needed) {
    if (volume_recovery.contains(key))
      continue;
    const auto previous = i.meshes.find(key);
    auto &vectors = prepared_volume_vectors[key];
    for (const auto &[name, sources] : requests) {
      if (previous != i.meshes.end()) {
        const auto cached = previous->second.volume_vectors.find(name);
        if (cached != previous->second.volume_vectors.end()) {
          bool same = true;
          for (int axis = 0; axis < 3; ++axis)
            same &= cached->second.revisions[axis] && sources.revisions[axis]
                        ? cached->second.revisions[axis] == sources.revisions[axis]
                        : cached->second.sources[axis] == sources.components[axis];
          if (same) {
            auto retained = cached->second;
            // A transported source has fresh headers. Keep only
            // current owners, not old copies of unchanged scalars.
            retained.sources = sources.components;
            retained.revisions = sources.revisions;
            vectors.emplace(name, std::move(retained));
            continue;
          }
        }
      }
      auto grid = merge_volume_vector(sources, name);
      const auto bytes = grid->memUsage();
      vectors.emplace(name, Impl::VolumeVectorRecord{sources.components, std::move(grid), bytes, sources.revisions});
      ++prepared_vector_merges;
    }
  }
  const double volume_vector_seconds = seconds(vectors_started);
  profile.mark("volume_vectors");
  const bool object_edits = i.validate_displacement_motion(incoming, u, pose_records);
  profile.mark("motion_validation");
  const auto binding_handoffs = i.prepare_binding_handoffs(u, bindings_needed, requirements);
  return PreparedUpdate{.desired_camera = std::move(desired_camera),
                        .desired_dicing = std::move(desired_dicing),
                        .force_dicing = force_dicing,
                        .hold_dicing = hold_dicing,
                        .recalculate_dicing = recalculate_dicing,
                        .desired_dicing_camera = std::move(desired_dicing_camera),
                        .dicing_projection_changed = dicing_projection_changed,
                        .prepared_iris = std::move(prepared_iris),
                        .iris_changed = iris_changed,
                        .denoise_policy = std::move(denoise_policy),
                        .aovs_changed = aovs_changed,
                        .lightgroups_changed = lightgroups_changed,
                        .crypto = std::move(crypto),
                        .crypto_changed = crypto_changed,
                        .preparation_started = preparation_started,
                        .object_edits = object_edits,
                        .effective_world = std::move(effective_world),
                        .world_changed = world_changed,
                        .world_graph_changed = world_graph_changed,
                        .world_requirements = std::move(world_requirements),
                        .prepared_world = std::move(prepared_world),
                        .effective_lights = std::move(effective_lights),
                        .light_requirements = std::move(light_requirements),
                        .prepared_lights = std::move(prepared_lights),
                        .prepared_graphs = std::move(prepared_graphs),
                        .requirements = std::move(requirements),
                        .effective_materials = std::move(effective_materials),
                        .bindings_needed = std::move(bindings_needed),
                        .volume_recovery = std::move(volume_recovery),
                        .recovered_requirements = std::move(recovered_requirements),
                        .prepared_bindings = std::move(prepared_bindings),
                        .pose_records = std::move(pose_records),
                        .prepared_volume_vectors = std::move(prepared_volume_vectors),
                        .prepared_vector_merges = prepared_vector_merges,
                        .volume_vector_seconds = volume_vector_seconds,
                        .binding_handoffs = std::move(binding_handoffs),
                        .volume_bounds = std::move(volume_bounds)};
}

} // namespace cycles_houdini
