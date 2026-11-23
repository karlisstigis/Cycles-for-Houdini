// SPDX-License-Identifier: Apache-2.0
// Host evaluation is deterministic here; runtime style tests cover real HDK nodes.
#include "../src/houdini/scene_adapter.h"
#include "../src/houdini/native_external_binding.h"
#include <iostream>
#include <stdexcept>
namespace ch = cycles_houdini;
namespace cycles_houdini {
NativeMaterialAssignment inherit_native_material(NativeMaterialAssignment local,
                                                 const NativeMaterialAssignment &inherited) {
  if (local.path.starts_with("op:"))
    local.path.erase(0, 3);
  const auto path = inherited.path.starts_with("op:") ? inherited.path.substr(3) : inherited.path;
  if (local.path.empty() || local.path == path) {
    if (local.path.empty())
      local.path = path;
    local.overrides.insert(local.overrides.begin(), inherited.overrides.begin(), inherited.overrides.end());
    local.surface_overrides.insert(local.surface_overrides.begin(), inherited.surface_overrides.begin(),
                                   inherited.surface_overrides.end());
    local.displacement_overrides.insert(local.displacement_overrides.begin(), inherited.displacement_overrides.begin(),
                                        inherited.displacement_overrides.end());
  }
  return local;
}
NativeMaterialReference resolve_native_material(const NativeMaterialAssignment &assignment, bool) {
  NativeMaterialReference value;
  value.path = assignment.path;
  value.key = "resolved:" + assignment.path;
  value.overrides = assignment.overrides;
  value.surface_overrides = assignment.surface_overrides;
  value.displacement_overrides = assignment.displacement_overrides;
  for (const auto *layers : {&assignment.overrides, &assignment.surface_overrides, &assignment.displacement_overrides})
    for (const auto &layer : *layers)
      value.key += ":" + layer;
  if (assignment.path == "missing")
    value.diagnostic = SourceDiagnostic{"missing", "source", "", "missing", "fallback"};
  return value;
}
NativeExternalMaterialStyle external_uniform_style(const std::string &style, double, const std::string &) {
  NativeExternalMaterialStyle value;
  if (style == "blanket")
    value.layer.material = "/mat/style";
  else if (style == "parameter")
    value.layer.parameters = "power:3";
  else
    value.diagnostics.push_back({"material.external_stylesheet", "", "", "selective omitted", "continue"});
  value.resources = {"style-resource"};
  return value;
}
NativeMaterialAssignment styled_material(NativeMaterialAssignment assignment,
                                         const NativeMaterialStyles::Layer &layer) {
  if (!layer.material.empty())
    assignment.path = layer.material;
  if (!layer.parameters.empty())
    assignment.overrides.push_back(layer.parameters);
  return assignment;
}
} // namespace cycles_houdini
using namespace cycles_houdini;
void require(bool value, const char *message) {
  if (!value)
    throw std::runtime_error(message);
}
struct PreparedScene {
  struct ExternalMesh {
    std::shared_ptr<const MeshData> source, mesh;
    std::vector<NativeMaterialReference> references;
  };
  std::map<std::string, ExternalMesh> external_meshes;
};
struct CachedMesh {
  std::shared_ptr<const MeshData> external_mesh, external_render_mesh;
};
int main(int argc, char **argv) {
  std::map<std::string, CachedMesh> cache_;
  auto source = std::make_shared<MeshData>();
  source->key = "shared-source";
  source->positions = {{0, 0, 0}, {1, 0, 0}, {0, 1, 0}};
  source->triangles = {{0, 1, 2}};
  source->corner_uv = {{0, 0}, {1, 0}, {0, 1}};
  source->materials = {"/mat/source"};
  source->corner_colors = {{1, 0, 0}, {0, 1, 0}, {0, 0, 1}};
  source->attributes.push_back({"authored", 3, {{1, 2, 3}}});
  std::uint64_t next_revision_ = 0;
  for (int round = 0; round < 2; ++round) {
    auto prepared = std::make_shared<PreparedScene>();
    std::vector<SourceDiagnostic> diagnostics;
    const auto record_issue = [&](SourceDiagnostic issue) { diagnostics.push_back(std::move(issue)); };
    std::set<std::string> material_paths_, stylesheet_resources_;
    std::size_t prepared_count = 0;
    NativeMaterialPreparation prepare_materials;
    prepare_materials.prepare = [&](const NativeMaterialLookup &lookup) {
      prepared_count += lookup.size();
      return std::set<std::string>{};
    };
    double time = 1.25;
    NativeExternalBindings binding;
    NativeExternalBindings::Context context{
        time,
        next_revision_,
        material_paths_,
        stylesheet_resources_,
        prepare_materials,
        record_issue,
        [&](const std::string &key, const std::shared_ptr<const MeshData> &mesh) {
          const auto found = cache_.find(key);
          return found != cache_.end() && found->second.external_mesh == mesh ? found->second.external_render_mesh
                                                                              : nullptr;
        },
        [&](const std::string &key, NativeExternalBindings::Mesh mesh) {
          prepared->external_meshes.try_emplace(
              key, PreparedScene::ExternalMesh{mesh.source, mesh.rendered, mesh.references});
        }};
    GeometryRequest member;
    member.geometry_key = "shared";
    member.external_mesh = source;
    GeometryRequest owner;
    owner.geometry_key = "owner";
    owner.sop_path = "/obj/source";
    owner.object_path = "/obj";
    owner.material_key = "fallback";
    owner.material_mode = NativeMaterialMode::Native;
    owner.object_material_path = "/mat/owner";
    owner.object_material_overrides = {"base:1"};
    const auto resolve = [&](const NativeMaterialAssignment &point = NativeMaterialAssignment{},
                             const std::vector<UniformAttribute> &fields = std::vector<UniformAttribute>{}) {
      return binding.resolve(member, owner, point, fields, context);
    };
    const auto a = resolve();
    require(resolve() == a, "Same capture binding memo changed");
    require(prepared->external_meshes.at(a).mesh->materials == std::vector<std::string>{"resolved:/mat/source"},
            "Source slot priority changed");
    member.external_material_paths = {"/mat/alternate"};
    const auto b = resolve();
    require(a != b, "Two resident palettes collapsed");
    member.external_material_paths = {""};
    const auto c = resolve(NativeMaterialAssignment("/mat/point"));
    require(prepared->external_meshes.at(c).mesh->materials == std::vector<std::string>{"resolved:/mat/point"},
            "Point fallback changed");
    owner.stylesheet = "blanket";
    const auto d = resolve();
    owner.stylesheet = "parameter";
    const auto e = resolve();
    owner.stylesheet = "selective";
    const auto f = resolve();
    owner.stylesheet = "";
    owner.stylesheet_errors = {"malformed"};
    owner.motion_blur = false;
    const auto g = resolve({}, {{"Cd", 3, {1, 1, 1}}, {"authored", 3, {1, 1, 1}}, {"instance", 1, {2, 0, 0}}});
    require(prepared->external_meshes.at(g).mesh->instance_attributes == AttributeLayout{{"instance", 1}},
            "Authored fields overwritten");
    owner.material_mode = NativeMaterialMode::Uniform;
    const auto h = resolve();
    require(prepared->external_meshes.at(h).mesh->materials == std::vector<std::string>{"fallback"},
            "Uniform fallback changed");
    for (const auto &[key, value] : prepared->external_meshes) {
      require(value.source == source, "Source owner changed");
      require(value.mesh->positions.storage_identity() == source->positions.storage_identity(),
              "Palette duplicated positions");
      require(value.mesh->triangles.storage_identity() == source->triangles.storage_identity(),
              "Palette duplicated topology");
      require(value.mesh->corner_uv.storage_identity() == source->corner_uv.storage_identity(),
              "Palette duplicated UVs");
      require(value.mesh->attributes[0].values.storage_identity() == source->attributes[0].values.storage_identity(),
              "Palette duplicated authored fields");
      if (round)
        require(value.mesh == cache_.at(key).external_render_mesh, "Accepted rendered mesh was rebuilt");
      else
        cache_[key] = {source, value.mesh};
      std::cout << round << " " << key << " revision=" << value.mesh->revision
                << " references=" << value.references.size() << "\n";
    }
    std::cout << "diagnostics=" << diagnostics.size() << " paths=" << material_paths_.size()
              << " styles=" << stylesheet_resources_.size() << " prepared=" << prepared_count << "\n";
  }
  require(next_revision_ == 8, "Warm bindings changed revision");
  require(source->materials == std::vector<std::string>{"/mat/source"}, "Source palette mutated");
  std::cout << "PASS two palettes, material-only refresh, source/storage ownership and warm reuse\n";
}
