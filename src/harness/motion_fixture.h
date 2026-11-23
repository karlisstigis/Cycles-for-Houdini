// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <filesystem>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <thread>

namespace cycles_houdini::harness {
inline void motion_require(bool test, const std::string &message) {
  if (!test)
    throw std::runtime_error(message);
}
inline std::shared_ptr<const FrameEvent> motion_wait(RenderSession &session, std::uint64_t generation) {
  const auto start = std::chrono::steady_clock::now();
  while (true) {
    const auto status = session.status();
    motion_require(status.state != SessionState::Failed, status.error);
    auto frame = session.frame();
    if (frame && frame->generation == generation && frame->complete)
      return frame;
    motion_require(std::chrono::steady_clock::now() - start < std::chrono::seconds(60),
                   "Motion reference timed out: " + status.message);
    std::this_thread::sleep_for(std::chrono::milliseconds(10));
  }
}
inline float motion_red(const FrameEvent &frame, int x, int y) {
  return frame.planes.front().pixels[(std::size_t(y) * frame.width + x) * 4];
}
inline float motion_coverage(const FrameEvent &frame) {
  float sum = 0;
  for (int y = frame.height / 2 - 1; y <= frame.height / 2; ++y)
    for (int x = frame.width / 2 - 1; x <= frame.width / 2; ++x)
      sum += motion_red(frame, x, y);
  return sum / (4 * 0.8f);
}
inline void check_motion_coverage(const FrameEvent &frame, float expected, const std::string &name) {
  const float coverage = motion_coverage(frame);
  motion_require(std::abs(coverage - expected) < 0.03f,
                 name + ": analytic shutter coverage differs: " + std::to_string(coverage) + " expected " +
                     std::to_string(expected));
  for (float value : frame.planes.front().pixels)
    motion_require(std::isfinite(value), "Non-finite motion image");
  motion_require(motion_red(frame, frame.width * 3 / 4, frame.height / 2) == 0,
                 "Motion has escaped the specified shutter bounds");
  std::cout << "{\"check\":\"" << name << "\",\"measured_coverage\":" << coverage
            << ",\"analytic_coverage\":" << expected << ",\"tolerance\":0.03}\n";
}
inline SceneUpdate motion_scene() {
  SceneUpdate scene;
  scene.background = Float3{};
  scene.motion_blur = true;
  scene.camera = CameraData{};
  scene.camera->orthographic = true;
  scene.camera->ortho_scale = 2.25f;
  scene.camera->shutter_frames = 0.5f;
  MaterialData material;
  material.key = "motion/emission";
  material.kind = MaterialKind::Emission;
  material.color = {0.8f, 0.1f, 0.02f};
  scene.materials.push_back(material);
  scene.objects.push_back({"motion/object", "motion/geometry"});
  return scene;
}
inline std::vector<Matrix> linear_transform_motion(const Matrix &center) {
  auto first = center, last = center;
  first[3] -= 0.5f;
  last[3] += 0.5f;
  return {first, center, last};
}
inline void motion_reference_test(const std::string &device, const std::filesystem::path &destination) {
  std::filesystem::create_directories(destination);
  SessionConfig config;
  config.device_id = device;
  config.width = 160;
  config.height = 90;
  config.samples = 2048;
  config.interactive = false;
  const auto prefix = device == "CPU" ? "cpu-" : "optix-";
  {
    RenderSession session(config);
    auto scene = motion_scene();
    auto points = std::make_shared<PointData>();
    points->key = "motion/geometry";
    points->material = "motion/emission";
    points->revision = 1;
    points->positions = {{0, 0, 0}};
    points->radii = {0.2f};
    points->motion.positions = {{{-0.5f, 0, 0}}, {{0.5f, 0, 0}}};
    scene.points.push_back(points);
    auto deform = motion_wait(session, session.apply(scene));
    check_motion_coverage(*deform, 0.4f, "Point deformation");
    session.write_exr((destination / (std::string(prefix) + "point-deformation.exr")).string());
    const auto generation = session.status().generation;
    auto broken = std::make_shared<PointData>(*points);
    broken->revision = 2;
    broken->motion.positions[0].push_back({0, 0, 0});
    SceneUpdate invalid;
    invalid.points = {broken};
    bool rejected = false;
    try {
      session.apply(invalid);
    } catch (const std::invalid_argument &error) {
      rejected = std::string(error.what()).find("correspondence") != std::string::npos;
    }
    motion_require(rejected && session.status().generation == generation,
                   "Invalid motion correspondence mutated the session");
    points = std::make_shared<PointData>(*points);
    points->revision = 2;
    points->motion.positions = {{{-0.5f, 0, 0}}, {{-0.25f, 0, 0}}, {{0.25f, 0, 0}}, {{0.5f, 0, 0}}};
    SceneUpdate five;
    five.points = {points};
    auto frame = motion_wait(session, session.apply(five));
    check_motion_coverage(*frame, 0.4f, "Five-step point deformation");
    motion_require(std::abs(motion_coverage(*frame) - motion_coverage(*deform)) < 0.005f,
                   "Equivalent three/five-step linear motion differs");
    SceneUpdate off;
    off.motion_blur = false;
    const auto converted = session.status().geometry_conversions;
    frame = motion_wait(session, session.apply(off));
    check_motion_coverage(*frame, 1, "Motion disabled");
    motion_require(session.status().geometry_conversions == converted,
                   "Disabling motion unnecessarily converted geometry");
    SceneUpdate on;
    on.motion_blur = true;
    frame = motion_wait(session, session.apply(on));
    check_motion_coverage(*frame, 0.4f, "Motion re-enabled");
    motion_require(session.status().geometry_conversions == converted,
                   "Re-enabling motion unnecessarily converted geometry");
    points = std::make_shared<PointData>(*points);
    points->revision = 3;
    points->motion.positions.clear();
    SceneUpdate object;
    object.points = {points};
    object.motion_blur = true;
    object.objects = scene.objects;
    object.objects.front().motion = linear_transform_motion(identity_matrix);
    frame = motion_wait(session, session.apply(object));
    check_motion_coverage(*frame, 0.4f, "Point object transform");
    session.write_exr((destination / (std::string(prefix) + "point-transform.exr")).string());
    motion_require(std::abs(motion_coverage(*frame) - motion_coverage(*deform)) < 0.005f,
                   "Point deformation and object translation differ");
    frame = motion_wait(session, session.apply(off));
    check_motion_coverage(*frame, 1, "Object motion disabled");
    frame = motion_wait(session, session.apply(on));
    check_motion_coverage(*frame, 0.4f, "Object motion re-enabled");
    // Camera motion has the same relative translation with the opposite sign.
    SceneUpdate camera;
    camera.objects = scene.objects;
    camera.camera = scene.camera;
    camera.camera->motion = linear_transform_motion(camera.camera->transform);
    frame = motion_wait(session, session.apply(camera));
    check_motion_coverage(*frame, 0.4f, "Camera transform");
    session.write_exr((destination / (std::string(prefix) + "camera-transform.exr")).string());
    frame = motion_wait(session, session.apply(off));
    check_motion_coverage(*frame, 1, "Camera motion disabled");
    frame = motion_wait(session, session.apply(on));
    check_motion_coverage(*frame, 0.4f, "Camera motion re-enabled");
    SceneUpdate radius;
    radius.camera = scene.camera;
    points = std::make_shared<PointData>(*points);
    points->revision = 4;
    points->motion.positions = {{{-0.5f, 0, 0}}, {{0.5f, 0, 0}}};
    points->motion.radii = {{0.1f}, {0.3f}};
    radius.points = {points};
    frame = motion_wait(session, session.apply(radius));
    check_motion_coverage(*frame, 5.0f / 12, "Point radius motion");
    session.write_exr((destination / (std::string(prefix) + "point-radius.exr")).string());
    session.close();
    motion_require(session.status().device_allocation_bytes == 0, "Motion session retained device allocations");
  }
  {
    RenderSession session(config);
    auto scene = motion_scene();
    auto hair = std::make_shared<CurveData>();
    hair->key = "motion/geometry";
    hair->material = "motion/emission";
    hair->revision = 1;
    hair->positions = {{0, -0.6f, 0}, {0, 0.6f, 0}};
    hair->radii = {0.1f, 0.1f};
    hair->first_key = {0};
    hair->motion.positions = {{{-0.5f, -0.6f, 0}, {-0.5f, 0.6f, 0}}, {{0.5f, -0.6f, 0}, {0.5f, 0.6f, 0}}};
    scene.curves.push_back(hair);
    auto frame = motion_wait(session, session.apply(scene));
    check_motion_coverage(*frame, 0.2f, "Hair deformation");
    session.write_exr((destination / (std::string(prefix) + "hair-deformation.exr")).string());
    hair = std::make_shared<CurveData>(*hair);
    hair->revision = 2;
    hair->motion.positions.clear();
    SceneUpdate edit;
    edit.curves = {hair};
    edit.objects = scene.objects;
    edit.objects.front().motion = linear_transform_motion(identity_matrix);
    frame = motion_wait(session, session.apply(edit));
    check_motion_coverage(*frame, 0.2f, "Hair object transform");
    session.write_exr((destination / (std::string(prefix) + "hair-transform.exr")).string());
    session.close();
    motion_require(session.status().device_allocation_bytes == 0, "Hair motion session retained device allocations");
  }
  {
    RenderSession session(config);
    auto scene = motion_scene();
    auto mesh = std::make_shared<MeshData>();
    mesh->key = "motion/geometry";
    mesh->revision = 1;
    mesh->materials = {"motion/emission"};
    mesh->positions = {{-0.2f, -0.6f, 0}, {0.2f, -0.6f, 0}, {0.2f, 0.6f, 0}, {-0.2f, 0.6f, 0}};
    mesh->triangles = {{0, 1, 2}, {0, 2, 3}};
    for (float offset : {-0.5f, 0.5f}) {
      auto sample = mesh->positions;
      for (auto &p : sample)
        p.x += offset;
      mesh->motion.positions.push_back(sample);
    }
    scene.meshes.push_back(mesh);
    auto frame = motion_wait(session, session.apply(scene));
    check_motion_coverage(*frame, 0.4f, "Mesh deformation");
    session.write_exr((destination / (std::string(prefix) + "mesh-deformation.exr")).string());
    mesh = std::make_shared<MeshData>(*mesh);
    mesh->revision = 2;
    mesh->motion.positions.clear();
    SceneUpdate edit;
    edit.meshes = {mesh};
    edit.objects = scene.objects;
    edit.objects.front().motion = linear_transform_motion(identity_matrix);
    frame = motion_wait(session, session.apply(edit));
    check_motion_coverage(*frame, 0.4f, "Mesh object transform");
    session.write_exr((destination / (std::string(prefix) + "mesh-transform.exr")).string());
    const auto generation = session.status().generation;
    auto broken = std::make_shared<MeshData>(*mesh);
    broken->revision = 3;
    broken->corner_normals.resize(6, Float3{0, 0, 1});
    broken->corner_normals[0].x = std::numeric_limits<float>::quiet_NaN();
    SceneUpdate invalid;
    invalid.meshes = {broken};
    bool rejected = false;
    try {
      session.apply(invalid);
    } catch (const std::invalid_argument &error) {
      rejected = std::string(error.what()).find("Non-finite normal") != std::string::npos;
    }
    motion_require(rejected && session.status().generation == generation,
                   "Invalid internal normals were accepted or mutated the session");
    std::cout << "{\"check\":\"Non-finite internal mesh normals rejected without mutation\",\"result\":\"passed\"}\n";
    for (bool uv : {true, false}) {
      broken = std::make_shared<MeshData>(*mesh);
      broken->revision = 3;
      if (uv) {
        broken->corner_uv.resize(6, Float2{0, 0});
        broken->corner_uv[0].x = std::numeric_limits<float>::quiet_NaN();
      } else {
        broken->corner_colors.resize(6, Float3{1, 1, 1});
        broken->corner_colors[0].x = std::numeric_limits<float>::infinity();
      }
      invalid.meshes = {broken};
      rejected = false;
      try {
        session.apply(invalid);
      } catch (const std::invalid_argument &error) {
        rejected = std::string(error.what()).find(uv ? "Non-finite UV" : "Non-finite mesh color") != std::string::npos;
      }
      motion_require(rejected && session.status().generation == generation,
                     "Invalid internal UV/color accepted or mutated the session");
    }
    std::cout
        << "{\"check\":\"Non-finite internal UV/color buffers rejected without mutation\",\"result\":\"passed\"}\n";
    session.close();
    motion_require(session.status().device_allocation_bytes == 0, "Mesh motion session retained device allocations");
  }
  {
    auto small = config;
    small.samples = 64;
    small.interactive = true;
    small.passes = {"combined", "position"};
    RenderSession session(small);
    auto scene = motion_scene();
    auto mesh = std::make_shared<MeshData>();
    mesh->key = "motion/geometry";
    mesh->revision = 1;
    mesh->materials = {"motion/emission"};
    mesh->positions = {{-.15f, -.18f, 0}, {.15f, -.18f, 0}, {.15f, .18f, 0}, {-.15f, .18f, 0}};
    mesh->triangles = {{0, 1, 2}, {0, 2, 3}};
    scene.meshes = {mesh};
    scene.objects.clear();
    const auto five = [](const Matrix &center) {
      std::vector<Matrix> result;
      for (float x : {-.5f, -.25f, 0.f, .25f, .5f}) {
        auto m = center;
        m[3] += x;
        result.push_back(m);
      }
      return result;
    };
    for (int index = 0; index < 4; ++index) {
      ObjectData object{"motion/instance" + std::to_string(index), mesh->key};
      object.transform[3] = index % 2 ? .9f : -.9f;
      object.transform[7] = index / 2 ? .5f : -.5f;
      object.motion = index == 2 ? five(object.transform) : linear_transform_motion(object.transform);
      scene.objects.push_back(object);
    }
    const auto reference = motion_wait(session, session.apply(scene));
    const auto conversions = session.status().geometry_conversions;
    const auto check = [&](const FrameEvent &frame, bool right_only) {
      motion_require(frame.planes.size() == reference->planes.size(), "Motion layout lost output planes");
      for (std::size_t pass = 0; pass < frame.planes.size(); ++pass) {
        const auto &plane = frame.planes[pass];
        const auto &expected = reference->planes[pass];
        motion_require(plane.pass == expected.pass, "Motion layout changed pass ordering");
        for (int y = 0; y < frame.height; ++y)
          for (int x = 0; x < frame.width; ++x)
            for (int c = 0; c < plane.channels; ++c) {
              const auto index = (y * frame.width + x) * plane.channels + c;
              const float actual = plane.pixels[index];
              motion_require(std::isfinite(actual), "Motion buffer layout produced non-finite pixels");
              if (!right_only || x >= frame.width / 2)
                motion_require(std::abs(actual - expected.pixels[index]) < .0001f,
                               "Motion buffer layout corrupted an unchanged instance");
            }
      }
      motion_require(session.status().geometry_conversions == conversions,
                     "Motion layout change reconverted shared geometry");
    };
    for (int repeat = 0; repeat < 6; ++repeat) {
      SceneUpdate edit;
      edit.objects = {scene.objects[0]};
      edit.objects[0].motion.clear();
      check(*motion_wait(session, session.apply(edit)), true);
      edit.objects = {scene.objects[0]};
      check(*motion_wait(session, session.apply(edit)), false);
      // Keep total allocation size unchanged but move the middle object's
      // offset by exchanging three/five-step arrays on its neighbors.
      edit.objects = {scene.objects[0], scene.objects[2]};
      edit.objects[0].motion = five(edit.objects[0].transform);
      edit.objects[1].motion = linear_transform_motion(edit.objects[1].transform);
      check(*motion_wait(session, session.apply(edit)), false);
      edit.objects = {scene.objects[0], scene.objects[2]};
      check(*motion_wait(session, session.apply(edit)), false);
    }
    std::cout << "{\"check\":\"Partial object motion updates preserve shared buffer offsets and finite "
                 "passes\",\"renders\":25,\"result\":\"passed\"}\n";
  }
  std::cout << "{\"result\":\"passed\",\"geometry\":\"point, hair, mesh\",\"checks\":\"deformation, transform, camera, "
               "radius, sample order, invalid correspondence, toggle, shutdown\"}\n";
}
} // namespace cycles_houdini::harness
