// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "motion_fixture.h"
#include "../core/camera_motion.h"
#include "cycles_houdini/snapshot.h"
#include <cmath>
#include <fstream>
#include <limits>

namespace cycles_houdini::harness {
struct CameraLensSpec {
  const char *name;
  int mode{};
  float shift{}, focus{2.5f};
};
inline void camera_lens_reference(const std::string &device, const std::string &output) {
  int contracts = 0;
  const auto require = [&](bool valid, const char *message) {
    motion_require(valid, message);
    ++contracts;
  };
  CameraSourceSample nominal;
  nominal.value.short_axis_fov = 2 * std::atan(.4f);
  nominal.value.far_clip = 1000;
  std::vector<CameraSourceSample> inputs(3, nominal);
  inputs.front().value.short_axis_fov = 2 * std::atan(.3f);
  inputs.back().value.short_axis_fov = 2 * std::atan(.5f);
  inputs.front().value.transform[3] = -.1f;
  inputs.back().value.transform[3] = .1f;
  auto resolved = resolve_camera_source(nominal, inputs, "/obj/independent_camera", .5f);
  require(!resolved.intrinsic_motion.empty() && resolved.motion.size() == 3 && resolved.source_diagnostics.empty(),
          "Camera source lost supported zoom/transform motion");
  WorkerUpdate packet;
  packet.scene.camera = resolved;
  auto decoded = decode_worker_update(encode_worker_update(packet));
  require(decoded.scene.camera == packet.scene.camera, "Perspective camera data did not round-trip");
  const auto recovered = [&](const char *code) {
    auto result = resolve_camera_source(nominal, inputs, "/obj/independent_camera", .5f);
    require(result.source_diagnostics.size() == 1 && result.source_diagnostics.front().code == code,
            "Wrong camera source recovery domain");
    WorkerUpdate p;
    p.scene.camera = result;
    require(decode_worker_update(encode_worker_update(p)).scene.camera == p.scene.camera,
            "Camera warnings did not round-trip");
    return result;
  };
  inputs.front().value.near_clip = -1;
  auto clipped = recovered("camera.shutter_clipping");
  require(clipped.near_clip == nominal.value.near_clip && clipped.motion.size() == 3 &&
              !clipped.intrinsic_motion.empty(),
          "Clip recovery removed independent camera motion");
  inputs.front().value.near_clip = nominal.value.near_clip;
  inputs.front().projection = 2;
  auto projection = recovered("camera.shutter_projection");
  require(projection.short_axis_fov == nominal.value.short_axis_fov && projection.intrinsic_motion.empty() &&
              projection.motion.size() == 3,
          "Projection recovery lost valid transform motion");
  inputs.front().projection = 0;
  inputs.front().value.transform[0] = 0;
  auto transform = recovered("camera.shutter_transform");
  require(transform.transform == nominal.value.transform && transform.motion.empty() &&
              !transform.intrinsic_motion.empty(),
          "Transform recovery removed valid lens motion");
  inputs.front().value.transform[0] = 1;
  inputs.front().value.short_axis_fov = std::numeric_limits<float>::quiet_NaN();
  auto focal = recovered("camera.shutter_projection");
  require(focal.short_axis_fov == nominal.value.short_axis_fov && focal.motion.size() == 3,
          "Invalid focal value removed usable camera motion");
  inputs.assign(3, nominal);
  nominal.projection = 1;
  for (auto &value : inputs)
    value.projection = 1;
  nominal.value.short_axis_fov = std::numeric_limits<float>::quiet_NaN();
  for (auto &value : inputs)
    value.value.short_axis_fov = nominal.value.short_axis_fov;
  resolved = resolve_camera_source(nominal, inputs, "/obj/independent_camera", .5f);
  require(resolved.source_diagnostics.empty() && std::isfinite(resolved.short_axis_fov),
          "Unused orthographic focal data was not ignored");
  inputs.front().value.ortho_scale = -1;
  auto ortho = recovered("camera.shutter_orthographic_scale");
  require(ortho.ortho_scale == nominal.value.ortho_scale, "Orthographic recovery did not use requested frame");
  auto invalid = nominal;
  invalid.value.transform[0] = 0;
  bool rejected = false;
  try {
    resolve_camera_source(invalid, {}, "/obj/invalid_camera", .5f);
  } catch (const std::invalid_argument &) {
    rejected = true;
  }
  require(rejected, "Invalid essential requested camera was silently replaced");

  std::filesystem::create_directories(output);
  SessionConfig config;
  config.device_id = device;
  config.width = 160;
  config.height = 96;
  config.samples = 1024;
  config.interactive = false;
  RenderSession session(config);
  std::uint64_t revision = 0;
  int images = 0;
  // Expected images use no perspective-motion fields and no source resolver.
  // Independently warp planar geometry into a fixed centre camera. Dense
  // temporal vertices approximate the rational inverse zoom between ray keys.
  const auto render = [&](const CameraLensSpec &s, int steps, float offset, float nominal_time, const std::string &name,
                          bool motion = true) {
    (void)steps; // References always use an independent dense temporal mesh.
    const float duration = .5f / 24, centre = nominal_time + (motion ? offset * duration * .5f : 0.f);
    const bool zoom = s.mode == 2 || s.mode == 5 || s.mode == 6 || s.mode == 9 || s.mode == 14,
               held = s.mode == 4 || s.mode == 5;
    const bool focus_pull = s.mode == 10 || s.mode == 11;
    const float camera_rate = s.mode == 9 ? 0.f : 6.f, geometry_rate = s.mode == 9 ? 0.f : 2.f;
    const auto half = [&](float t) {
      if (s.mode == 14) {
        const float q = std::clamp((t / duration + .5f) * 4, 0.f, 4.f);
        const float wave = q < 1 ? -q : q < 3 ? q - 2 : 4 - q;
        return .4f * (1 + .2f * wave);
      }
      return zoom ? .4f * (1 + 24 * (t - (s.mode == 6 ? nominal_time : 0.f))) : .4f;
    };
    const float center_x = s.shift + camera_rate * (held ? nominal_time : centre), a = half(centre);
    const auto positions = [&](float t) {
      const float relative = geometry_rate * t - (s.shift + camera_rate * (held ? nominal_time : t));
      // An independent constant-focus camera can represent a focus pull
      // by moving and scaling the planar target. Solve both the pinhole
      // and pupil coefficients, rather than using the new optics array.
      const float focus = s.focus + 24 * (t - (s.mode == 11 ? nominal_time : 0.f));
      const float distance = focus_pull ? 1 / (1 / 5.f + 1 / s.focus - 1 / focus) : 5.f;
      const float scale = s.mode == 3 ? 6 / (6 + 24 * t) : a / half(t) * (distance / 5);
      std::vector<Float3> values;
      for (const auto p : std::vector<Float3>{{-.7f, -.5f, 0}, {.7f, -.5f, 0}, {.7f, .5f, 0}, {-.7f, .5f, 0}})
        values.push_back({center_x + (p.x + relative) * scale, p.y * scale, 5 - distance});
      return values;
    };
    SceneUpdate scene;
    MaterialData material;
    material.key = "independent_camera_emission";
    material.kind = MaterialKind::Emission;
    material.color = {.2f, .4f, .8f};
    scene.materials = {material};
    auto mesh = std::make_shared<MeshData>();
    mesh->key = "independent_camera_mesh";
    mesh->revision = ++revision;
    mesh->materials = {material.key};
    mesh->triangles = {{0, 1, 2}, {0, 2, 3}};
    mesh->positions = positions(centre);
    if (motion)
      for (int step = 0; step < 129; ++step)
        if (step != 64)
          mesh->motion.positions.push_back(positions(centre + (float(step) / 128 - .5f) * duration));
    scene.meshes = {mesh};
    ObjectData object;
    object.key = "independent_camera_object";
    object.geometry_key = mesh->key;
    scene.objects = {object};
    if (s.mode == 7 || s.mode == 8 || s.mode == 12 || s.mode == 13) {
      const bool near_witness = s.mode == 7 || s.mode == 12;
      const float size = near_witness ? .01f : .1f, x = near_witness ? 0.f : 1.f, z = near_witness ? 4.925f : -.11f;
      const auto witness = [&](float t) {
        std::vector<Float3> values;
        const float depth = 5 - z;
        const float reference_depth = s.mode == 12 ? depth + 4 * t : s.mode == 13 ? depth - 2 * t : depth;
        const float ratio = reference_depth / depth;
        for (const auto p : std::vector<Float3>{{-size, -size, 0}, {size, -size, 0}, {size, size, 0}, {-size, size, 0}})
          values.push_back({center_x + (x + p.x - s.shift - 6 * t) * ratio, p.y * ratio, 5 - reference_depth});
        return values;
      };
      MaterialData red;
      red.key = "independent_red_witness";
      red.kind = MaterialKind::Emission;
      red.color = {1, 0, 0};
      scene.materials.push_back(red);
      auto fore = std::make_shared<MeshData>();
      fore->key = "independent_witness";
      fore->revision = revision;
      fore->materials = {red.key};
      fore->triangles = {{0, 1, 2}, {0, 2, 3}};
      fore->positions = witness(centre);
      if (motion)
        for (int step = 0; step < 129; ++step)
          if (step != 64)
            fore->motion.positions.push_back(witness(centre + (float(step) / 128 - .5f) * duration));
      scene.meshes.push_back(fore);
      ObjectData extra;
      extra.key = "independent_witness_object";
      extra.geometry_key = fore->key;
      scene.objects.push_back(extra);
    } else {
      scene.deleted_objects = {"independent_witness_object"};
      scene.deleted_geometry = {"independent_witness"};
    }
    CameraData camera;
    camera.transform[3] = center_x;
    camera.short_axis_fov = 2 * std::atan(a);
    camera.shutter_frames = .5f;
    if (s.mode == 1 || focus_pull) {
      camera.aperture_size = .5f;
      camera.focus_distance = s.focus;
    }
    if (s.mode == 3 || s.mode == 15) {
      camera.orthographic = true;
      camera.ortho_scale = 3.6f;
    }
    if (s.mode == 12)
      camera.near_clip = .1f;
    if (s.mode == 13)
      camera.far_clip = 5.1f;
    scene.camera = camera;
    scene.motion_blur = motion;
    scene.background = Float3{};
    RenderSettings settings;
    settings.samples = 1024;
    settings.adaptive_sampling = false;
    settings.frame = nominal_time * 24 + 1;
    scene.rendering = settings;
    motion_wait(session, session.apply(scene));
    session.write_exr((std::filesystem::path(output) / (name + ".exr")).string());
    ++images;
  };
  const CameraLensSpec specs[] = {{"pinhole", 0},
                                  {"dof", 1},
                                  {"zoom", 2},
                                  {"ortho", 3},
                                  {"held_transform", 4},
                                  {"held_transform_zoom", 5},
                                  {"ortho_static", 15},
                                  {"quarter_zoom", 14},
                                  {"animated_near_witness", 12},
                                  {"animated_far_witness", 13},
                                  {"gui_dof_focus", 1, 0, 3.5f},
                                  {"gui_dof_shift", 1, .1f},
                                  {"gui_zoom_shift", 2, .1f},
                                  {"near_witness", 7},
                                  {"far_witness", 8},
                                  {"zoom_only", 9},
                                  {"focus_pull", 10}};
  for (const auto &s : specs)
    for (int steps : {3, 5})
      for (const auto [offset, label] : {std::pair{-.75f, "m"}, std::pair{0.f, "c"}, std::pair{.75f, "p"}})
        render(s, steps, offset, 0, std::string(s.name) + "-" + std::to_string(steps) + "-" + label);
  render(specs[2], 3, 0, 0, "zoom-no-blur", false);
  for (int frame = 1; frame <= 5; ++frame) {
    CameraLensSpec s{"batch", frame == 3 ? 0 : frame == 4 ? 6 : frame == 2 ? 11 : 1};
    render(s, 3, 0, float(frame - 1) / 24, "frame-" + std::to_string(frame));
  }
  SceneUpdate ledger;
  ledger.camera = CameraData{};
  ledger.source_diagnostics = std::vector<SourceDiagnostic>{
      {"test.unrelated_source", "/obj/other", "other", "Unrelated retained source warning", "Keep supported geometry"}};
  motion_wait(session, session.apply(ledger));
  SceneUpdate warning;
  warning.camera = CameraData{};
  warning.camera->source_diagnostics = clipped.source_diagnostics;
  motion_wait(session, session.apply(warning));
  require(session.status().source_diagnostics.size() == 2, "Camera warning replaced unrelated diagnostics");
  SceneUpdate repair;
  repair.camera = CameraData{};
  motion_wait(session, session.apply(repair));
  require(session.status().source_diagnostics.size() == 1 &&
              session.status().source_diagnostics.front().code == "test.unrelated_source",
          "Camera repair did not preserve unrelated diagnostics");
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(3);
  while (session.status().state != SessionState::Complete && std::chrono::steady_clock::now() < deadline)
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  require(session.status().state == SessionState::Complete, "Reference did not reach terminal completion");
  const auto generation = session.status().generation;
  for (int kind = 0; kind < 4; ++kind) {
    SceneUpdate bad;
    CameraData value;
    value.intrinsic_motion = {{.8f}, {.85f}, {.9f}};
    if (kind == 0)
      value.intrinsic_motion[0].short_axis_fov = std::numeric_limits<float>::quiet_NaN();
    if (kind == 1)
      value.intrinsic_motion[0].panorama = PanoramaSettings{};
    if (kind == 2)
      value.intrinsic_motion[2].short_axis_fov = -1;
    if (kind == 3)
      value.focus_distance = -1;
    bad.camera = value;
    bool strict = false;
    try {
      session.apply(bad);
    } catch (const std::invalid_argument &) {
      strict = true;
    }
    require(strict && session.status().generation == generation && session.status().state == SessionState::Complete,
            "Malformed engine camera data changed the accepted scene");
  }
  session.close();
  require(session.status().owned_geometry_bytes == 0, "Reference camera retained geometry");
  std::cout << "{\"result\":\"passed\",\"contracts\":" << contracts << ",\"images\":" << images
            << ",\"worker_protocol\":" << worker_protocol_version << ",\"temporal_reference_vertices\":129}\n";
}
} // namespace cycles_houdini::harness
