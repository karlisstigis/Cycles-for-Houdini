// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
#include "cycles_houdini/transform.h"
#include "cycles_houdini/volume.h"
#include "cycles_houdini/image_iris.h"
#include "pass_catalogue.h"
#include "shader_graph.h"
#include "linking.h"
#include "texture_memory.h"
#include "named_attributes.h"
#include "instance_motion.h"
#include "subdivision_mesh.h"
#include "coarse_subdivision_mesh.h"
#include "generated_coordinates.h"
#include "mesh_islands.h"
#include "mesh_pointiness.h"
#include "mesh_shading.h"
#include "mesh_sharing.h"
#include "color_internal.h"
#include "display_image.h"
#include "dicing_policy.h"
#include "denoising.h"
#include "camera_motion.h"
#include "render_settings.h"
#include "render_devices.h"
#include "integrator/denoiser.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <bit>
#include <cmath>
#include <cstdlib>
#include <cstdio>
#include <filesystem>
#include <locale>
#include <map>
#include <limits>
#include <mutex>
#include <numbers>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string_view>
#include <unordered_set>
#include <unordered_map>

#include "device/device.h"
#include "scene/attribute.h"
#include "scene/background.h"
#include "scene/camera.h"
#include "util/colorspace.h"
#include "scene/film.h"
#include "scene/integrator.h"
#include "scene/light.h"
#include "scene/mesh.h"
#include "scene/pointcloud.h"
#include "scene/hair.h"
#include "scene/volume.h"
#include "scene/image.h"
#include "scene/image_vdb.h"
#include "scene/object.h"
#include "scene/pass.h"
#include "scene/scene.h"
#include "scene/stats.h"
#include "scene/shader.h"
#include "scene/shader_graph.h"
#include "scene/shader_nodes.h"
#include "session/buffers.h"
#include "session/display_driver.h"
#include "session/output_driver.h"
#include "session/session.h"
#include "util/atomic.h"
#include "util/log.h"
#include "util/path.h"
#include "util/hash.h"
#include <cstdio>
#include <tuple>

