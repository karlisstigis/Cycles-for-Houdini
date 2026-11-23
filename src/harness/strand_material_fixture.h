// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "motion_fixture.h"
#include <type_traits>

namespace cycles_houdini::harness {
// Independent constants and a deliberately different palette order from the
// Houdini fixture. No host geometry, native graphs or converted scene replay.
inline SceneUpdate strand_material_fixture(bool hair) {
  SceneUpdate scene;
  scene.camera = CameraData{};
  scene.camera->orthographic = true;
  scene.camera->ortho_scale = 4;
  scene.background = Float3{};
  for (const auto &[key, color] : std::vector<std::pair<std::string, Float3>>{{"reference/blue", {.05f, .1f, .8f}},
                                                                              {"reference/green", {.05f, .7f, .1f}},
                                                                              {"reference/red", {.8f, .1f, .05f}}}) {
    MaterialData material;
    material.key = key;
    material.kind = MaterialKind::Emission;
    material.color = color;
    scene.materials.push_back(material);
  }
  const std::vector<std::string> palette = {"reference/blue", "reference/green", "reference/red"};
  const std::vector<int> slots = {2, 0, 1, 1};
  const std::vector<float> positions = {-1.2f, -.4f, .4f, 1.2f};
  if (hair) {
    auto data = std::make_shared<CurveData>();
    data->key = "reference/curves";
    data->revision = 1;
    data->materials = palette;
    data->material_slots = slots;
    for (const auto x : positions) {
      data->first_key.push_back(int(data->positions.size()));
      data->positions.push_back({x, -.6f, 0});
      data->positions.push_back({x, .6f, 0});
      data->radii.push_back(.06f);
      data->radii.push_back(.06f);
    }
    scene.curves = {data};
    scene.objects.push_back({"reference/object", data->key});
  } else {
    auto data = std::make_shared<PointData>();
    data->key = "reference/points";
    data->revision = 1;
    data->materials = palette;
    data->material_slots = slots;
    for (const auto x : positions) {
      data->positions.push_back({x, 0, 0});
      data->radii.push_back(.24f);
    }
    scene.points = {data};
    scene.objects.push_back({"reference/object", data->key});
  }
  return scene;
}
inline void strand_expect(const FrameEvent &frame, const std::vector<Float3> &colors) {
  const auto &pixels = frame.planes.front().pixels;
  for (std::size_t i = 0; i < colors.size(); ++i) {
    const int x = int(((-1.2f + .8f * i) / 4 + .5f) * frame.width);
    const auto offset = (std::size_t(frame.height / 2) * frame.width + x) * 4;
    const auto expected = colors[i];
    motion_require(std::abs(pixels[offset] - expected.x) < .001f && std::abs(pixels[offset + 1] - expected.y) < .001f &&
                       std::abs(pixels[offset + 2] - expected.z) < .001f,
                   "Independent strand palette radiance differs");
  }
}
template <class Data>
inline void strand_material_case(const std::string &device, const std::filesystem::path &destination, bool hair) {
  SessionConfig config;
  config.device_id = device;
  config.width = config.height = 96;
  config.samples = 8;
  config.interactive = false;
  RenderSession session(config);
  auto scene = strand_material_fixture(hair);
  std::shared_ptr<Data> original;
  if constexpr (std::is_same_v<Data, PointData>)
    original = std::make_shared<Data>(*scene.points.front());
  else
    original = std::make_shared<Data>(*scene.curves.front());
  auto image = motion_wait(session, session.apply(scene));
  strand_expect(*image, {{.8f, .1f, .05f}, {.05f, .1f, .8f}, {.05f, .7f, .1f}, {.05f, .7f, .1f}});
  session.write_exr((destination / (hair ? "reference-curves.exr" : "reference-points.exr")).string());
  const auto update = [](std::shared_ptr<Data> data) {
    SceneUpdate result;
    if constexpr (std::is_same_v<Data, PointData>)
      result.points = {data};
    else
      result.curves = {data};
    return result;
  };
  std::size_t checks = 1;
  const auto reject = [&](SceneUpdate change, const std::string &message) {
    const auto generation = session.status().generation, sequence = session.frame()->sequence;
    bool failed = false;
    try {
      session.apply(change);
    } catch (const std::invalid_argument &error) {
      failed = std::string(error.what()).find(message) != std::string::npos;
    }
    motion_require(failed, "Invalid strand update did not report: " + message);
    motion_require(session.status().generation == generation && session.frame()->sequence == sequence,
                   "Invalid strand update mutated the render");
    ++checks;
  };
  const std::vector<Float3> authored_colors{{.8f, .1f, .05f}, {.05f, .1f, .8f}, {.05f, .7f, .1f}, {.05f, .7f, .1f}};
  const auto recover_domain = [&](SceneUpdate change, const std::string &material, bool retained) {
    const auto converted = session.status().geometry_conversions;
    strand_expect(*motion_wait(session, session.apply(change)),
                  {{.8f, .1f, .05f}, {1, 0, 1}, {.05f, .7f, .1f}, {.05f, .7f, .1f}});
    const auto status = session.status();
    motion_require(status.source_diagnostics.size() == 1 &&
                       status.source_diagnostics.front().code == "material.geometry_binding" &&
                       status.source_diagnostics.front().source == material &&
                       status.source_diagnostics.front().resource == original->key,
                   "Strand volume binding did not report its local material/geometry diagnostic");
    if (retained)
      motion_require(status.geometry_conversions == converted,
                     "Material-only strand recovery converted retained geometry");
    ++checks;
  };
  const auto repair_domain = [&](SceneUpdate change, bool retained) {
    const auto converted = session.status().geometry_conversions;
    strand_expect(*motion_wait(session, session.apply(change)), authored_colors);
    motion_require(session.status().source_diagnostics.empty(), "Repaired strand palette retained diagnostics");
    if (retained)
      motion_require(session.status().geometry_conversions == converted,
                     "Material-only strand repair converted retained geometry");
    ++checks;
  };
  for (const int operation : {0, 1, 2, 3, 4, 5, 6}) {
    auto broken = std::make_shared<Data>(*original);
    broken->revision = 2;
    std::string diagnostic;
    switch (operation) {
    case 0:
      broken->material = "reference/red";
      diagnostic = "cannot also specify";
      break;
    case 1:
      broken->materials[0] = "";
      diagnostic = "empty key";
      break;
    case 2:
      broken->materials[0] = "reference/missing";
      diagnostic = "Missing material";
      break;
    case 3:
      broken->material_slots.pop_back();
      diagnostic = "slot count mismatch";
      break;
    case 4:
      broken->material_slots[0] = -1;
      diagnostic = "Invalid point/curve shader slot";
      break;
    case 5:
      broken->material_slots[0] = 3;
      diagnostic = "Invalid point/curve shader slot";
      break;
    case 6:
      broken->materials.clear();
      broken->material = "reference/red";
      diagnostic = "explicit material palette";
      break;
    }
    reject(update(broken), diagnostic);
  }
  // Shader-domain incompatibility is recoverable, unlike malformed palettes.
  // Only the affected blue slot becomes a camera-visible magenta marker.
  auto volume_bound = std::make_shared<Data>(*original);
  volume_bound->revision = 2;
  volume_bound->materials[0] = "reference/volume";
  auto domain_change = update(volume_bound);
  MaterialData volume;
  volume.key = "reference/volume";
  volume.kind = MaterialKind::VolumeAbsorption;
  domain_change.materials = {volume};
  recover_domain(domain_change, volume.key, false);
  auto repaired = std::make_shared<Data>(*original);
  repaired->revision = 3;
  auto repair = update(repaired);
  repair.deleted_materials = {volume.key};
  repair_domain(repair, false);
  SceneUpdate deletion;
  deletion.deleted_materials = {"reference/blue"};
  reject(deletion, "Material is still bound");
  volume = scene.materials.front();
  volume.kind = MaterialKind::VolumeAbsorption;
  SceneUpdate retype;
  retype.materials = {volume};
  recover_domain(retype, volume.key, true);
  SceneUpdate surface_repair;
  surface_repair.materials = {scene.materials.front()};
  repair_domain(surface_repair, true);
  auto uniform = std::make_shared<Data>(*original);
  uniform->revision = 4;
  uniform->material_slots.clear();
  strand_expect(*motion_wait(session, session.apply(update(uniform))), std::vector<Float3>(4, {.05f, .1f, .8f}));
  ++checks;
  auto legacy = std::make_shared<Data>(*uniform);
  legacy->revision = 5;
  legacy->materials.clear();
  legacy->material = "reference/red";
  strand_expect(*motion_wait(session, session.apply(update(legacy))), std::vector<Float3>(4, {.8f, .1f, .05f}));
  ++checks;
  session.close();
  motion_require(session.status().device_allocation_bytes == 0, "Strand palette allocations retained after close");
  std::cout << "{\"kind\":\"" << (hair ? "curves" : "points") << "\",\"checks\":" << checks
            << ",\"result\":\"passed\"}\n";
}
inline void strand_material_test(const std::string &device, const std::filesystem::path &destination) {
  std::filesystem::create_directories(destination);
  strand_material_case<PointData>(device, destination, false);
  strand_material_case<CurveData>(device, destination, true);
  std::cout << "{\"result\":\"passed\",\"checks\":30}\n";
}
} // namespace cycles_houdini::harness
