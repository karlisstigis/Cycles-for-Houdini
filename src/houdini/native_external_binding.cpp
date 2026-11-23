// SPDX-License-Identifier: Apache-2.0
#include "native_external_binding.h"
#include "scene_adapter.h"
#include <algorithm>
#include <stdexcept>

namespace cycles_houdini {
std::string NativeExternalBindings::resolve(const GeometryRequest &member, const GeometryRequest &owner,
                                            const NativeMaterialAssignment &point_binding,
                                            const std::vector<UniformAttribute> &fields, const Context &context) {
  if (!member.external_mesh)
    throw std::logic_error("External instance has no native mesh");
  const auto binding = inherit_native_material(point_binding, owner.material_assignment());
  const auto &paths =
      member.external_material_paths.empty() ? member.external_mesh->materials : member.external_material_paths;
  std::string variant = member.geometry_key + "/binding:" + binding.path;
  const auto append = [&](const std::string &value) { variant += ":" + std::to_string(value.size()) + ":" + value; };
  append(std::to_string(paths.size()));
  for (const auto &path : paths)
    append(path);
  for (const auto *layers : {&binding.overrides, &binding.surface_overrides, &binding.displacement_overrides}) {
    append("layer");
    for (const auto &layer : *layers)
      append(layer);
  }
  append(owner.material_key);
  append(std::to_string(int(owner.material_mode)));
  append(owner.motion_blur ? "blur" : "static");
  append(owner.stylesheet);
  const auto style_cwd = owner.object_path.empty() ? owner.sop_path : owner.object_path;
  if (!owner.stylesheet.empty())
    append(style_cwd);

  AttributeLayout layout;
  // Link hair and point clouds ride the same path as a MeshData carrier
  // (MeshData::external_curves, MeshData::external_points).
  const auto *curves = member.external_mesh->external_curves.get();
  const auto *points = member.external_mesh->external_points.get();
  const auto &authored = curves ? curves->attributes : points ? points->attributes : member.external_mesh->attributes;
  for (const auto &field : fields) {
    const bool authored_color = field.name == "Cd" && (curves   ? !curves->colors.empty()
                                                       : points ? !points->colors.empty()
                                                                : !member.external_mesh->corner_colors.empty());
    const bool authored_attribute = std::any_of(authored.begin(), authored.end(),
                                                [&](const auto &attribute) { return attribute.name == field.name; });
    if (!authored_color && !authored_attribute) {
      layout.emplace(field.name, field.components);
      append(field.name);
      append(std::to_string(field.components));
    }
  }
  if (const auto found = variants_.find(variant); found != variants_.end())
    return found->second;

  NativeMaterialStyles::Layer style;
  if (owner.material_mode == NativeMaterialMode::Native && !owner.stylesheet.empty()) {
    auto [found, inserted] = styles_.try_emplace(std::make_pair(owner.stylesheet, style_cwd));
    if (inserted) {
      found->second = external_uniform_style(owner.stylesheet, context.time, style_cwd);
      for (auto issue : found->second.diagnostics) {
        issue.source = owner.sop_path;
        issue.resource = owner.geometry_key;
        context.record_issue(std::move(issue));
      }
      context.stylesheet_resources.insert(found->second.resources.begin(), found->second.resources.end());
    }
    style = found->second.layer;
  }
  for (const auto &message : owner.stylesheet_errors) {
    context.record_issue({"material.stylesheet_ignored", owner.sop_path, owner.geometry_key, message,
                          "Keeping supported blanket stylesheet rules and underlying material bindings", "warning",
                          "material_stylesheet"});
  }

  std::vector<NativeMaterialReference> references;
  NativeMaterialLookup lookup;
  std::vector<std::string> materials;
  for (const auto &path : paths) {
    auto assignment = inherit_native_material(NativeMaterialAssignment(path), binding);
    assignment = styled_material(std::move(assignment), style);
    if (owner.material_mode != NativeMaterialMode::Native || assignment.path.empty()) {
      materials.push_back(owner.material_key);
    } else {
      auto reference = resolve_native_material(assignment);
      materials.push_back(reference.key);
      if (reference.diagnostic)
        context.record_issue(*reference.diagnostic);
      lookup.emplace(reference.assignment(), reference);
      context.material_paths.insert(reference.path);
      references.push_back(std::move(reference));
    }
  }
  if (materials.empty())
    materials.push_back(owner.material_key);
  if (context.prepare_materials && !lookup.empty())
    context.prepare_materials(lookup);

  auto key = member.geometry_key + "/native-bindings";
  for (const auto &material : materials)
    key += ":" + std::to_string(material.size()) + ":" + material;
  for (const auto &[name, components] : layout)
    key += "/field:" + name + ":" + std::to_string(components);
  if (!owner.motion_blur)
    key += "/motion:disabled";
  auto rendered = context.find_rendered(key, member.external_mesh);
  if (!rendered) {
    // MeshData's shared arrays retain immutable geometry storage across palettes.
    auto mesh = std::make_shared<MeshData>(*member.external_mesh);
    mesh->key = key;
    mesh->materials = std::move(materials);
    mesh->instance_attributes = layout;
    mesh->motion_blur = owner.motion_blur;
    mesh->revision = ++context.next_revision;
    if (curves) {
      // The vectors are copied once per material/field variant (CurveData owns plain vectors).
      auto rendered_curves = std::make_shared<CurveData>(*curves);
      rendered_curves->key = key;
      rendered_curves->materials = mesh->materials;
      rendered_curves->instance_attributes = layout;
      rendered_curves->revision = mesh->revision;
      mesh->external_curves = std::move(rendered_curves);
    }
    if (points) {
      // As for curves: PointData owns plain vectors, copied once per variant.
      auto rendered_points = std::make_shared<PointData>(*points);
      rendered_points->key = key;
      rendered_points->materials = mesh->materials;
      rendered_points->instance_attributes = layout;
      rendered_points->revision = mesh->revision;
      mesh->external_points = std::move(rendered_points);
    }
    rendered = std::move(mesh);
  }
  context.publish(key, Mesh{member.external_mesh, std::move(rendered), std::move(references)});
  variants_.emplace(std::move(variant), key);
  return key;
}
} // namespace cycles_houdini
