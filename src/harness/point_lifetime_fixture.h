// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "motion_fixture.h"
#include <limits>

namespace cycles_houdini::harness {
inline float lifetime_red(const FrameEvent &frame) {
  float total = 0;
  for (int y = frame.height / 2 - 2; y < frame.height / 2 + 2; ++y)
    for (int x = frame.width / 2 - 2; x < frame.width / 2 + 2; ++x)
      total += motion_red(frame, x, y);
  return total / 16;
}
inline void lifetime_check(const FrameEvent &frame, float expected, const std::string &name) {
  const float measured = lifetime_red(frame) / 0.8f;
  motion_require(std::abs(measured - expected) < 0.03f,
                 name + ": particle existence coverage differs: " + std::to_string(measured));
  for (float value : frame.planes.front().pixels)
    motion_require(std::isfinite(value), "Non-finite lifetime image");
  std::cout << "{\"check\":\"" << name << "\",\"measured\":" << measured << ",\"expected\":" << expected
            << ",\"tolerance\":0.03}\n";
}
inline SceneUpdate lifetime_scene() {
  auto scene = motion_scene();
  scene.camera->ortho_scale = 8.0f / 3;
  return scene;
}
inline void point_lifetime_test(const std::string &device, const std::filesystem::path &destination) {
  std::filesystem::create_directories(destination);
  SessionConfig config;
  config.device_id = device;
  config.width = 96;
  config.height = 64;
  config.samples = 2048;
  config.interactive = false;
  RenderSession session(config);
  auto scene = lifetime_scene();
  auto points = std::make_shared<PointData>();
  points->key = "motion/geometry";
  points->material = "motion/emission";
  points->revision = 1;
  points->positions = {{0, 0, 0}};
  points->radii = {0.5f};
  points->lifetimes = {{0, 1}};
  scene.points = {points};
  auto frame = motion_wait(session, session.apply(scene));
  lifetime_check(*frame, 1, "Full particle lifetime");
  std::uint64_t revision = 1;
  for (const auto interval : std::vector<Float2>{{0, 0.25f}, {0.75f, 1}, {0.25f, 0.75f}, {0.6f, 0.6f}}) {
    auto edit = std::make_shared<PointData>(*points);
    edit->revision = ++revision;
    edit->lifetimes = {interval};
    SceneUpdate update;
    update.points = {edit};
    frame = motion_wait(session, session.apply(update));
    lifetime_check(*frame, interval.y - interval.x, "Clipped lifetime " + std::to_string(interval.x));
  }
  auto clip = std::make_shared<PointData>(*points);
  clip->revision = ++revision;
  clip->lifetimes = {{0.75f, 1}};
  SceneUpdate update;
  update.points = {clip};
  frame = motion_wait(session, session.apply(update));
  const auto converted = session.status().geometry_conversions;
  SceneUpdate off;
  off.motion_blur = false;
  frame = motion_wait(session, session.apply(off));
  lifetime_check(*frame, 0, "No existence at the motion-disabled centre");
  SceneUpdate on;
  on.motion_blur = true;
  frame = motion_wait(session, session.apply(on));
  lifetime_check(*frame, 0.25f, "Re-enable lifetime clipping");
  motion_require(session.status().geometry_conversions == converted, "Lifetime toggle converted geometry");
  clip = std::make_shared<PointData>(*clip);
  clip->revision = ++revision;
  clip->lifetimes = {{0.25f, 0.75f}};
  update = {};
  update.points = {clip};
  update.motion_blur = false;
  frame = motion_wait(session, session.apply(update));
  lifetime_check(*frame, 1, "Motion-disabled centre lies inside lifetime");
  clip = std::make_shared<PointData>(*clip);
  clip->revision = ++revision;
  clip->lifetimes = {{0.75f, 1}};
  update = {};
  update.points = {clip};
  update.motion_blur = true;
  motion_wait(session, session.apply(update));
  // A second resource changes global primitive/attribute offsets. Instancing
  // the clipped resource exercises the Embree instID path as well.
  auto other = std::make_shared<PointData>(*points);
  other->key = "other/geometry";
  other->positions = {{-1.5f, 0, 0}};
  other->revision = 1;
  update = {};
  update.points = {other};
  update.objects = {{"other/object", "other/geometry"}, {"second/instance", clip->key}};
  update.objects.back().transform[3] = 1.5f;
  frame = motion_wait(session, session.apply(update));
  lifetime_check(*frame, 0.25f, "Lifetime with multiple resources and instances");
  const auto &pixels = frame->planes.front().pixels;
  const int y = frame->height / 2, x = int((1.5f / 4 + 0.5f) * frame->width);
  motion_require(std::abs(pixels[(std::size_t(y) * frame->width + x) * 4] / 0.8f - 0.25f) < 0.04f,
                 "Instance lifetime differs");
  const auto generation = session.status().generation;
  for (const auto interval :
       std::vector<Float2>{{-0.1f, 1}, {0, 1.1f}, {0.8f, 0.2f}, {std::numeric_limits<float>::quiet_NaN(), 1}}) {
    auto invalid = std::make_shared<PointData>(*clip);
    invalid->revision = ++revision;
    invalid->lifetimes = {interval};
    update = {};
    update.points = {invalid};
    bool rejected = false;
    try {
      session.apply(update);
    } catch (const std::invalid_argument &error) {
      rejected = std::string(error.what()).find("lifetime") != std::string::npos;
    }
    motion_require(rejected && session.status().generation == generation, "Invalid lifetime mutated the scene");
  }
  // Clearing an attribute must clear its object flag and restore all-time
  // existence rather than retaining stale GPU lifetime data.
  clip = std::make_shared<PointData>(*clip);
  clip->revision = ++revision;
  clip->lifetimes.clear();
  update = {};
  update.points = {clip};
  frame = motion_wait(session, session.apply(update));
  lifetime_check(*frame, 1, "Remove lifetime attribute");
  session.close();
  motion_require(session.status().device_allocation_bytes == 0, "Lifetime session retained allocations");

  // Native GSplats use a custom Embree/OptiX intersection. Compare its
  // independent shutter-time integral to the full-lifetime image, so this
  // test does not assume the Gaussian has opaque sphere coverage.
  {
    RenderSession splat_session(config);
    auto splat_scene = lifetime_scene();
    auto splat = std::make_shared<PointData>();
    splat->key = "motion/geometry";
    splat->revision = 1;
    splat->positions = {{0, 0, 0}};
    splat->radii = {.5f};
    splat->lifetimes = {{0, 1}};
    splat->splats.emplace();
    auto &gaussian = *splat->splats;
    gaussian.scene_linear_color = true;
    gaussian.scales = {{.3f, .15f, .03f}};
    gaussian.rotations = {{0, 0, 0, 1}};
    gaussian.radiance = {{.8f, .2f, .1f, .8f}};
    splat_scene.points = {splat};
    auto full = motion_wait(splat_session, splat_session.apply(splat_scene));
    const float splat_reference = lifetime_red(*full);
    motion_require(splat_reference > .2f, "Full-lifetime GSplat emitted no radiance");
    const auto check_splat = [&](const FrameEvent &value, float expected, const char *phase) {
      const float measured = lifetime_red(value) / splat_reference;
      for (float pixel : value.planes.front().pixels)
        motion_require(std::isfinite(pixel), "Non-finite GSplat lifetime image");
      motion_require(std::abs(measured - expected) < .045f,
                     std::string("GSplat lifetime ") + phase + ": " + std::to_string(measured));
      std::cout << "{\"check\":\"GSplat lifetime\",\"phase\":\"" << phase << "\",\"measured\":" << measured
                << ",\"expected\":" << expected << ",\"tolerance\":0.045}\n";
    };
    std::uint64_t splat_revision = 1;
    for (const auto interval : std::vector<Float2>{{0, .25f}, {.25f, .75f}, {.6f, .6f}}) {
      auto clipped = std::make_shared<PointData>(*splat);
      clipped->revision = ++splat_revision;
      clipped->lifetimes = {interval};
      SceneUpdate edit;
      edit.points = {clipped};
      auto result = motion_wait(splat_session, splat_session.apply(edit));
      check_splat(*result, interval.y - interval.x, "shutter integral");
    }
    auto clipped = std::make_shared<PointData>(*splat);
    clipped->revision = ++splat_revision;
    clipped->lifetimes = {{.75f, 1}};
    SceneUpdate edit;
    edit.points = {clipped};
    edit.motion_blur = false;
    check_splat(*motion_wait(splat_session, splat_session.apply(edit)), 0, "disabled shutter midpoint outside");
    clipped = std::make_shared<PointData>(*clipped);
    ++clipped->revision;
    clipped->lifetimes = {{.25f, .75f}};
    edit.points = {clipped};
    check_splat(*motion_wait(splat_session, splat_session.apply(edit)), 1, "disabled shutter midpoint inside");
    clipped = std::make_shared<PointData>(*clipped);
    ++clipped->revision;
    clipped->lifetimes.clear();
    edit.points = {clipped};
    check_splat(*motion_wait(splat_session, splat_session.apply(edit)), 1, "attribute removed");
    splat_session.close();
    motion_require(splat_session.status().device_allocation_bytes == 0, "GSplat lifetime session retained allocations");
  }

  // Independent hard-shadow integral. The sphere is away from the camera ray
  // but lies exactly on the centre receiver-to-point-light segment.
  RenderSession shadow(config);
  scene = lifetime_scene();
  scene.materials.clear();
  scene.objects.clear();
  MaterialData ground;
  ground.key = "receiver";
  ground.color = {1, 1, 1};
  ground.roughness = 0;
  MaterialData blocker;
  blocker.key = "blocker";
  blocker.color = {0, 0, 0};
  scene.materials = {ground, blocker};
  auto mesh = std::make_shared<MeshData>();
  mesh->key = "ground";
  mesh->revision = 1;
  mesh->materials = {ground.key};
  mesh->positions = {{-4, -4, 0}, {4, -4, 0}, {4, 4, 0}, {-4, 4, 0}};
  mesh->triangles = {{0, 1, 2}, {0, 2, 3}};
  scene.meshes = {mesh};
  scene.objects = {{"ground/object", "ground"}};
  LightData light;
  light.key = "light";
  light.radius = 0;
  light.transform[3] = -4;
  light.transform[11] = 5;
  light.strength = {100, 100, 100};
  scene.lights = {light};
  auto baseline = motion_wait(shadow, shadow.apply(scene));
  const float reference = lifetime_red(*baseline);
  shadow.write_exr(
      (destination / (device == "CPU" ? "cpu-shadow-receiver.exr" : "optix-shadow-receiver.exr")).string());
  std::cout << "{\"check\":\"Independent shadow receiver\",\"radiance\":" << reference << "}\n";
  constexpr double pi = 3.14159265358979323846;
  const float analytic = float(100 * 5 / (4 * pi * pi * 41 * std::sqrt(41.0)));
  motion_require(std::abs(reference - analytic) < 0.001f,
                 "Independent Lambertian point-light irradiance differs: " + std::to_string(reference));
  auto sphere = std::make_shared<PointData>();
  sphere->key = "blocker";
  sphere->material = blocker.key;
  sphere->revision = 1;
  sphere->positions = {{-0.8f, 0, 1}};
  sphere->radii = {0.25f};
  sphere->lifetimes = {{0, 1}};
  update = {};
  update.points = {sphere};
  update.objects = {{"blocker/object", "blocker"}};
  frame = motion_wait(shadow, shadow.apply(update));
  motion_require(lifetime_red(*frame) < reference * 0.02f,
                 "Full-lifetime particle did not cast the expected hard shadow");
  for (const auto interval : std::vector<Float2>{{0, 0.25f}, {0.75f, 1}, {0.6f, 0.6f}}) {
    sphere = std::make_shared<PointData>(*sphere);
    ++sphere->revision;
    sphere->lifetimes = {interval};
    update = {};
    update.points = {sphere};
    frame = motion_wait(shadow, shadow.apply(update));
    const float measured = lifetime_red(*frame) / reference;
    const float expected = 1 - (interval.y - interval.x);
    motion_require(std::abs(measured - expected) < 0.03f, "Particle shadow did not obey the lifetime interval");
    std::cout << "{\"check\":\"Particle shadow lifetime\",\"measured\":" << measured << ",\"expected\":" << expected
              << ",\"tolerance\":0.03}\n";
  }
  shadow.write_exr(
      (destination / (device == "CPU" ? "cpu-lifetime-shadow.exr" : "optix-lifetime-shadow.exr")).string());
  shadow.close();
  motion_require(shadow.status().device_allocation_bytes == 0, "Shadow lifetime session retained allocations");
  std::cout << "{\"result\":\"passed\",\"scope\":\"per-point existence, primary rays, shadows, instances, toggle, "
               "validation and retirement\"}\n";
}
} // namespace cycles_houdini::harness