namespace cycles_houdini::session_detail {
using Clock = std::chrono::steady_clock;
inline void bind_object_geometry(ccl::Object *object, ccl::Geometry *geometry, ccl::Scene *scene) {
  if (object->get_geometry() == geometry)
    return;
  object->set_geometry(geometry);
  // Existing prototypes can exchange owners without changing transforms,
  // visibility or geometry buffers. Cycles' Object::tag_update does not
  // classify its geometry socket as a scene-BVH change, so explicitly
  // refresh the instance acceleration structure and retain unchanged BLASes.
  scene->geometry_manager->tag_update(scene, ccl::GeometryManager::TRANSFORM_MODIFIED);
}
struct BindingKey {
  std::string first;
  unsigned second{};
  AttributeLayout attributes, instance_attributes;
  auto operator<=>(const BindingKey &) const = default;
};
constexpr unsigned volume_fallback_binding = 4; // UV/color use bits 0 and 1.
constexpr unsigned strand_binding = 8;
constexpr std::string_view temporal_material_prefix = "internal:displacement-sample/";
inline bool temporal_material(const std::string &key) { return key.starts_with(temporal_material_prefix); }
inline std::string temporal_material_key(const std::string &key, std::size_t sample) {
  return std::string(temporal_material_prefix) + std::to_string(key.size()) + ":" + key + "/" + std::to_string(sample);
}
inline BindingKey binding_key(const std::string &key, bool has_uv, bool has_color, bool strands = false,
                              const AttributeLayout &attributes = {}, const AttributeLayout &instances = {}) {
  return {key, (!has_uv ? 1u : 0u) | (!has_color ? 2u : 0u) | (strands ? strand_binding : 0u), attributes, instances};
}
// Camera source diagnostics travel with ownership, but do not alter engine
// optics. Keep this tuple aligned with CameraData's physical render fields.
inline auto camera_render_state(const CameraData &c) {
  return std::tie(c.transform, c.short_axis_fov, c.near_clip, c.far_clip, c.orthographic, c.ortho_scale,
                  c.aperture_size, c.focus_distance, c.aperture_rotation, c.aperture_ratio, c.aperture_blades,
                  c.image_iris, c.image_iris_method, c.image_iris_color, c.dof_motion, c.intrinsic_motion,
                  c.shutter_frames, c.motion, c.pixel_aspect, c.sampling, c.panorama);
}
// Window shader coordinates consume XY projection, not depth clipping, DOF or
// shutter controls. Raster dimensions are immutable for one Session; a viewer
// resolution change creates a new session and evaluates every mesh afresh.
inline auto camera_coordinate_projection(const CameraData &c) {
  return std::tuple(c.orthographic, c.panorama,
                    c.panorama       ? 0.f
                    : c.orthographic ? c.ortho_scale
                                     : c.short_axis_fov,
                    c.pixel_aspect);
}
inline double seconds(Clock::time_point since) { return std::chrono::duration<double>(Clock::now() - since).count(); }
// Opt-in wall-time attribution for owned update processing. The persistent
// boundary callback completes before apply returns, so this stack-owned trace
// is never read concurrently. No paths, resources or transport fields are logged.
struct UpdateProfile {
  struct Stage {
    const char *name{};
    double seconds{};
  };
  bool enabled{}, accepted{};
  std::uint64_t generation{};
  std::size_t records{}, poses{}, attributes{}, attribute_poses{}, count{};
  std::array<Stage, 32> stages{};
  Clock::time_point started{}, last{};
  explicit UpdateProfile(const SceneUpdate &update)
      : records(update.objects.size()), poses(update.object_poses.size()), attributes(update.object_attributes.size()) {
    static const bool active = []() {
      const char *value = std::getenv("CYCLES_HOUDINI_PROFILE_UPDATES");
      return value && std::string_view(value) == "1";
    }();
    enabled = active;
    if (enabled)
      attribute_poses = std::count_if(update.object_attributes.begin(), update.object_attributes.end(),
                                      [](const auto &value) { return value.pose.has_value(); });
    if (enabled)
      started = last = Clock::now();
  }
  void mark(const char *name) {
    if (!enabled)
      return;
    const auto now = Clock::now();
    if (count < stages.size())
      stages[count++] = {name, std::chrono::duration<double>(now - last).count()};
    last = now;
  }
  void accept(std::uint64_t value) {
    accepted = true;
    generation = value;
  }
  ~UpdateProfile() noexcept {
    if (!enabled)
      return;
    try {
      mark("return");
      const double total = seconds(started);
      std::ostringstream out;
      out.imbue(std::locale::classic());
      out << "CH_UPDATE_PROFILE {\"success\":" << (accepted ? "true" : "false") << ",\"generation\":" << generation
          << ",\"object_records\":" << records << ",\"object_poses\":" << poses
          << ",\"object_attributes\":" << attributes << ",\"object_attribute_poses\":" << attribute_poses
          << ",\"total_seconds\":" << total << ",\"stages\":{";
      for (std::size_t n = 0; n < count; ++n) {
        if (n)
          out << ',';
        out << '\"' << stages[n].name << "\":" << stages[n].seconds;
      }
      out << "}}";
      std::fprintf(stderr, "%s\n", out.str().c_str());
    } catch (...) { /* Diagnostic collection must not change scene recovery. */
    }
  }
};
// Reasons only: no source paths or shader contents enter this opt-in trace.
// CYCLES_HOUDINI_POSE_FAST=0: pose-only updates take the general geometry path (comparison switch).
inline bool pose_fast_rule() {
  static const bool enabled = [] {
    const char *value = std::getenv("CYCLES_HOUDINI_POSE_FAST");
    return !(value && std::string_view(value) == "0");
  }();
  return enabled;
}
inline void mesh_update_profile(std::uint64_t generation, const char *reason, bool first = false, bool second = false) {
  static const bool enabled = []() {
    const char *value = std::getenv("CYCLES_HOUDINI_PROFILE_GEOMETRY");
    return value && std::string_view(value) == "1";
  }();
  if (enabled)
    std::fprintf(stderr, "CH_MESH_PROFILE generation=%llu reason=%s first=%d second=%d\n",
                 static_cast<unsigned long long>(generation), reason, int(first), int(second));
}
inline ccl::float3 vec(Float3 p) { return ccl::make_float3(p.x, p.y, p.z); }
inline ccl::Transform transform(const Matrix &m) {
  return {ccl::make_float4(m[0], m[1], m[2], m[3]), ccl::make_float4(m[4], m[5], m[6], m[7]),
          ccl::make_float4(m[8], m[9], m[10], m[11])};
}
inline void configure_camera_projection(ccl::Camera *camera, const CameraData &c, const SessionConfig &config) {
  camera->set_matrix(transform(camera_engine_transform(c, c.transform)));
  camera->set_camera_type(c.panorama       ? ccl::CAMERA_PANORAMA
                          : c.orthographic ? ccl::CAMERA_ORTHOGRAPHIC
                                           : ccl::CAMERA_PERSPECTIVE);
  if (c.panorama) {
    const auto p = canonical_panorama(*c.panorama);
    static const std::map<std::string, ccl::PanoramaType> types = {
        {"equirectangular", ccl::PANORAMA_EQUIRECTANGULAR},
        {"equiangular_cubemap_face", ccl::PANORAMA_EQUIANGULAR_CUBEMAP_FACE},
        {"mirrorball", ccl::PANORAMA_MIRRORBALL},
        {"fisheye_equidistant", ccl::PANORAMA_FISHEYE_EQUIDISTANT},
        {"fisheye_equisolid", ccl::PANORAMA_FISHEYE_EQUISOLID},
        {"fisheye_lens_polynomial", ccl::PANORAMA_FISHEYE_LENS_POLYNOMIAL},
        {"central_cylindrical", ccl::PANORAMA_CENTRAL_CYLINDRICAL}};
    camera->set_panorama_type(types.at(p.type));
    camera->set_fisheye_fov(p.fisheye_fov);
    camera->set_fisheye_lens(p.fisheye_lens);
    camera->set_latitude_min(p.latitude_min);
    camera->set_latitude_max(p.latitude_max);
    camera->set_longitude_min(p.longitude_min);
    camera->set_longitude_max(p.longitude_max);
    camera->set_fisheye_polynomial_k0(p.polynomial[0]);
    camera->set_fisheye_polynomial_k1(p.polynomial[1]);
    camera->set_fisheye_polynomial_k2(p.polynomial[2]);
    camera->set_fisheye_polynomial_k3(p.polynomial[3]);
    camera->set_fisheye_polynomial_k4(p.polynomial[4]);
    camera->set_central_cylindrical_range_u_min(p.cylindrical_u_min);
    camera->set_central_cylindrical_range_u_max(p.cylindrical_u_max);
    camera->set_central_cylindrical_range_v_min(p.cylindrical_v_min);
    camera->set_central_cylindrical_range_v_max(p.cylindrical_v_max);
    if (p.type == "fisheye_equisolid" || p.type == "fisheye_lens_polynomial") {
      const auto [width, height] = panorama_sensor(p, config.width, config.height, c.pixel_aspect);
      camera->set_sensorwidth(width);
      camera->set_sensorheight(height);
    }
  }
  const float raster_aspect = float(config.width) / config.height;
  const float aspect = raster_aspect * c.pixel_aspect;
  const auto camera_fov = [&](float value) {
    return float(2 * std::atan(std::tan(double(value) / 2) * std::max(1.0, double(raster_aspect)) /
                               std::max(1.0, double(aspect))));
  };
  ccl::array<float> intrinsic_motion;
  intrinsic_motion.resize(c.intrinsic_motion.size() * 24);
  for (std::size_t step = 0; step < c.intrinsic_motion.size(); ++step) {
    const auto &sample = c.intrinsic_motion[step];
    auto *packed = intrinsic_motion.data() + step * 24;
    packed[0] = sample.near_clip;
    packed[1] = sample.far_clip;
    packed[2] = std::tan(camera_fov(sample.short_axis_fov) * .5f);
    packed[3] = sample.ortho_scale / c.pixel_aspect;
    const auto p = canonical_panorama(sample.panorama.value_or(PanoramaSettings{}));
    const auto sensor = panorama_sensor(p, config.width, config.height, c.pixel_aspect);
    packed[4] = p.fisheye_fov;
    packed[5] = p.fisheye_lens;
    packed[6] = sensor.first;
    packed[7] = sensor.second;
    packed[8] = p.longitude_min - p.longitude_max;
    packed[9] = -p.longitude_min;
    packed[10] = p.latitude_min - p.latitude_max;
    packed[11] = -p.latitude_min + std::numbers::pi_v<float> * .5f;
    packed[12] = -p.cylindrical_u_min;
    packed[13] = -p.cylindrical_u_max;
    packed[14] = p.cylindrical_v_min;
    packed[15] = p.cylindrical_v_max;
    for (int n = 0; n < 4; ++n)
      packed[16 + n] = p.polynomial[n + 1];
    packed[20] = p.polynomial[0];
    packed[21] = packed[22] = packed[23] = 0;
  }
  camera->set_intrinsic_motion(intrinsic_motion);
  camera->set_fov(camera_fov(c.short_axis_fov));
  camera->set_use_perspective_motion(false);
  camera->set_fov_pre(camera_fov(c.short_axis_fov));
  camera->set_fov_post(camera_fov(c.short_axis_fov));
  camera->set_nearclip(c.near_clip);
  camera->set_farclip(c.far_clip);
  camera->set_full_width(config.width);
  camera->set_full_height(config.height);
  ccl::BoundBox2D viewplane(ccl::BoundBox2D::full);
  if (c.orthographic) {
    const float half = float(double(c.ortho_scale) * config.width / config.height / 2);
    viewplane = ccl::BoundBox2D(ccl::make_float2(half, c.ortho_scale / c.pixel_aspect / 2));
  } else if (!c.panorama) {
    viewplane = ccl::BoundBox2D(ccl::make_float2(std::max(1.f, aspect), 1 / std::max(1.f, 1 / aspect)));
  }
  // Set the final bounds directly so viewplane-only edits dirty their sockets.
  camera->set_viewplane_left(viewplane.left);
  camera->set_viewplane_right(viewplane.right);
  camera->set_viewplane_bottom(viewplane.bottom);
  camera->set_viewplane_top(viewplane.top);
}
inline bool finite(Float3 p) { return std::isfinite(p.x) && std::isfinite(p.y) && std::isfinite(p.z); }
inline std::size_t shading_motion_bytes(const MeshData &mesh) {
  std::size_t bytes = 0;
  for (const auto &step : mesh.motion_corner_uv)
    bytes += step.size() * sizeof(Float2);
  for (const auto &step : mesh.motion_corner_colors)
    bytes += step.size() * sizeof(Float3);
  return bytes;
}
inline bool same_material_shading(const MaterialData &a, const MaterialData &b) {
  const auto values = [](const MaterialData &m) {
    return std::tie(m.kind, m.color, m.roughness, m.metallic, m.emission_strength, m.density, m.anisotropy,
                    m.volume_emission_strength, m.volume_emission_color, m.emission_field, m.blackbody_intensity,
                    m.temperature_kelvin_scale, m.color_attribute, m.graph, m.displacement_samples);
  };
  return values(a) == values(b);
}
// Compare every owned geometry field, excluding resource identity, revision and
// shader names. Slot layout still participates: equal positions alone do not
// establish compatible render geometry. Keep this in sync with MeshData.
inline bool same_mesh_payload(const MeshData &a, const MeshData &b) {
  const auto values = [](const MeshData &m) {
    return std::tie(m.positions, m.triangles, m.corner_normals, m.corner_uv, m.corner_colors, m.triangle_material,
                    m.smooth, m.motion, m.motion_corner_normals, m.motion_corner_uv, m.motion_corner_colors,
                    m.motion_uv_complete, m.motion_color_complete, m.subdivision, m.nominal_pose, m.motion_blur,
                    m.attributes, m.instance_attributes, m.pointiness, m.texture_space, m.blender_edges,
                    m.blender_vertex_normals);
  };
  return a.materials.size() == b.materials.size() && mesh_payload_equal(values(a), values(b));
}
template <class Data> inline std::vector<std::string> strand_materials(const Data &data) {
  return data.materials.empty() ? std::vector<std::string>{data.material} : data.materials;
}
inline double determinant(const Matrix &m) { return transform_determinant(m); }
inline void validate_matrix(const Matrix &m) {
  for (float v : m)
    if (!std::isfinite(v))
      throw std::invalid_argument("Non-finite transform");
  if (determinant(m) == 0)
    throw std::invalid_argument("Singular transform");
}
inline void validate_transform_motion(const std::vector<Matrix> &samples, const Matrix &center) {
  if (samples.empty())
    return;
  if (samples.size() < 3 || samples.size() > ccl::Object::MAX_MOTION_STEPS || samples.size() % 2 == 0)
    throw std::invalid_argument("Transform motion requires an odd count from 3 to 129, including the centre");
  const auto &middle = samples[samples.size() / 2];
  for (std::size_t i = 0; i < middle.size(); ++i)
    if (std::abs(middle[i] - center[i]) > 1e-5f)
      throw std::invalid_argument("Transform motion centre differs from the base transform");
  const bool negative = determinant(center) < 0;
  for (const auto &sample : samples) {
    validate_matrix(sample);
    if ((determinant(sample) < 0) != negative)
      throw std::invalid_argument("Transform motion cannot change handedness within the shutter");
  }
}
inline void validate_camera_transform_motion(const std::vector<Matrix> &samples, const Matrix &requested) {
  if (samples.empty())
    return;
  if (samples.size() < 3 || samples.size() > ccl::Object::MAX_MOTION_STEPS || samples.size() % 2 == 0)
    throw std::invalid_argument("Camera transform motion requires an odd count from 3 to 129 complete samples");
  // Camera::update decomposes the complete chronological array and camera
  // rays interpolate it directly. Unlike object/geometry centre storage, a
  // shifted shutter never aliases the requested pose into its midpoint.
  const bool negative = determinant(requested) < 0;
  for (const auto &sample : samples) {
    validate_matrix(sample);
    if ((determinant(sample) < 0) != negative)
      throw std::invalid_argument("Camera transform motion cannot change handedness within the shutter");
  }
}
inline ccl::array<ccl::Transform> transform_motion(const std::vector<Matrix> &matrices) {
  ccl::array<ccl::Transform> result;
  result.reserve(matrices.size());
  for (const auto &matrix : matrices)
    result.push_back_reserved(transform(matrix));
  return result;
}
inline void validate_geometry_motion(const GeometryMotion &motion, std::size_t elements, bool radii) {
  if (!motion.positions.empty() &&
      (motion.positions.size() % 2 || motion.positions.size() + 1 > ccl::Geometry::MAX_MOTION_STEPS))
    throw std::invalid_argument("Geometry motion requires 2 to 128 other samples, with the centre stored separately");
  for (const auto &sample : motion.positions) {
    if (sample.size() != elements)
      throw std::invalid_argument("Deformation motion requires stable topology/correspondence");
    for (const auto &p : sample)
      if (!finite(p))
        throw std::invalid_argument("Non-finite motion position");
  }
  if (!motion.radii.empty() && (!radii || motion.radii.size() != motion.positions.size()))
    throw std::invalid_argument("Radius motion must match point/curve position samples");
  for (const auto &sample : motion.radii) {
    if (sample.size() != elements)
      throw std::invalid_argument("Radius motion requires stable point/curve correspondence");
    for (float value : sample)
      if (!std::isfinite(value) || value < 0)
        throw std::invalid_argument("Motion radius must be finite and nonnegative");
  }
}
inline std::size_t geometry_motion_bytes(const GeometryMotion &motion) {
  std::size_t result = 0;
  for (const auto &values : motion.positions)
    result += values.size() * sizeof(ccl::packed_float3);
  for (const auto &values : motion.radii)
    result += values.size() * sizeof(float);
  return result;
}
inline void update_geometry_motion(ccl::Geometry *geometry, const GeometryMotion &motion, bool enabled) {
  geometry->set_motion_steps(motion.positions.empty() ? 0 : unsigned(motion.positions.size() + 1));
  geometry->set_use_motion_blur(enabled && !motion.positions.empty());
  if (motion.positions.empty())
    return;
  auto *positions = geometry->attributes.find(ccl::ATTR_STD_POSITION);
  positions->add_motion(geometry);
  for (std::size_t step = 0; step < motion.positions.size(); ++step) {
    auto *target = positions->data_for_write<ccl::packed_float3>(int(step + 1));
    for (const auto &value : motion.positions[step])
      *target++ = ccl::packed_float3(vec(value));
  }
  if (!motion.radii.empty()) {
    auto *radius = geometry->attributes.find(ccl::ATTR_STD_RADIUS);
    radius->add_motion(geometry);
    for (std::size_t step = 0; step < motion.radii.size(); ++step)
      std::copy(motion.radii[step].begin(), motion.radii[step].end(), radius->data_for_write<float>(int(step + 1)));
  }
}

} // namespace cycles_houdini::session_detail
