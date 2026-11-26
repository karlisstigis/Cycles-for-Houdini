// SPDX-License-Identifier: Apache-2.0
#include "python_status.h"
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include "cycles_houdini/shader.h"
#include <stdexcept>
#include <string>

namespace ch = cycles_houdini;
namespace py = pybind11;
namespace cycles_houdini {
namespace {
std::string resource_object(const PythonStatusExtraction &a, const std::string &resource) {
  const auto found = a.resource_objects.find(resource);
  return found == a.resource_objects.end() ? std::string{} : found->second;
}
} // namespace
py::dict python_status_value(const SessionStatus &s, const PythonStatusExtraction &a) {
  py::dict result;
  result["state"] = py::str(ch::state_name(s.state));
  result["generation"] = s.generation;
  result["message"] = py::str(s.message.c_str());
  result["error"] = py::str(s.error.c_str());
  result["progress"] = s.progress;
  result["elapsed_seconds"] = s.elapsed_seconds;
  result["first_image_seconds"] = s.first_image_seconds;
  result["geometry_conversions"] = s.geometry_conversions;
  result["object_updates"] = s.object_updates;
  result["direct_attribute_updates"] = s.direct_attribute_updates;
  result["expanded_attribute_updates"] = s.expanded_attribute_updates;
  result["attribute_fallback_count"] = s.attribute_fallback_count;
  result["attribute_fallback_reason"] = s.attribute_fallback_reason;
  result["owned_geometry_bytes"] = s.owned_geometry_bytes;
  result["device_allocation_bytes"] = s.device_allocation_bytes;
  result["device_allocation_peak_bytes"] = s.device_allocation_peak_bytes;
  result["cooking_seconds"] = a.cooking_seconds;
  result["extraction_seconds"] = a.conversion_seconds;
  result["extractions"] = a.extractions;
  result["cache_hits"] = a.cache_hits;
  result["geometry_resources"] = s.geometry_resources;
  result["object_resources"] = s.object_resources;
  result["material_resources"] = s.material_resources;
  result["light_resources"] = s.light_resources;
  result["packed_instances"] = a.packed_instances;
  result["index_instance_ids"] = a.index_instance_ids;
  result["engine_shader_resources"] = s.engine_shader_resources;
  result["retired_shader_resources"] = s.retired_shader_resources;
  result["shader_reuses"] = s.shader_reuses;
  result["volume_ray_marching"] = s.volume_ray_marching;
  result["mesh_variant_resources"] = s.mesh_variant_resources;
  result["mesh_variant_bytes"] = s.mesh_variant_bytes;
  result["retained_mesh_source_bytes"] = s.retained_mesh_source_bytes;
  result["displacement_rebuilds"] = s.displacement_rebuilds;
  result["mesh_refits"] = s.mesh_refits;
  result["subdivision_control_faces"] = s.subdivision_control_faces;
  result["subdivision_vertices"] = s.subdivision_vertices;
  result["subdivision_triangles"] = s.subdivision_triangles;
  result["subdivision_tessellations"] = s.subdivision_tessellations;
  result["subdivision_topology_cache_hits"] = s.subdivision_topology_cache_hits;
  result["subdivision_topology_cache_misses"] = s.subdivision_topology_cache_misses;
  result["subdivision_topology_cache_admissions"] = s.subdivision_topology_cache_admissions;
  result["subdivision_topology_cache_evictions"] = s.subdivision_topology_cache_evictions;
  result["subdivision_topology_cache_oversized"] = s.subdivision_topology_cache_oversized;
  result["subdivision_topology_cache_entries"] = s.subdivision_topology_cache_entries;
  result["subdivision_topology_cache_bytes"] = s.subdivision_topology_cache_bytes;
  result["subdivision_topology_cache_budget_bytes"] = s.subdivision_topology_cache_budget_bytes;
  result["subdivision_topology_cache_active_retired_bytes"] = s.subdivision_topology_cache_active_retired_bytes;
  result["image_iris_preparations"] = s.image_iris_preparations;
  result["image_iris_table_bytes"] = s.image_iris_table_bytes;
  result["mesh_family_resources"] = s.mesh_family_resources;
  result["mesh_family_members"] = s.mesh_family_members;
  result["mesh_variant_bindings"] = s.mesh_variant_bindings;
  result["mesh_metric_records"] = s.mesh_metric_records;
  result["dielectric_priority_objects"] = s.dielectric_priority_objects;
  result["nested_probe_unresolved"] = s.nested_probe_unresolved;
  result["color_epoch"] = s.color_epoch;
  result["color_target_epoch"] = s.color_target_epoch;
  result["unchanged_reuses"] = s.unchanged_reuses;
  result["volume_vector_merges"] = s.volume_vector_merges;
  result["volume_vector_fields"] = s.volume_vector_fields;
  result["volume_vector_source_bytes"] = s.volume_vector_source_bytes;
  result["image_iris_preparation_seconds"] = s.image_iris_preparation_seconds;
  result["engine_nested_dielectrics"] = s.engine_nested_dielectrics;
  result["nested_stack_depth_clamped"] = s.nested_stack_depth_clamped;
  result["color_update_pending"] = s.color_update_pending;
  result["dielectric_priority_min"] = s.dielectric_priority_min;
  result["dielectric_priority_max"] = s.dielectric_priority_max;
  result["nested_stack_depth"] = s.nested_stack_depth;
  result["current_sample"] = s.current_sample;
  try {
    result["denoiser"] = py::str(s.denoiser.c_str());
    result["denoise_device_id"] = py::str(s.denoise_device_id.c_str());
  } catch (const py::error_already_set &) {
    throw std::runtime_error("Could not create denoising metadata");
  }
  result["render_device_ids"] = s.render_device_ids;
  result["material_motion_cooks"] = a.material_motion_cooks;
  result["material_motion_samples"] = a.material_motion_samples;
  result["material_motion_cache_hits"] = a.material_motion_cache_hits;
  result["motion_warnings"] = a.motion_warnings;
  py::list diagnostics;
  for (const auto &issue : s.source_diagnostics) {
    py::dict entry;
    entry["code"] = py::str(issue.code.c_str());
    entry["source"] = py::str(issue.source.c_str());
    entry["resource"] = py::str(issue.resource.c_str());
    entry["message"] = py::str(issue.code == "material.mesh_volume_attribute"
                                   ? (resource_object(a, issue.resource) + issue.message).c_str()
                                   : issue.message.c_str());
    entry["action"] = py::str(issue.action.c_str());
    entry["severity"] = py::str(issue.severity.c_str());
    entry["attribute"] = py::str(issue.attribute.c_str());
    diagnostics.append(entry);
  }
  result["source_diagnostics"] = std::move(diagnostics);
  return result;
}
py::dict python_capabilities_value(const Capabilities &caps) {
  py::dict result;
  result["engine_commit"] = py::str(caps.engine_commit.c_str());
  result["meshes"] = caps.meshes;
  result["point_spheres"] = caps.point_spheres;
  result["gaussian_splats"] = caps.gaussian_splats;
  result["linear_round_curves"] = caps.linear_round_curves;
  result["volumes"] = caps.volumes;
  result["osl"] = caps.osl;
  result["image_iris"] = caps.image_iris;
  result["bdpt_prototype"] = caps.bdpt_prototype;
  result["path_guiding"] = caps.path_guiding;

  result["surface_graph_definitions"] = ch::surface_graph_definitions();
  result["volume_graph_definitions"] = ch::volume_graph_definitions();
  result["world_graph_definitions"] = ch::world_graph_definitions();
  result["light_graph_definitions"] = ch::light_graph_definitions();

  for (const auto *name : {"native_ipr",
                           "development_build",
                           "native_scene_definitions",
                           "native_world_materials",
                           "native_material_vops",
                           "native_transparent_film",
                           "native_transparent_glass",
                           "native_surface_holdout",
                           "native_linear_subdivision",
                           "native_catmull_clark_subdivision",
                           "native_subdivision_creases",
                           "native_subdivision_polygon_soups",
                           "material_subnetworks",
                           "native_object_materials",
                           "native_primitive_materials",
                           "native_packed_materials",
                           "native_point_materials",
                           "native_curve_materials",
                           "native_material_overrides",
                           "native_material_stylesheets",
                           "graph_volume_output",
                           "graph_displacement_output",
                           "graph_combined_outputs",
                           "closed_mesh_volumes",
                           "native_transform_motion",
                           "native_velocity_motion",
                           "native_deformation_motion",
                           "native_volume_velocity_motion",
                           "native_packed_deformation",
                           "native_packed_velocity",
                           "native_camera_source_recovery",
                           "native_animated_camera_intrinsics",
                           "native_perspective_zoom_motion",
                           "native_panoramic_cameras",
                           "native_panoramic_motion_vectors",
                           "native_panoramic_depth_of_field",
                           "native_obj_scene_selection",
                           "native_obj_ray_visibility",
                           "native_numeric_lights",
                           "native_denoising",
                           "native_denoising_guides",
                           "native_denoising_comparison"}) {
    result[name] = true;
  }
  for (const auto *name : {"particle_lifetime_clipping", "particle_lifetime_motion", "native_moving_lights",
                           "native_velocity_authored_mesh_normals"}) {
    result[name] = false;
  }
  result["native_stylesheet_categories"] =
      std::vector<std::string>{"material", "materialParameters", "surfaceParameters", "displaceParameters"};
  result["material_ramp_bases"] =
      std::vector<std::string>{"constant", "linear", "catmull_rom", "monotone_cubic", "bezier", "bspline", "hermite"};
  result["material_ramp_max_keys"] = 32;
  result["native_houdini_light_types"] =
      std::vector<std::string>{"point", "grid", "disk", "sphere", "distant", "sun", "envlight"};
  result["volume_motion_integrator"] = py::str("ray_marching");
  return result;
}
} // namespace cycles_houdini
