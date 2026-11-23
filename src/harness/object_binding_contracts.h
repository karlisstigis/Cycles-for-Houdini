// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <thread>

namespace cycles_houdini::harness {
inline void object_binding_contracts(const std::string &device) {
  const auto require = [](bool value, const char *message) {
    if (!value)
      throw std::runtime_error(message);
  };
  const auto wait = [&](RenderSession &session, std::uint64_t generation) {
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(30);
    while (true) {
      const auto status = session.status();
      if (status.state == SessionState::Failed)
        throw std::runtime_error(status.error);
      const auto frame = session.frame();
      if (frame && frame->generation == generation && frame->complete)
        return frame;
      require(std::chrono::steady_clock::now() < deadline, "Object binding render did not finish");
      std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }
  };
  const auto depth = [&](const std::shared_ptr<const FrameEvent> &frame) -> const std::vector<float> & {
    require(bool(frame), "Missing accepted image");
    const auto plane =
        std::find_if(frame->planes.begin(), frame->planes.end(), [](const auto &p) { return p.pass == "depth"; });
    require(plane != frame->planes.end(), "Missing independent depth pass");
    return plane->pixels;
  };
  const auto visible = [&](const auto &frame) {
    const auto &values = depth(frame);
    return std::count_if(values.begin(), values.end(), [](float d) { return std::isfinite(d) && d > 0 && d < 10; }) >
           20;
  };
  int rejected = 0, accepted = 0, references = 0;
  for (bool persistent : {false, true}) {
    SessionConfig config;
    config.device_id = device;
    config.width = 96;
    config.height = 64;
    config.samples = 4;
    config.interactive = false;
    config.persistent_updates = persistent;
    config.passes = {"combined", "depth"};
    RenderSession session(config);
    auto initial = calibration_fixture();
    const auto geometry = initial.meshes.front()->key;
    auto a = initial.objects.front();
    a.key = "binding/a";
    auto b = a;
    b.key = "binding/b";
    b.transform[3] = .15f;
    auto c = a;
    c.key = "binding/c";
    c.transform[3] = -.2f;
    initial.objects = {b, c, a}; // Deliberately unsorted resource keys.
    const auto submit = [&](const SceneUpdate &update) {
      auto frame = wait(session, session.apply(update));
      ++accepted;
      return frame;
    };
    require(visible(submit(initial)), "Initial unsorted objects disappeared");
    const auto conversions = session.status().geometry_conversions, objects = session.status().object_updates;
    SceneUpdate camera;
    camera.camera = initial.camera;
    camera.camera->transform[3] += .2f;
    require(visible(submit(camera)), "Camera-only update lost objects");
    camera.camera = initial.camera;
    submit(camera);
    require(session.status().geometry_conversions == conversions && session.status().object_updates == objects,
            "Camera validation converted geometry or rewrote object resources");
    const auto invalid = [&](const SceneUpdate &update) {
      const auto before = session.status();
      const auto image = session.frame();
      bool refused = false;
      try {
        session.apply(update);
      } catch (const SceneUpdateRejected &) {
        refused = true;
      }
      const auto after = session.status();
      require(refused && after.state != SessionState::Failed && after.generation == before.generation,
              "Unsafe ownership candidate was accepted or poisoned the render");
      require(after.geometry_resources == before.geometry_resources &&
                  after.object_resources == before.object_resources && depth(session.frame()) == depth(image),
              "Rejected ownership candidate changed accepted resources/image");
      ++rejected;
    };
    SceneUpdate bad;
    bad.objects = {a, a};
    invalid(bad);
    bad = {};
    bad.deleted_objects = {a.key, a.key};
    invalid(bad);
    bad = {};
    auto empty = a;
    empty.key.clear();
    bad.objects = {empty};
    invalid(bad);
    bad = {};
    bad.objects = {a};
    bad.deleted_objects = {a.key};
    invalid(bad);
    bad = {};
    auto missing = a;
    missing.geometry_key = "missing/geometry";
    bad.objects = {missing};
    invalid(bad);
    bad = {};
    auto singular = a;
    singular.transform[0] = 0;
    bad.objects = {singular};
    invalid(bad);
    bad = {};
    auto nonfinite = a;
    nonfinite.transform[3] = std::numeric_limits<float>::quiet_NaN();
    bad.objects = {nonfinite};
    invalid(bad);
    bad = {};
    bad.deleted_geometry = {geometry};
    invalid(bad);
    bad = {};
    bad.deleted_materials = {initial.materials.front().key};
    invalid(bad);
    bad = {};
    auto properties = a;
    properties.properties.ao_distance = -1;
    bad.objects = {properties};
    invalid(bad);
    bad = {};
    auto points = std::make_shared<PointData>();
    points->key = geometry;
    bad.points = {points};
    invalid(bad);
    bad = {};
    bad.materials = {initial.materials.front(), initial.materials.front()};
    invalid(bad);
    bad = {};
    bad.lights = {initial.lights.front(), initial.lights.front()};
    invalid(bad);
    bad = {};
    bad.meshes.emplace_back();
    invalid(bad);
    bad = {};
    bad.volumes.emplace_back();
    invalid(bad);
    bad = {};
    bad.points.emplace_back();
    invalid(bad);
    bad = {};
    bad.curves.emplace_back();
    invalid(bad);

    auto replacement = std::make_shared<MeshData>(*initial.meshes.front());
    replacement->key = "binding/replacement";
    auto next_b = b;
    next_b.geometry_key = replacement->key;
    auto next_c = c;
    next_c.geometry_key = replacement->key;
    bad = {};
    bad.meshes = {replacement};
    bad.objects = {next_b};
    bad.deleted_geometry = {geometry};
    invalid(bad);
    // All old users are either removed or rebound in this single transaction.
    // This must work without an update order dependency or a dangling old key.
    SceneUpdate rebind;
    rebind.meshes = {replacement};
    rebind.objects = {next_c, next_b};
    rebind.deleted_objects = {a.key};
    rebind.deleted_geometry = {geometry};
    require(visible(submit(rebind)), "Rebinding and deleting old geometry lost valid objects");
    require(session.status().object_resources == 2 && session.status().geometry_resources == 1,
            "Replacement ownership is incorrect");
    bad = {};
    bad.deleted_objects = {b.key};
    bad.deleted_geometry = {replacement->key};
    invalid(bad);

    auto unused = std::make_shared<MeshData>(*replacement);
    unused->key = "binding/unused";
    SceneUpdate add;
    add.meshes = {unused};
    require(visible(submit(add)), "Unbound geometry addition lost retained users");
    SceneUpdate remove;
    remove.deleted_geometry = {unused->key};
    require(visible(submit(remove)), "Unbound deletion lost retained users");
    remove.deleted_geometry = {replacement->key};
    remove.deleted_objects = {b.key, c.key};
    require(!visible(submit(remove)), "Removed geometry still appears in the depth pass");
    require(session.status().object_resources == 0 && session.status().geometry_resources == 0,
            "Removed resources remain owned");
    remove = {};
    remove.deleted_objects = {"already/absent"};
    submit(remove);
    auto final = initial;
    final.meshes = {replacement};
    final.objects = {next_c, next_b};
    const auto actual = submit(final);
    require(visible(actual), "Recreated objects disappeared");
    session.close();
    require(session.status().owned_geometry_bytes == 0, "Closed validation session retains geometry");

    config.persistent_updates = false;
    RenderSession reference(config);
    const auto expected = wait(reference, reference.apply(final));
    require(depth(actual).size() == depth(expected).size(), "Reference dimensions differ");
    for (std::size_t n = 0; n < depth(actual).size(); ++n)
      require(std::abs(depth(actual)[n] - depth(expected)[n]) < .0001f,
              "Rebound geometry differs from fresh depth reference");
    ++references;
    reference.close();
  }
  std::cout << "{\"result\":\"passed\",\"modes\":2,\"safe_rejections\":" << rejected
            << ",\"accepted_updates\":" << accepted << ",\"depth_reference_comparisons\":" << references << "}\n";
}
} // namespace cycles_houdini::harness
