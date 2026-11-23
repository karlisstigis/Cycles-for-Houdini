// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
#include "scene/shader_graph.h"
#include "texture_assets.h"
#include "ies_assets.h"
#include <map>
#include <set>
namespace cycles_houdini {
// Known source-context problems in an otherwise well-formed world graph.
// Ownership, socket/schema errors and other internal contracts stay strict.
class WorldSourceError : public std::invalid_argument {
public:
  using std::invalid_argument::invalid_argument;
};
class LightSourceError : public std::invalid_argument {
public:
  using std::invalid_argument::invalid_argument;
};
enum class ShaderContext { Surface, World, Light };
enum class ShaderGeometry { Any, Mesh, Strands };
ShaderGraphData diagnostic_surface_graph();
struct ShaderGraphRequirements {
  std::vector<SourceDiagnostic> source_diagnostics;
  bool surface{}, volume{}, displacement{};
  bool volume_diagnostic{}; // Recover sparse bindings using retained density, not a constant medium.
  // The surface is the camera-only diagnostic marker. Its emission is never
  // light: the engine must not build mesh-light emitters for these triangles.
  bool diagnostic_surface{};
  bool displacement_transform_dependent{}, displacement_identity_dependent{};
  bool displacement_camera_transform_dependent{};
  bool displacement_camera_projection_dependent{};
  bool displacement_deformation_dependent{}, displacement_requires_uv{};
  bool displacement_requires_color{};
  bool displacement_requires_pointiness{};
  bool displacement_requires_object_index{}, displacement_requires_material_index{};
  bool displacement_motion_supported{true};
  bool requires_uv{}, requires_color{}, requires_mesh_tangents{};
  unsigned tangent_bases{}; // 1: original/undisplaced; 2: displaced.
  std::map<std::string, unsigned> named_tangent_bases;
  bool requires_generated{}, requires_mesh_islands{}, requires_mesh_pointiness{};
  bool retain_generated{}; // Authored dependency can outlive a temporary texture fallback.
  std::set<std::string> named_attributes;
  std::set<std::string> displacement_named_attributes;
  std::vector<TextureAsset> textures;
  std::vector<IesAsset> ies;
  std::set<std::string> volume_fields;
  std::set<std::string> scalar_volume_fields, vector_volume_fields;
};
// Validate an owned graph and its output domain before touching the live scene.
// Field requirements follow reachable inputs, including constructed defaults.
ccl::unique_ptr<ccl::ShaderGraph>
prepare_shader_graph(const ShaderGraphData &data, ShaderGraphRequirements &requirements,
                     const TextureInputPolicy &policy, const std::vector<AovDefinition> &aovs = {},
                     ShaderContext context = ShaderContext::Surface, bool has_uv = true, bool has_color = true,
                     ShaderGeometry geometry = ShaderGeometry::Any, const AttributeLayout *attributes = nullptr,
                     const AttributeLayout *instance_attributes = nullptr);
} // namespace cycles_houdini
