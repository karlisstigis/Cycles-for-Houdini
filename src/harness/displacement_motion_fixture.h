// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "motion_fixture.h"

namespace cycles_houdini::harness {
// Independent expected meshes: no displacement shader, native conversion or
// replay of the plugin's scene. All offsets and transforms are analytic.
inline void displacement_motion_reference(const std::string &device, const std::string &output,
                                          bool subdivision_normals_only = false) {
  std::filesystem::create_directories(output);
  SessionConfig config;
  config.device_id = device;
  config.width = 160;
  config.height = 90;
  config.samples = 1024;
  config.interactive = false;
  config.passes = {"combined", "position", "normal", "depth"};
  RenderSession session(config);
  std::uint64_t revision = 0;
  int references = 0;
  for (const auto &kind :
       {std::string("normal"), std::string("world"), std::string("position"), std::string("authored_normal"),
        std::string("linear_position"), std::string("catmull_clark_position"), std::string("legacy_sequence_position"),
        std::string("held_position")})
    for (int steps : {3, 5}) {
      if (subdivision_normals_only && kind != "authored_normal")
        continue;
      SceneUpdate scene;
      MaterialData material;
      material.key = "reference";
      material.kind = MaterialKind::Emission;
      material.color = {.2f, .4f, .8f};
      scene.materials = {material};
      auto mesh = std::make_shared<MeshData>();
      mesh->key = "reference";
      mesh->revision = ++revision;
      mesh->materials = {material.key};
      const std::vector<Float3> square = {{-.5f, -.5f, 0}, {.5f, -.5f, 0}, {.5f, .5f, 0}, {-.5f, .5f, 0}};
      mesh->triangles = {{0, 1, 2}, {0, 2, 3}};
      const auto positions = [&](float q) {
        auto values = square;
        if (kind == "held_position")
          q = 0;
        if (kind == "legacy_sequence_position")
          q = q < 0 ? -.5f : q > 0 ? .5f : 0;
        const float k = q, angle = q * float(M_PI) / 4, inv = 1 / std::sqrt(1 + k * k);
        for (auto &p : values) {
          if (kind == "normal") {
            const float x = p.x;
            p.x -= .5f * k * inv;
            p.z = k * x + .5f * inv;
          } else if (kind == "world") {
            p.x -= .5f * std::sin(angle);
            p.z = .5f * std::cos(angle);
          } else if (kind == "position" || kind.ends_with("_position")) {
            p.x = 1.5f * (p.x + q);
          } else {
            p.x += .5f * k * inv;
            p.z = .5f * inv;
          }
        }
        return values;
      };
      mesh->positions = positions(0);
      if (kind == "linear_position" || kind == "catmull_clark_position") {
        mesh->triangles.clear();
        mesh->subdivision.emplace();
        auto &subd = *mesh->subdivision;
        subd.face_sizes = {4};
        subd.face_corners = {0, 1, 2, 3};
        subd.settings.type = kind == "linear_position" ? SubdivisionType::Linear : SubdivisionType::CatmullClark;
        subd.settings.space = SubdivisionSpace::Object;
        subd.settings.dicing_rate = .1f;
        subd.settings.max_level = 3;
      }
      ObjectData object;
      object.key = "reference";
      object.geometry_key = mesh->key;
      for (int step = 0; step < steps; ++step) {
        const float q = float(step) / float(steps - 1) - .5f;
        if (step != steps / 2)
          mesh->motion.positions.push_back(positions(q));
        if (kind == "world") {
          const float a = q * float(M_PI) / 4;
          Matrix matrix = identity_matrix;
          matrix[0] = matrix[10] = std::cos(a);
          matrix[2] = std::sin(a);
          matrix[8] = -std::sin(a);
          object.motion.push_back(matrix);
        }
      }
      scene.meshes = {mesh};
      scene.objects = {object};
      scene.motion_blur = true;
      scene.background = Float3{};
      CameraData camera;
      camera.orthographic = true;
      camera.ortho_scale = 2.25f;
      camera.shutter_frames = .5f;
      scene.camera = camera;
      RenderSettings rendering;
      rendering.samples = 1024;
      rendering.adaptive_sampling = false;
      scene.rendering = rendering;
      motion_wait(session, session.apply(scene));
      session.write_exr((std::filesystem::path(output) / (kind + "-" + std::to_string(steps) + ".exr")).string());
      ++references;
    }
  session.close();
  motion_require(session.status().owned_geometry_bytes == 0, "Displacement reference retained geometry");
  std::cout << "{\"result\":\"passed\",\"independent_motion_references\":" << references << "}\n";
}
} // namespace cycles_houdini::harness
