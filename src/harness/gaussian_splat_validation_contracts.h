// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "motion_fixture.h"
#include "cycles_houdini/snapshot.h"
#include <functional>
#include <OpenColorIO/OpenColorIO.h>

namespace cycles_houdini::harness {
inline void gaussian_splat_validation_contracts(const std::string &device) {
  SessionConfig config;
  config.device_id = device;
  config.width = 48;
  config.height = 48;
  config.samples = 512;
  config.interactive = false;
  RenderSession session(config);
  SceneUpdate scene;
  scene.background = Float3{};
  scene.transparent_background = true;
  scene.motion_blur = false;
  scene.camera = CameraData{};
  scene.camera->orthographic = true;
  scene.camera->ortho_scale = 2;
  scene.camera->transform[11] = 5;
  auto points = std::make_shared<PointData>();
  points->key = "gaussian/geometry";
  points->revision = 1;
  points->positions = {{0, 0, 0}};
  points->radii = {.12f};
  points->splats.emplace();
  auto &splat = *points->splats;
  constexpr float C0 = .28209479177387814f;
  splat.radiance = {{(.7f - .5f) / C0, (.5f - .5f) / C0, (.3f - .5f) / C0, .75f}};
  splat.scales = {{.3f, .12f, .03f}};
  splat.rotations = {{0, 0, 0, 1}};
  scene.points = {points};
  scene.objects = {{"gaussian/object", points->key}};
  const auto initial = motion_wait(session, session.apply(scene));
  const auto pixels = [](const FrameEvent &f) -> const auto & { return f.planes.front().pixels; };
  const auto center = (std::size_t(24) * 48 + 24) * 4;
  const auto &beauty = pixels(*initial);
  for (const float v : beauty)
    motion_require(std::isfinite(v), "Gaussian image contains non-finite values");
  std::cout << "{\"check\":\"Gaussian centre diagnostic\",\"rgba\":[" << beauty[center] << "," << beauty[center + 1]
            << "," << beauty[center + 2] << "," << beauty[center + 3] << "],\"maximum_alpha\":" <<
      [&] {
        float maximum = 0;
        for (std::size_t p = 3; p < beauty.size(); p += 4)
          maximum = std::max(maximum, beauty[p]);
        return maximum;
      }() << ",\"working_space\":\""
            << color_policy().working_space << "\",\"orthographic\":true,\"ortho_scale\":" << scene.camera->ortho_scale
            << ",\"camera_z\":" << scene.camera->transform[11] << ",\"camera_z_basis\":" << scene.camera->transform[10]
            << "}" << std::endl;
  // The raw training color is sRGB; convert the independent linear reference
  // with OCIO, since the session can render in ACEScg or another working space.
  float expected_color[3];
  int component = 0;
  for (const float srgb : {.7f, .5f, .3f})
    expected_color[component++] = std::pow((srgb + .055f) / 1.055f, 2.4f);
  const auto reference_config = OCIO_NAMESPACE::GetCurrentConfig();
  if (reference_config->getColorSpace(OCIO_NAMESPACE::ROLE_SCENE_LINEAR)) {
    std::string rec709_space;
    if (reference_config->getColorSpace("lin_rec709_scene"))
      rec709_space = "lin_rec709_scene";
    else
      for (int i = 0; i < reference_config->getNumColorSpaces(); ++i) {
        const std::string name = reference_config->getColorSpaceNameByIndex(i);
        if (name.starts_with("Linear Rec.709")) {
          rec709_space = name;
          break;
        }
      }
    motion_require(!rec709_space.empty(), "Gaussian reference fixture requires a linear Rec.709 OCIO colorspace");
    reference_config->getProcessor(rec709_space.c_str(), OCIO_NAMESPACE::ROLE_SCENE_LINEAR)
        ->getDefaultCPUProcessor()
        ->applyRGB(expected_color);
  }
  float color_error = 0;
  for (int channel = 0; channel < 3; ++channel)
    color_error =
        std::max(color_error, std::abs(beauty[center + channel] - beauty[center + 3] * expected_color[channel]));
  motion_require(std::abs(beauty[center + 3] - .75f) < .075f && color_error < .015f,
                 "Native Gaussian centre differs from independent opacity/sRGB radiance reference");
  const auto generation = session.status().generation;
  std::uint64_t rejected_count = 0;
  const std::vector<std::function<void(PointData &)>> corruptions = {
      [](auto &p) { p.splats->radiance[0][0] = std::numeric_limits<float>::quiet_NaN(); },
      [](auto &p) { p.splats->radiance[0][3] = 1.01f; },
      [](auto &p) { p.splats->scales[0].y = -1; },
      [](auto &p) { p.splats->scales[0].x = std::numeric_limits<float>::max(); },
      [](auto &p) {
        p.positions[0].x = std::numeric_limits<float>::max();
        p.splats->scales[0].x = std::numeric_limits<float>::max() / 8;
      },
      [](auto &p) { p.splats->rotations[0] = {0, 0, 0, 0}; },
      [](auto &p) { p.splats->rotations[0] = {2, 0, 0, 0}; },
      [](auto &p) { p.splats->radiance.clear(); },
      [](auto &p) { p.splats->scales.clear(); },
      [](auto &p) { p.splats->harmonics.resize(2); },
      [](auto &p) {
        p.splats->harmonics.resize(1);
        p.splats->harmonics[0][6].z = std::numeric_limits<float>::infinity();
      },
      [](auto &p) {
        p.motion.positions = {p.positions, p.positions};
        p.splats->motion_scales = {p.splats->scales};
      }};
  for (const auto &corrupt : corruptions) {
    auto invalid = std::make_shared<PointData>(*points);
    invalid->revision = 2;
    corrupt(*invalid);
    WorkerUpdate wire;
    wire.scene.points = {invalid};
    const auto candidate = decode_worker_update(encode_worker_update(wire));
    bool rejected = false;
    try {
      session.apply(candidate.scene);
    } catch (const SceneUpdateRejected &) {
      rejected = true;
    }
    motion_require(rejected && session.status().generation == generation &&
                       session.status().state != SessionState::Failed,
                   "Invalid Gaussian payload crossed the scene transaction or retired the accepted scene");
    ++rejected_count;
  }
  // A material-only transaction revisits every retained point palette. The
  // native radiance fallback must remain selected without a geometry recook.
  MaterialData unrelated;
  unrelated.key = "unrelated";
  unrelated.kind = MaterialKind::Emission;
  unrelated.color = {1, 0, 0};
  SceneUpdate shading;
  shading.materials = {unrelated};
  const auto converted = session.status().geometry_conversions;
  auto frame = motion_wait(session, session.apply(shading));
  motion_require(session.status().geometry_conversions == converted &&
                     std::abs(pixels(*frame)[center] - beauty[center]) < .035f,
                 "Material-only update replaced the native Gaussian radiance shader");
  auto sphere = std::make_shared<PointData>(*points);
  sphere->revision = 2;
  sphere->splats.reset();
  sphere->material = unrelated.key;
  SceneUpdate edit;
  edit.points = {sphere};
  const auto sphere_frame = motion_wait(session, session.apply(edit));
  const auto off_center = (std::size_t(24) * 48 + 31) * 4;
  motion_require(pixels(*initial)[off_center + 3] > .2f && pixels(*sphere_frame)[off_center + 3] < .02f,
                 "Gaussian-to-sphere transition retained Gaussian bounds or opacity");
  auto restored = std::make_shared<PointData>(*points);
  restored->revision = 3;
  edit.points = {restored};
  frame = motion_wait(session, session.apply(edit));
  motion_require(std::abs(pixels(*frame)[off_center + 3] - pixels(*initial)[off_center + 3]) < .065f,
                 "Sphere-to-Gaussian transition lost native primitive attributes");
  // Empty fallback palette slots and explicit custom shaders coexist.
  restored = std::make_shared<PointData>(*restored);
  restored->revision = 4;
  restored->materials = {"", unrelated.key};
  restored->material_slots = {0};
  edit.points = {restored};
  frame = motion_wait(session, session.apply(edit));
  motion_require(std::abs(pixels(*frame)[center] - beauty[center]) < .04f,
                 "Empty Gaussian palette slot lost native emission");
  session.close();
  motion_require(session.status().device_allocation_bytes == 0 && session.status().owned_geometry_bytes == 0,
                 "Gaussian session retained owned/device allocations");
  std::cout << "{\"result\":\"passed\",\"scope\":\"native Gaussian validation, radiance fallback and sphere "
               "transitions\",\"rejected_payloads\":"
            << rejected_count << "}\n";
}
} // namespace cycles_houdini::harness
