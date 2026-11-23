// SPDX-License-Identifier: Apache-2.0
#include "session_internal.h"
#include <cstdlib>
#include <cstring>

namespace cycles_houdini {
namespace {
// Per-material engine settings. Every path that (re)uses a shader slot sets
// all of them, so a reused slot never keeps a previous owner's values.
// suppress_emission keeps the camera-only diagnostic marker out of light
// sampling whatever the artist chose for the material.
void apply_material_settings(ccl::Shader *shader, const MaterialSettings &settings, bool suppress_emission) {
  static constexpr ccl::EmissionSampling emission[] = {ccl::EMISSION_SAMPLING_NONE, ccl::EMISSION_SAMPLING_AUTO,
                                                       ccl::EMISSION_SAMPLING_FRONT, ccl::EMISSION_SAMPLING_BACK,
                                                       ccl::EMISSION_SAMPLING_FRONT_BACK};
  static constexpr ccl::VolumeSampling volume[] = {ccl::VOLUME_SAMPLING_DISTANCE, ccl::VOLUME_SAMPLING_EQUIANGULAR,
                                                   ccl::VOLUME_SAMPLING_MULTIPLE_IMPORTANCE};
  shader->set_emission_sampling_method(suppress_emission ? ccl::EMISSION_SAMPLING_NONE
                                                         : emission[int(settings.emission_sampling)]);
  shader->set_use_transparent_shadow(settings.transparent_shadows);
  shader->set_use_bump_map_correction(settings.bump_map_correction);
  shader->set_volume_sampling_method(volume[int(settings.volume_sampling)]);
  shader->set_volume_interpolation_method(settings.volume_interpolation == VolumeInterpolation::Cubic
                                              ? ccl::VOLUME_INTERPOLATION_CUBIC
                                              : ccl::VOLUME_INTERPOLATION_LINEAR);
  shader->set_volume_step_rate(settings.volume_step_rate);
}
} // namespace
void RenderSession::Impl::update_material(const MaterialData &m, ccl::unique_ptr<ccl::ShaderGraph> prepared,
                                          bool camera_only_emission) {
  if (auto previous = material_definitions.find(m.key);
      previous != material_definitions.end() && previous->second == m && !prepared)
    return;
  auto *&shader = materials[m.key];
  if (shader && !prepared) {
    const auto previous = material_definitions.find(m.key);
    if (previous != material_definitions.end() && same_material_shading(previous->second, m)) {
      shader->name = ccl::ustring(m.cryptomatte_name.empty() ? m.key : m.cryptomatte_name);
      shader->set_pass_id(m.pass_index);
      session->scene->shader_manager->tag_update(session->scene.get(), ccl::ShaderManager::UPDATE_ALL);
      material_definitions[m.key] = m;
      return;
    }
  }
  if (!shader) {
    if (retired_shaders.empty())
      shader = session->scene->create_node<ccl::Shader>();
    else {
      shader = retired_shaders.back();
      retired_shaders.pop_back();
      ++shader_reuses;
    }
  }
  shader->name = ccl::ustring(m.cryptomatte_name.empty() ? m.key : m.cryptomatte_name);
  shader->set_pass_id(m.pass_index);
  // This marker emits only to camera rays. The engine's conservative
  // graph estimate cannot infer that restriction and otherwise builds
  // a mesh-light tree for every affected triangle. Restore AUTO when
  // the same assignment is repaired or a shader slot changes ownership.
  // Recovered graphs whose whole surface became the marker use NONE too.
  // Other materials use their own settings (graph materials) or the
  // engine defaults (built-in kinds), Emission Sampling Auto included.
  apply_material_settings(shader, m.kind == MaterialKind::Graph && m.graph ? m.graph->settings : MaterialSettings{},
                          m.kind == MaterialKind::DiagnosticSurface || camera_only_emission);
  if (m.kind == MaterialKind::Graph) {
    if (!prepared)
      throw std::logic_error("Surface graph was not prepared before live mutation");
    const auto method = m.graph->displacement_method;
    const auto engine_method = method == DisplacementMethod::True   ? ccl::DISPLACE_TRUE
                               : method == DisplacementMethod::Both ? ccl::DISPLACE_BOTH
                                                                    : ccl::DISPLACE_BUMP;
    const bool method_changed = shader->get_displacement_method() != engine_method;
    const auto old_displacement = shader->graph ? shader->graph->displacement_hash : std::string{};
    const bool pending_displacement = shader->need_update_displacement;
    shader->set_displacement_method(engine_method);
    shader->set_graph(std::move(prepared));
    const auto old = material_definitions.find(m.key);
    const bool displacement_changed =
        method_changed || shader->need_update_displacement ||
        (old != material_definitions.end() && old->second.displacement_samples != m.displacement_samples);
    if (method != DisplacementMethod::Bump) {
      mesh_update_profile(generation, "source_displacement", method_changed, displacement_changed);
      mesh_update_profile(generation, "source_hash", old_displacement != shader->graph->displacement_hash,
                          pending_displacement);
    }
    // tag_update marks every shader socket modified, conservatively
    // treating a surface-only edit as a displacement-method edit.
    // Preserve the engine's pre-optimization displacement hash result.
    shader->tag_update(session->scene.get());
    shader->need_update_displacement = displacement_changed;
    material_definitions[m.key] = m;
    ++material_builds;
    return;
  }
  auto graph = ccl::make_unique<ccl::ShaderGraph>();
  shader->set_displacement_method(ccl::DISPLACE_BUMP);
  ccl::ShaderOutput *out{};
  ccl::ShaderInput *surface_color{};
  const bool volume = m.kind == MaterialKind::VolumeAbsorption || m.kind == MaterialKind::PrincipledVolume;
  if (m.kind == MaterialKind::VolumeAbsorption) {
    auto *n = graph->create_node<ccl::AbsorptionVolumeNode>();
    n->set_color(vec(m.color));
    auto *attribute = graph->create_node<ccl::AttributeNode>();
    attribute->set_attribute(ccl::ustring("density"));
    auto *scale = graph->create_node<ccl::MathNode>();
    scale->set_math_type(ccl::NODE_MATH_MULTIPLY);
    scale->set_value2(m.density);
    graph->connect(attribute->output("Fac"), scale->input("Value1"));
    graph->connect(scale->output("Value"), n->input("Density"));
    out = n->output("Volume");
  } else if (m.kind == MaterialKind::PrincipledVolume) {
    auto *n = graph->create_node<ccl::PrincipledVolumeNode>();
    n->set_color(vec(m.color));
    n->set_density(m.density);
    n->set_density_attribute(ccl::ustring("density"));
    n->set_color_attribute(ccl::ustring());
    n->set_anisotropy(m.anisotropy);
    n->set_temperature_attribute(ccl::ustring("temperature"));
    n->set_temperature(m.temperature_kelvin_scale);
    n->set_blackbody_intensity(m.blackbody_intensity);
    n->set_emission_color(vec(m.volume_emission_color));
    if (m.volume_emission_strength > 0) {
      auto *attribute = graph->create_node<ccl::AttributeNode>();
      if (m.emission_field.empty()) {
        attribute->set_attribute(ccl::ustring("density"));
        auto *scale = graph->create_node<ccl::MathNode>();
        scale->set_math_type(ccl::NODE_MATH_MULTIPLY);
        scale->set_value2(m.volume_emission_strength);
        graph->connect(attribute->output("Fac"), scale->input("Value1"));
        graph->connect(scale->output("Value"), n->input("Emission Strength"));
      } else {
        attribute->set_attribute(ccl::ustring(m.emission_field));
        auto *tint = graph->create_node<ccl::VectorMathNode>();
        tint->set_math_type(ccl::NODE_VECTOR_MATH_MULTIPLY);
        tint->set_vector2(vec(m.volume_emission_color));
        graph->connect(attribute->output("Color"), tint->input("Vector1"));
        graph->connect(tint->output("Vector"), n->input("Emission Color"));
        n->set_emission_strength(m.volume_emission_strength);
      }
    }
    out = n->output("Volume");
  } else if (m.kind == MaterialKind::DiagnosticSurface) {
    // Visible even in an unlit scene, without emitting light into it.
    auto *camera = graph->create_node<ccl::LightPathNode>();
    auto *diffuse = graph->create_node<ccl::DiffuseBsdfNode>();
    diffuse->set_color(vec(m.color));
    auto *marker = graph->create_node<ccl::EmissionNode>();
    marker->set_color(vec(m.color));
    marker->set_strength(1);
    auto *mix = graph->create_node<ccl::MixClosureNode>();
    graph->connect(camera->output("Is Camera Ray"), mix->input("Fac"));
    graph->connect(diffuse->output("BSDF"), mix->input("Closure1"));
    graph->connect(marker->output("Emission"), mix->input("Closure2"));
    out = mix->output("Closure");
  } else if (m.kind == MaterialKind::Emission) {
    auto *n = graph->create_node<ccl::EmissionNode>();
    n->set_color(vec(m.color));
    n->set_strength(m.emission_strength);
    out = n->output("Emission");
    surface_color = n->input("Color");
  } else if (m.kind == MaterialKind::Principled) {
    auto *n = graph->create_node<ccl::PrincipledBsdfNode>();
    n->set_base_color(vec(m.color));
    n->set_roughness(m.roughness);
    n->set_metallic(m.metallic);
    out = n->output("BSDF");
    surface_color = n->input("Base Color");
  } else {
    auto *n = graph->create_node<ccl::DiffuseBsdfNode>();
    n->set_color(vec(m.color));
    n->set_roughness(m.roughness);
    out = n->output("BSDF");
    surface_color = n->input("Color");
  }
  if (surface_color && !m.color_attribute.empty()) {
    auto *attribute = graph->create_node<ccl::AttributeNode>();
    attribute->set_attribute(ccl::ustring(m.color_attribute));
    auto *tint = graph->create_node<ccl::VectorMathNode>();
    tint->set_math_type(ccl::NODE_VECTOR_MATH_MULTIPLY);
    tint->set_vector2(vec(m.color));
    graph->connect(attribute->output("Color"), tint->input("Vector1"));
    graph->connect(tint->output("Vector"), surface_color);
  }
  graph->connect(out, graph->output()->input(volume ? "Volume" : "Surface"));
  shader->set_graph(std::move(graph));
  shader->tag_update(session->scene.get());
  material_definitions[m.key] = m;
  ++material_builds;
}

RenderSession::Impl::BindingHandoffs RenderSession::Impl::prepare_binding_handoffs(
    const SceneUpdate &update, const std::set<BindingKey> &recovered,
    const std::map<std::string, ShaderGraphRequirements> &requirements) const {
  BindingHandoffs result;
  if (update.meshes.empty() || update.deleted_geometry.empty())
    return result;
  const std::set<std::string> deleted_geometry(update.deleted_geometry.begin(), update.deleted_geometry.end());
  const std::set<std::string> deleted_materials(update.deleted_materials.begin(), update.deleted_materials.end());
  std::map<std::string, std::vector<std::string>> before, after;
  for (const auto &[key, object] : objects)
    if (deleted_geometry.contains(object.geometry_key))
      before[object.geometry_key].push_back(key);
  for (const auto &object : update.objects)
    after[object.geometry_key].push_back(object.key);
  for (auto &[key, users] : after)
    std::sort(users.begin(), users.end());
  std::map<std::string, const MaterialData *> incoming;
  for (const auto &material : update.materials)
    incoming.emplace(material.key, &material);
  const auto recovered_shader = [&](const std::string &key) {
    return std::any_of(binding_materials.begin(), binding_materials.end(),
                       [&](const auto &item) { return item.first.first == key; }) ||
           std::any_of(recovered.begin(), recovered.end(), [&](const auto &item) { return item.first == key; });
  };
  const auto available_shader = [&](const std::string &previous, const std::string &next) {
    return materials.contains(previous) && !materials.contains(next) && deleted_materials.contains(previous) &&
           material_definitions.contains(previous) && incoming.contains(next) && !recovered_shader(previous) &&
           !recovered_shader(next);
  };
  // Only an exact, nonempty group of surviving object owners can hand off
  // storage. Splitting/merging prototypes and ambiguous shader aliases use
  // ordinary conversion; they must never steal a neighbour's geometry.
  std::map<std::string, std::set<std::string>> shader_targets, shader_sources;
  std::set<std::string> claimed, inherited;
  for (const auto &mesh : update.meshes) {
    if (meshes.contains(mesh->key) || after[mesh->key].empty())
      continue;
    const auto first = objects.find(after[mesh->key].front());
    if (first == objects.end())
      continue;
    const auto old = meshes.find(first->second.geometry_key);
    if (old == meshes.end() || !deleted_geometry.contains(old->first) || before[old->first] != after[mesh->key])
      continue;
    const auto &record = old->second;
    if (!record.source || record.dirty || !same_mesh_geometry(*record.source, *mesh))
      continue;
    bool compatible = mesh->materials.size() == record.materials.size();
    std::vector<std::pair<std::string, std::string>> aliases;
    for (std::size_t slot = 0; compatible && slot < mesh->materials.size(); ++slot) {
      const auto &previous = record.materials[slot];
      const auto &next = mesh->materials[slot];
      if (previous == next)
        continue;
      if (!available_shader(previous, next)) {
        compatible = false;
        break;
      }
      const auto count = material_definitions.at(previous).displacement_samples.size();
      if (count != incoming.at(next)->displacement_samples.size()) {
        compatible = false;
        break;
      }
      aliases.emplace_back(previous, next);
      // A sampled mesh stores auxiliary shader pointers in its owned
      // palette. Transfer the entire unchanged slot layout together;
      // replacing only its surface shader would leave retired pointers.
      for (std::size_t step = 0; step < count; ++step) {
        auto old_sample = temporal_material_key(previous, step), new_sample = temporal_material_key(next, step);
        if (!available_shader(old_sample, new_sample)) {
          compatible = false;
          break;
        }
        aliases.emplace_back(std::move(old_sample), std::move(new_sample));
      }
      if (!compatible)
        break;
    }
    if (!compatible)
      aliases.clear(); // Physical storage can still transfer without aliasing shaders.
    for (const auto &[previous, next] : aliases) {
      shader_targets[previous].insert(next);
      shader_sources[next].insert(previous);
    }
    result.meshes.push_back({old->first, mesh, std::move(aliases)});
    claimed.insert(old->first);
    inherited.insert(mesh->key);
  }
  // A palette permutation can move owners between surviving bindings.
  // An exact owner group is then unavailable even though another retiring
  // logical record owns the identical body. Reuse only that retired
  // record, once, with exact payload equality and surface-only palettes.
  // Shader pointers never transfer between these unrelated owner groups.
  const auto surface_only = [&](const MeshData &source) {
    if ((source.subdivision && source.subdivision->settings.adaptive) || source.nominal_pose)
      return false;
    for (const auto &key : source.materials) {
      if (recovered_shader(key))
        return false;
      const auto next = incoming.find(key);
      const auto previous = material_definitions.find(key);
      const auto *material = next != incoming.end()                   ? next->second
                             : previous != material_definitions.end() ? &previous->second
                                                                      : nullptr;
      if (!material || material->kind == MaterialKind::VolumeAbsorption ||
          material->kind == MaterialKind::PrincipledVolume || !material->displacement_samples.empty() ||
          (material->graph && material->graph->displacement &&
           material->graph->displacement_method != DisplacementMethod::Bump))
        return false;
      const auto needed = requirements.find(key);
      const auto old = material_requirements.find(key);
      if ((needed != requirements.end() && needed->second.volume) ||
          (old != material_requirements.end() && old->second.volume))
        return false;
    }
    return true;
  };
  std::map<std::uint64_t, std::vector<std::string>> candidates;
  for (const auto &key : deleted_geometry)
    if (!claimed.contains(key)) {
      const auto found = meshes.find(key);
      if (found == meshes.end())
        continue;
      const auto &record = found->second;
      if (!record.source || record.dirty || record.displaced || !surface_only(*record.source))
        continue;
      const auto hash = record.hashed_source == record.source ? record.shape_hash : mesh_shape_hash(*record.source);
      candidates[hash].push_back(key);
    }
  if (!candidates.empty())
    for (const auto &mesh : update.meshes) {
      if (meshes.contains(mesh->key) || inherited.contains(mesh->key) || after[mesh->key].empty() ||
          !surface_only(*mesh))
        continue;
      const auto bucket = candidates.find(mesh_shape_hash(*mesh));
      if (bucket == candidates.end())
        continue;
      for (const auto &key : bucket->second)
        if (!claimed.contains(key) && same_mesh_payload(*meshes.at(key).source, *mesh)) {
          result.meshes.push_back({key, mesh, {}});
          claimed.insert(key);
          break;
        }
    }
  for (auto &handoff : result.meshes) {
    const bool ambiguous = std::any_of(handoff.shaders.begin(), handoff.shaders.end(), [&](const auto &pair) {
      return shader_targets[pair.first].size() != 1 || shader_sources[pair.second].size() != 1;
    });
    if (ambiguous)
      handoff.shaders.clear();
  }
  for (const auto &handoff : result.meshes)
    for (const auto &[previous, next] : handoff.shaders)
      result.shaders.emplace(next, previous);
  return result;
}

std::set<ccl::Shader *> RenderSession::Impl::apply_binding_handoffs(const BindingHandoffs &handoffs) {
  std::set<ccl::Shader *> retained;
  for (const auto &[next, previous] : handoffs.shaders) {
    auto *shader = materials.at(previous);
    materials.emplace(next, shader);
    retained.insert(shader);
    auto definition = material_definitions.at(previous);
    definition.key = next;
    material_definitions.emplace(next, std::move(definition));
    if (const auto found = material_requirements.find(previous); found != material_requirements.end())
      material_requirements.emplace(next, found->second);
  }
  std::map<std::string, std::string> keys;
  for (const auto &handoff : handoffs.meshes) {
    auto entry = meshes.extract(handoff.previous);
    auto &record = entry.mapped();
    const auto old_source = record.source;
    if (same_mesh_payload(*old_source, *handoff.source)) {
      // A binding handoff has the same caller-isolation contract as
      // update_mesh. A const shared_ptr does not freeze its sender.
      record.source = std::make_shared<MeshData>(*handoff.source);
      record.materials = handoff.source->materials;
      record.revision = handoff.source->revision;
      if (record.seam_source == old_source)
        record.seam_source = record.source;
      else if (record.seam_source) {
        auto source = std::make_shared<MeshData>(*record.seam_source);
        source->key = record.source->key;
        source->revision = record.revision;
        source->materials = record.materials;
        record.seam_source = std::move(source);
      }
    } else
      record.force_source_update = true;
    entry.key() = handoff.source->key;
    meshes.insert(std::move(entry));
    keys.emplace(handoff.previous, handoff.source->key);
    mesh_update_profile(generation, "binding_storage_handoff");
  }
  if (!keys.empty())
    for (auto &[key, object] : objects)
      if (const auto found = keys.find(object.geometry_key); found != keys.end())
        object.geometry_key = found->second;
  return retained;
}

bool RenderSession::Impl::palette_generated(const std::vector<std::string> &palette) const {
  const auto needed = [&](const auto &key) {
    const auto found = material_requirements.find(key);
    return found != material_requirements.end() && (found->second.requires_generated || found->second.retain_generated);
  };
  for (const auto &key : palette) {
    if (needed(key))
      return true;
    if (const auto found = material_definitions.find(key); found != material_definitions.end())
      for (std::size_t step = 0; step < found->second.displacement_samples.size(); ++step)
        if (needed(temporal_material_key(key, step)))
          return true;
  }
  return false;
}

bool RenderSession::Impl::palette_islands(const std::vector<std::string> &palette) const {
  const auto needed = [&](const auto &key) {
    const auto found = material_requirements.find(key);
    return found != material_requirements.end() && found->second.requires_mesh_islands;
  };
  for (const auto &key : palette) {
    if (needed(key))
      return true;
    if (const auto found = material_definitions.find(key); found != material_definitions.end())
      for (std::size_t step = 0; step < found->second.displacement_samples.size(); ++step)
        if (needed(temporal_material_key(key, step)))
          return true;
  }
  return false;
}

bool RenderSession::Impl::palette_pointiness(const std::vector<std::string> &palette) const {
  const auto needed = [&](const std::string &key) {
    const auto found = material_requirements.find(key);
    return found != material_requirements.end() && found->second.requires_mesh_pointiness;
  };
  for (const auto &key : palette) {
    if (needed(key))
      return true;
    if (const auto found = material_definitions.find(key); found != material_definitions.end())
      for (std::size_t step = 0; step < found->second.displacement_samples.size(); ++step)
        if (needed(temporal_material_key(key, step)))
          return true;
  }
  return false;
}

ccl::Shader *RenderSession::Impl::binding_shader(const std::string &key, bool has_uv, bool has_color, bool strands,
                                                 const AttributeLayout &attributes,
                                                 const AttributeLayout &instances) const {
  if (key.empty())
    return session->scene->default_surface;
  if (auto found = binding_materials.find(binding_key(key, has_uv, has_color, strands, attributes, instances));
      found != binding_materials.end())
    return found->second;
  return materials.at(key);
}

RenderSession::Impl::MeshDisplacementState
RenderSession::Impl::mesh_displacement_state(const MeshData &source, bool sampled,
                                             const std::set<std::string> &frozen) const {
  MeshDisplacementState result;
  const auto layout = attribute_layout(source.attributes, source.instance_attributes);
  const auto add_requirements = [&](const ShaderGraphRequirements &required) {
    result.fields.uv |= required.displacement_requires_uv;
    result.fields.color |= required.displacement_requires_color;
    result.fields.pointiness |= required.displacement_requires_pointiness;
    result.fields.named.insert(required.displacement_named_attributes.begin(),
                               required.displacement_named_attributes.end());
    result.transform |= required.displacement_transform_dependent;
    result.identity |= required.displacement_identity_dependent;
    result.object_index |= required.displacement_requires_object_index;
  };
  const auto shader_stamp = [&](const ccl::Shader *shader, const ShaderGraphRequirements &required) {
    if (!shader->graph)
      throw std::logic_error("Active displacement shader has no owned graph");
    return std::to_string(int(shader->get_displacement_method())) + ":" + shader->graph->displacement_hash + ":" +
           std::to_string(required.displacement_requires_material_index ? shader->get_pass_id() : 0) + ":" +
           std::to_string(target_color_epoch);
  };
  for (const auto &material : source.materials) {
    std::string stamp;
    if (!material.empty()) {
      auto *shader = binding_shader(material, !source.corner_uv.empty(), !source.corner_colors.empty(), false, layout,
                                    source.instance_attributes);
      const auto key = binding_key(material, !source.corner_uv.empty(), !source.corner_colors.empty(), false, layout,
                                   source.instance_attributes);
      const auto recovered = recovered_requirements.find(key);
      const auto original = material_requirements.find(material);
      const auto *required = recovered != recovered_requirements.end() ? &recovered->second
                             : original != material_requirements.end() ? &original->second
                                                                       : nullptr;
      if (required && required->displacement && shader->get_displacement_method() != ccl::DISPLACE_BUMP) {
        result.active = true;
        add_requirements(*required);
        stamp = shader_stamp(shader, *required);
        stamp += sampled ? "/sampled" : "/static";
        if (sampled && !frozen.contains(material)) {
          const auto &samples = material_definitions.at(material).displacement_samples;
          for (std::size_t step = 0; step < samples.size(); ++step) {
            const auto sample = temporal_material_key(material, step);
            const auto &sample_required = material_requirements.at(sample);
            add_requirements(sample_required);
            stamp += "/" + shader_stamp(materials.at(sample), sample_required);
          }
        }
      }
    }
    result.slots.push_back(std::move(stamp));
  }
  return result;
}

std::vector<ccl::Shader *> RenderSession::Impl::resolved_mesh_palette(const MeshRecord &record) const {
  std::vector<ccl::Shader *> result;
  if (record.materials.empty())
    result.push_back(session->scene->default_surface);
  else
    for (const auto &key : record.materials)
      result.push_back(
          binding_shader(key, record.has_uv, record.has_color, false, record.attributes, record.instance_attributes));
  return result;
}

bool RenderSession::Impl::surface_mesh_sharing_allowed(const MeshRecord &record) const {
  const auto *disabled = std::getenv("CYCLES_HOUDINI_DISABLE_MESH_SHARING");
  if (disabled && std::string_view(disabled) == "1")
    return false;
  if (mesh_displacement_state(*record.source, record.sampled_displacement, record.frozen_temporal_materials).active)
    return false;
  for (const auto &material : record.materials) {
    const auto key =
        binding_key(material, record.has_uv, record.has_color, false, record.attributes, record.instance_attributes);
    const auto recovered = recovered_requirements.find(key);
    const auto original = material_requirements.find(material);
    const auto *required = recovered != recovered_requirements.end() ? &recovered->second
                           : original != material_requirements.end() ? &original->second
                                                                     : nullptr;
    if (required && required->volume)
      return false;
  }
  return true;
}

void RenderSession::Impl::retire_shader(ccl::Shader *shader) {
  if (!shader)
    return;
  if (shader->reference_count() < 0)
    throw std::logic_error("Retired shader has a negative engine reference count");
  if (shader->reference_count() > 0) {
    deferred_retired_shaders.insert(shader);
    return;
  }
  apply_material_settings(shader, MaterialSettings{}, false);
  shader->set_displacement_method(ccl::DISPLACE_BUMP);
  shader->set_pass_id(0);
  shader->set_graph(ccl::make_unique<ccl::ShaderGraph>());
  shader->tag_update(session->scene.get());
  shader->name = ccl::ustring("retired_integration_shader");
  session->scene->delete_nodes(ccl::set<ccl::Shader *>{shader});
  retired_shaders.push_back(shader);
}

namespace {
// What a material asks of the geometry and of mesh variants: everything in the requirements except
// diagnostics and image/IES resources (those reach the shader, not the mesh).
bool same_geometry_requirements(const ShaderGraphRequirements &a, const ShaderGraphRequirements &b) {
  return a.surface == b.surface && a.volume == b.volume && a.displacement == b.displacement &&
         a.volume_diagnostic == b.volume_diagnostic && a.diagnostic_surface == b.diagnostic_surface &&
         a.displacement_transform_dependent == b.displacement_transform_dependent &&
         a.displacement_identity_dependent == b.displacement_identity_dependent &&
         a.displacement_camera_transform_dependent == b.displacement_camera_transform_dependent &&
         a.displacement_camera_projection_dependent == b.displacement_camera_projection_dependent &&
         a.displacement_deformation_dependent == b.displacement_deformation_dependent &&
         a.displacement_requires_uv == b.displacement_requires_uv &&
         a.displacement_requires_color == b.displacement_requires_color &&
         a.displacement_requires_pointiness == b.displacement_requires_pointiness &&
         a.displacement_requires_object_index == b.displacement_requires_object_index &&
         a.displacement_requires_material_index == b.displacement_requires_material_index &&
         a.displacement_motion_supported == b.displacement_motion_supported && a.requires_uv == b.requires_uv &&
         a.requires_color == b.requires_color && a.requires_mesh_tangents == b.requires_mesh_tangents &&
         a.tangent_bases == b.tangent_bases && a.named_tangent_bases == b.named_tangent_bases &&
         a.requires_generated == b.requires_generated && a.requires_mesh_islands == b.requires_mesh_islands &&
         a.requires_mesh_pointiness == b.requires_mesh_pointiness && a.retain_generated == b.retain_generated &&
         a.named_attributes == b.named_attributes &&
         a.displacement_named_attributes == b.displacement_named_attributes && a.volume_fields == b.volume_fields &&
         a.scalar_volume_fields == b.scalar_volume_fields && a.vector_volume_fields == b.vector_volume_fields;
}
bool true_displacement(const MaterialData &m) {
  return (m.graph && m.graph->displacement && m.graph->displacement_method != DisplacementMethod::Bump) ||
         !m.displacement_samples.empty();
}
bool material_values_rule() {
  static const bool enabled = [] {
    const char *value = std::getenv("CYCLES_HOUDINI_MATERIAL_VALUES_ONLY");
    return !(value && std::strcmp(value, "0") == 0);
  }();
  return enabled;
}
} // namespace

std::vector<ccl::Shader *> RenderSession::Impl::sync_material_bindings(const SceneUpdate &u, PreparedUpdate &prepared,
                                                                       std::set<std::string> &variant_updates) {
  auto &i = *this;
  auto *scene = i.session->scene.get();
  auto &effective_materials = prepared.effective_materials;
  auto &prepared_graphs = prepared.prepared_graphs;
  auto &requirements = prepared.requirements;
  auto &recovered_requirements = prepared.recovered_requirements;
  auto &volume_recovery = prepared.volume_recovery;
  auto &bindings_needed = prepared.bindings_needed;
  auto &prepared_bindings = prepared.prepared_bindings;
  auto &prepared_volume_vectors = prepared.prepared_volume_vectors;
  // A value-only material edit: decided against the definitions and requirements before they are replaced.
  bool values_only = material_values_rule() && !effective_materials.empty() && !i.color_update_pending;
  for (const auto &material : effective_materials) {
    if (!values_only)
      break;
    const auto old_definition = i.material_definitions.find(material.key);
    const auto old_needed = i.material_requirements.find(material.key);
    const auto new_needed = requirements.find(material.key);
    values_only = old_definition != i.material_definitions.end() && old_needed != i.material_requirements.end() &&
                  new_needed != requirements.end() && !true_displacement(old_definition->second) &&
                  !true_displacement(material) && same_geometry_requirements(old_needed->second, new_needed->second);
  }
  // Install material graphs and rebind the retained geometry palettes.
  for (const auto &material : effective_materials) {
    const auto found = prepared_graphs.find(material.key);
    const auto needed = requirements.find(material.key);
    i.update_material(material,
                      found == prepared_graphs.end() ? ccl::unique_ptr<ccl::ShaderGraph>{} : std::move(found->second),
                      needed != requirements.end() && needed->second.diagnostic_surface);
    if (i.color_update_pending && material.graph && material.graph->displacement &&
        material.graph->displacement_method != DisplacementMethod::Bump)
      i.materials.at(material.key)->need_update_displacement = true;
  }
  std::set<std::string> added_tangent_attributes;
  for (const auto &[key, needed] : requirements) {
    const auto old = i.material_requirements.find(key);
    if (needed.tangent_bases & ~(old == i.material_requirements.end() ? 0u : old->second.tangent_bases))
      added_tangent_attributes.insert(key);
    for (const auto &[name, bases] : needed.named_tangent_bases)
      if (old == i.material_requirements.end() || !old->second.named_tangent_bases.contains(name) ||
          (bases & ~old->second.named_tangent_bases.at(name)))
        added_tangent_attributes.insert(key);
  }
  i.material_requirements = std::move(requirements);
  i.recovered_requirements = std::move(recovered_requirements);
  i.volume_recovery = std::move(volume_recovery);
  // Keep removed variants alive until every old palette has been rebound.
  std::vector<ccl::Shader *> retired_binding_shaders;
  for (auto it = i.binding_materials.begin(); it != i.binding_materials.end();) {
    if (!bindings_needed.contains(it->first)) {
      values_only = false;
      retired_binding_shaders.push_back(it->second);
      it = i.binding_materials.erase(it);
    } else
      ++it;
  }
  for (auto &[key, graph] : prepared_bindings) {
    auto *&shader = i.binding_materials[key];
    if (!shader) {
      values_only = false; // A new binding shader enters the mesh palettes.
      if (i.retired_shaders.empty())
        shader = scene->create_node<ccl::Shader>();
      else {
        shader = i.retired_shaders.back();
        i.retired_shaders.pop_back();
        ++i.shader_reuses;
      }
    }
    const auto &source = i.material_definitions.at(key.first);
    shader->name = ccl::ustring(source.cryptomatte_name.empty() ? key.first : source.cryptomatte_name);
    shader->set_pass_id(source.pass_index);
    const auto method = source.graph && i.recovered_requirements.at(key).displacement
                            ? source.graph->displacement_method
                            : DisplacementMethod::Bump;
    const auto engine_method = method == DisplacementMethod::True   ? ccl::DISPLACE_TRUE
                               : method == DisplacementMethod::Both ? ccl::DISPLACE_BOTH
                                                                    : ccl::DISPLACE_BUMP;
    const bool method_changed = shader->get_displacement_method() != engine_method;
    shader->set_displacement_method(engine_method);
    shader->set_graph(std::move(graph));
    // A binding whose surface became the camera-only diagnostic marker
    // must not turn every affected triangle into a sampled mesh light.
    apply_material_settings(shader, source.graph ? source.graph->settings : MaterialSettings{},
                            i.recovered_requirements.at(key).diagnostic_surface);
    // Recovered per-geometry bindings have the same surface-only
    // invalidation contract as their source shader (update_material).
    const bool displacement_changed = method_changed || shader->need_update_displacement;
    shader->tag_update(scene);
    shader->need_update_displacement = displacement_changed;
    if (displacement_changed)
      values_only = false;
  }
  i.material_values_only = values_only;
  for (auto &[key, record] : i.meshes) {
    if (record.source && i.palette_islands(record.materials) != record.islands) {
      record.islands = i.palette_islands(record.materials);
      record.shading_dirty = true;
      variant_updates.insert(key);
    }
    if (record.source && i.palette_pointiness(record.materials) != record.has_pointiness) {
      record.has_pointiness = i.palette_pointiness(record.materials);
      record.shading_dirty = true;
      variant_updates.insert(key);
    }
    if (record.volume &&
        std::find(u.deleted_geometry.begin(), u.deleted_geometry.end(), key) == u.deleted_geometry.end() &&
        std::none_of(u.volumes.begin(), u.volumes.end(), [&](const auto &incoming) { return incoming->key == key; })) {
      i.update_volume(record.volume, record.volume_motion_bounds, std::move(prepared_volume_vectors[key]));
    }
    if (record.source && i.palette_generated(record.materials) != record.generated) {
      mesh_update_profile(i.generation, "generated_attributes", record.generated,
                          i.palette_generated(record.materials));
      record.generated = i.palette_generated(record.materials);
      record.shading_dirty = true;
      variant_updates.insert(key);
    } else if (record.kind == Impl::GeometryKind::Points || record.kind == Impl::GeometryKind::Curves)
      i.update_strand_generated(record);
    // A shader can introduce tangent/undisplaced-normal attributes
    // without any SOP change. Cycles generates them only for modified
    // meshes; refresh from our retained source, not by recooking SOPs.
    if (record.source && std::any_of(record.materials.begin(), record.materials.end(), [&](const auto &material) {
          return added_tangent_attributes.contains(material);
        })) {
      record.shading_dirty = true;
      variant_updates.insert(key);
    }
    if (record.kind != Impl::GeometryKind::Volume) {
      ccl::array<ccl::Node *> shaders;
      shaders.reserve(std::max(std::size_t(1), record.materials.size()));
      if (record.materials.empty())
        shaders.push_back_reserved(record.gaussian_splats ? scene->default_gsplat : scene->default_surface);
      else
        for (const auto &material : record.materials) {
          auto *shader =
              material.empty() && record.kind == Impl::GeometryKind::Points &&
                      static_cast<ccl::PointCloud *>(record.node)->get_render_as() == ccl::PointCloud::RENDER_AS_GSPLATS
                  ? scene->default_gsplat
                  : i.binding_shader(material, record.has_uv, record.has_color, record.kind != Impl::GeometryKind::Mesh,
                                     record.attributes, record.instance_attributes);
          shader->tag_used(scene);
          shaders.push_back_reserved(shader);
        }
      // Motion-displacement shaders extend the owned mesh palette.
      // Compare its surface prefix here; changes to the temporal
      // suffix are tracked through displacement_samples below.
      const auto &current = record.node->get_used_shaders();
      const bool palette_changed =
          record.source ? record.surface_palette != i.resolved_mesh_palette(record) : current != shaders;
      if (palette_changed) {
        mesh_update_profile(i.generation, "shader_palette", bool(record.source));
        if (record.source) {
          variant_updates.insert(key);
        } else {
          record.node->set_used_shaders(shaders);
          // Cycles packs engine shader IDs into each curve/point
          // primitive. A palette change alone does not mark the
          // hair shader stream dirty, leaving retired IDs on the
          // device until an unrelated geometry edit.
          if (record.kind == Impl::GeometryKind::Curves)
            static_cast<ccl::Hair *>(record.node)->tag_curve_shader_modified();
          else if (record.kind == Impl::GeometryKind::Points)
            static_cast<ccl::PointCloud *>(record.node)->tag_shader_modified();
          record.node->tag_update(scene, false);
        }
      }
      // A binding whose graph changed keeps its shader node: the mesh variants hold the same shader, so a
      // value-only edit needs no variant work (the engine re-reads the shader from its tag).
      if (!values_only && record.source &&
          std::any_of(record.materials.begin(), record.materials.end(), [&](const auto &m) {
            return prepared_bindings.contains(
                binding_key(m, record.has_uv, record.has_color, false, record.attributes, record.instance_attributes));
          }))
        variant_updates.insert(key);
    }
  }
  for (const auto &key : u.deleted_materials) {
    i.texture_watch_paths.erase(key);
    i.texture_watch_revisions.erase(key);
    i.texture_watch_auto.erase(key);
  }
  if (!effective_materials.empty())
    for (auto &[key, record] : i.meshes)
      if (record.source) {
        // Index-only material edits can affect displacement without changing
        // the graph or setting the engine's displacement-update flag.
        const auto displacement =
            i.mesh_displacement_state(*record.source, record.sampled_displacement, record.frozen_temporal_materials);
        if (!same_displacement_assignments(*record.source, record.displacement_slots, *record.source,
                                           displacement.slots))
          variant_updates.insert(key);
        for (const auto &material : record.materials) {
          bool changed = i.materials.at(material)->need_update_displacement;
          const auto &samples = i.material_definitions.at(material).displacement_samples;
          for (std::size_t step = 0; step < samples.size(); ++step)
            changed |= i.materials.at(temporal_material_key(material, step))->need_update_displacement;
          if (changed) {
            variant_updates.insert(key);
            break;
          }
        }
      }
  return retired_binding_shaders;
}

} // namespace cycles_houdini
