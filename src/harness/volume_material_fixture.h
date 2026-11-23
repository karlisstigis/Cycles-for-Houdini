// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "volume_fixture.h"
#include "motion_fixture.h"

namespace cycles_houdini::harness {
inline SceneUpdate volume_material_reference(MaterialKind kind) {
  SceneUpdate scene;
  auto volume = std::make_shared<VolumeData>();
  volume->key = "independent/volume";
  volume->material = "independent/material";
  volume->revision = 1;
  auto density = density_grid();
  auto temperature = density->deepCopy();
  temperature->setName("temperature");
  temperature->fill(openvdb::CoordBBox(openvdb::Coord(-4), openvdb::Coord(4)), 2.f, true);
  auto emission = density->deepCopy();
  emission->setName("emission");
  emission->fill(openvdb::CoordBBox(openvdb::Coord(-4), openvdb::Coord(4)), .25f, true);
  // Independently constructed engine input: the native graph, captured grid
  // handles, material identifiers and bindings are never replayed here.
  volume->fields = {{"temperature", temperature}, {"emission", emission}, {"density", density}};
  MaterialData material;
  material.key = volume->material;
  material.kind = kind;
  material.color = {0, 0, 0};
  material.density = 1;
  scene.volumes = {volume};
  scene.materials = {material};
  scene.objects.push_back({"independent/object", volume->key});
  // Independent camera specification: horizontal extent 4, square pixels,
  // 80 by 48 image. The core's ortho_scale is its vertical extent.
  CameraData camera;
  camera.orthographic = true;
  camera.ortho_scale = 4.f * 48 / 80;
  scene.camera = camera;
  scene.background = Float3{1, 1, 1};
  return scene;
}
inline Float3 volume_material_mean(const FrameEvent &frame) {
  Float3 result{};
  const auto &pixels = frame.planes.front().pixels;
  for (int y = 21; y < 27; ++y)
    for (int x = 37; x < 43; ++x) {
      const auto offset = (std::size_t(y) * frame.width + x) * 4;
      result.x += pixels[offset] / 36;
      result.y += pixels[offset + 1] / 36;
      result.z += pixels[offset + 2] / 36;
    }
  return result;
}
inline void volume_material_test(const std::string &device, const std::filesystem::path &directory) {
  std::filesystem::create_directories(directory);
  SessionConfig config;
  config.device_id = device;
  config.width = 80;
  config.height = 48;
  config.samples = 512;
  config.interactive = false;
  RenderSession session(config);
  auto scene = volume_material_reference(MaterialKind::VolumeAbsorption);
  motion_wait(session, session.apply(scene));
  auto mean = volume_material_mean(*session.frame());
  const float expected = std::exp(-9.f / 8);
  motion_require(std::abs(mean.x - expected) < .025f && std::abs(mean.y - expected) < .025f &&
                     std::abs(mean.z - expected) < .025f,
                 "Independent volume absorption integral differs");
  session.write_exr((directory / "reference-absorption.exr").string());
  int checks = 1;
  scene.materials.front().kind = MaterialKind::PrincipledVolume;
  scene.materials.front().density = 0;
  scene.materials.front().emission_field = "emission";
  scene.materials.front().volume_emission_color = {.8f, .2f, .1f};
  scene.materials.front().volume_emission_strength = 2;
  scene.background = Float3{0, 0, 0};
  motion_wait(session, session.apply(scene));
  mean = volume_material_mean(*session.frame());
  motion_require(std::abs(mean.x - .45f) < .025f && std::abs(mean.y - .1125f) < .025f &&
                     std::abs(mean.z - .05625f) < .025f,
                 "Independent volume emission integral differs");
  session.write_exr((directory / "reference-emission.exr").string());
  ++checks;
  scene.materials.front().volume_emission_strength = 0;
  scene.materials.front().blackbody_intensity = 1;
  scene.materials.front().temperature_kelvin_scale = 1500;
  motion_wait(session, session.apply(scene));
  mean = volume_material_mean(*session.frame());
  motion_require(mean.x > mean.y && mean.y > mean.z && mean.z >= 0, "Independent 3000 K blackbody ordering differs");
  session.write_exr((directory / "reference-blackbody.exr").string());
  ++checks;
  const auto reject = [&](const SceneUpdate &update, const std::string &reason) {
    const auto generation = session.status().generation;
    bool rejected = false;
    try {
      session.apply(update);
    } catch (const std::exception &error) {
      rejected = std::string(error.what()).find(reason) != std::string::npos;
    }
    motion_require(rejected && session.status().generation == generation,
                   "Strict volume contract was not preserved: " + reason);
    ++checks;
  };
  for (const auto kind :
       {MaterialKind::Diffuse, MaterialKind::Principled, MaterialKind::Emission, MaterialKind::DiagnosticSurface}) {
    SceneUpdate invalid;
    invalid.materials = {scene.materials.front()};
    invalid.materials.front().kind = kind;
    reject(invalid, "A sparse volume is still bound to this material");
  }
  auto broken = std::make_shared<VolumeData>(*scene.volumes.front());
  ++broken->revision;
  std::erase_if(broken->fields, [](const auto &field) { return field.name == "temperature"; });
  SceneUpdate invalid;
  invalid.volumes = {broken};
  reject(invalid, "temperature field"); // Engine clients have not opted into source recovery.
  broken->recover_material = true;
  broken->step_size = -1;
  reject(invalid, "Invalid sparse volume"); // Recovery cannot bypass structural validation.
  for (const auto &field : std::vector<VolumeField>{{"temperature", motion_velocity_grid()},
                                                    {"color", density_grid()},
                                                    {"uv", density_grid()},
                                                    {"velocity", density_grid()},
                                                    {"density", {}}}) {
    broken = std::make_shared<VolumeData>(*scene.volumes.front());
    ++broken->revision;
    std::erase_if(broken->fields, [&](const auto &value) { return value.name == field.name; });
    broken->fields.push_back(field);
    broken->recover_material = true;
    invalid.volumes = {broken};
    reject(invalid, volume_field_error(field));
  }
  session.close();
  motion_require(session.status().device_allocation_bytes == 0,
                 "Independent volume reference retains allocations after close");
  ++checks;
  std::cout << "{\"result\":\"passed\",\"checks\":" << checks << "}\n";
}
} // namespace cycles_houdini::harness
