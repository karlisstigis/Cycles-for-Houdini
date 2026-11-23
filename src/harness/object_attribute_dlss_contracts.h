// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "instance_motion_contracts.h"
#include "../core/denoising.h"
#include <chrono>
#include <cmath>
#include <cstring>
#include <iostream>
#include <limits>
#include <thread>

namespace cycles_houdini::harness {
inline void object_attribute_dlss_contracts(const std::string &device) {
  int checks = 0, pairs = 0, rejected = 0;
  double maximum_rmse = 0;
  const auto require = [&](bool value, const std::string &message) {
    ++checks;
    if (!value)
      throw std::runtime_error("DLSS object attribute contract: " + message);
  };
  const auto same_transform = [](const ccl::Transform &a, const ccl::Transform &b) {
    return std::memcmp(&a, &b, sizeof(a)) == 0;
  };
  const auto first = ccl::transform_translate(ccl::make_float3(1, 2, 3));
  const auto second = ccl::transform_translate(ccl::make_float3(4, 5, 6));
  const auto third = ccl::transform_translate(ccl::make_float3(7, 8, 9));
  auto history = denoising_motion_history({}, first);
  require(history.size() == 2 && same_transform(history[0], first) && same_transform(history[1], first),
          "initial motion history must repeat the current pose");
  history[1] = second;
  const auto replaced = denoising_motion_history(history, third);
  require(same_transform(replaced[0], first) && same_transform(replaced[1], third) &&
              same_transform(history[1], second),
          "replacement reused the stale second sample or changed accepted history");
  history[0] = second; // Simulate the engine's completed evaluation advancing the previous sample.
  const auto advanced = denoising_motion_history(history, third);
  require(same_transform(advanced[0], second) && same_transform(advanced[1], third),
          "motion history discarded the engine-advanced previous sample");
  SessionConfig config;
  config.device_id = device;
  config.width = 96;
  config.height = 64;
  config.samples = 32;
  config.threads = 2;
  config.interactive = true;
  config.persistent_updates = true;
  config.collect_diagnostics = true;
  config.denoising.enabled = true;
  config.denoising.method = "dlss";
  config.denoising.start_sample = 1;
  config.passes = {"combined", "position", "depth"};
  const auto wait = [&](RenderSession &session, std::uint64_t generation) {
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(45);
    for (;;) {
      const auto status = session.status();
      if (status.state == SessionState::Failed)
        throw std::runtime_error(status.error);
      const auto frame = session.frame();
      if (frame && frame->generation == generation && frame->complete)
        return frame;
      require(std::chrono::steady_clock::now() < deadline, "render timed out");
      std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }
  };
  const auto fields_only = [](const SceneUpdate &scene) {
    SceneUpdate update;
    for (const auto &object : scene.objects)
      update.object_attributes.push_back({object.key, object.attributes});
    return update;
  };
  const auto full_objects = [](const SceneUpdate &scene) {
    SceneUpdate update;
    update.objects = scene.objects;
    return update;
  };
  const auto combined = [&](const SceneUpdate &scene) {
    auto update = fields_only(scene);
    for (std::size_t index = 0; index < scene.objects.size(); ++index) {
      const auto &object = scene.objects[index];
      update.object_attributes[index].pose =
          ObjectPoseData{object.key, object.transform, object.motion, object.nominal_transform};
    }
    return update;
  };
  std::shared_ptr<const FrameEvent> previous;
  const auto compare = [&](RenderSession &candidate, RenderSession &oracle, const SceneUpdate &compact,
                           const SceneUpdate &full, const std::string &label, std::uint64_t direct, bool dlss = true,
                           bool changed = true, std::uint64_t ordinary_updates = 0) {
    const auto before_updates = candidate.status().object_updates;
    const auto actual =
        wait(candidate, candidate.apply(decode_worker_update(encode_worker_update(WorkerUpdate{compact})).scene));
    const auto reference =
        wait(oracle, oracle.apply(decode_worker_update(encode_worker_update(WorkerUpdate{full})).scene));
    const auto status = candidate.status();
    require(status.denoiser == oracle.status().denoiser && (status.denoiser == "dlss") == dlss,
            label + ": wrong actual denoising backend");
    require(status.render_device_ids == std::vector<std::string>{device}, label + ": wrong actual render device");
    require(status.direct_attribute_updates == direct && oracle.status().direct_attribute_updates == 0,
            label + ": wrong direct/full path count");
    if (direct)
      require(status.object_updates == before_updates + direct + ordinary_updates,
              label + ": mixed or combined command updated objects twice");
    require(actual->planes.size() == reference->planes.size(), label + ": pass inventory differs");
    double error = 0, difference = 0;
    std::size_t count = 0;
    bool useful = false;
    for (const auto &plane : actual->planes) {
      const auto match = std::find_if(reference->planes.begin(), reference->planes.end(),
                                      [&](const auto &value) { return value.pass == plane.pass; });
      require(match != reference->planes.end() && match->pixels.size() == plane.pixels.size(),
              label + ": incompatible pass storage");
      const auto old = previous ? std::find_if(previous->planes.begin(), previous->planes.end(),
                                               [&](const auto &value) { return value.pass == plane.pass; })
                                : actual->planes.end();
      for (std::size_t index = 0; index < plane.pixels.size(); ++index) {
        require(std::isfinite(plane.pixels[index]) && std::isfinite(match->pixels[index]), label + ": nonfinite image");
        const auto delta = double(plane.pixels[index]) - match->pixels[index];
        error += delta * delta;
        ++count;
        if (plane.pass == "combined" && index % std::size_t(plane.channels) < 3) {
          useful |= plane.pixels[index] > .02f;
          if (previous && old != previous->planes.end() && old->pixels.size() == plane.pixels.size())
            difference += std::abs(double(plane.pixels[index]) - old->pixels[index]);
        }
      }
    }
    const double rmse = std::sqrt(error / std::max<std::size_t>(1, count));
    maximum_rmse = std::max(maximum_rmse, rmse);
    require(rmse < .0002,
            label + ": differs from full-update session with the same DLSS history: " + std::to_string(rmse));
    require(useful, label + ": blank RGB image");
    if (previous && changed)
      require(difference > .001, label + ": expected edit did not change RGB");
    previous = actual;
    ++pairs;
  };
  auto scene = instance_field_scene(0, {{"Cd", 3, {.2f, .4f, .8f}}, {"Cd", 3, {.7f, .3f, .1f}}}, 0, "dlss-attributes",
                                    false, false);
  RenderSession candidate(config), oracle(config);
  compare(candidate, oracle, scene, scene, "initial DLSS history", 0, true, false);
  SceneUpdate pose;
  for (auto &object : scene.objects) {
    object.transform[3] += .15f;
    object.transform[7] += .1f;
    pose.object_poses.push_back({object.key, object.transform});
  }
  compare(candidate, oracle, pose, full_objects(scene), "pose before attribute update", 0);
  scene.objects[0].attributes[0].value = {.8f, .2f, .1f};
  scene.objects[1].attributes[0].value = {.1f, .7f, .3f};
  compare(candidate, oracle, fields_only(scene), full_objects(scene), "attribute after pose", 2);
  for (auto &object : scene.objects) {
    object.transform[3] += .1f;
    object.attributes[0].value.z += .15f;
  }
  compare(candidate, oracle, combined(scene), full_objects(scene), "combined attribute and pose history", 2);
  scene.objects[0].transform[7] += .12f;
  compare(candidate, oracle, combined(scene), full_objects(scene), "same attributes with attached pose history", 2);
  const auto before_reject = candidate.status();
  const auto before_image = candidate.frame();
  for (bool nested_key : {false, true}) {
    auto invalid = combined(scene);
    if (nested_key)
      invalid.object_attributes[0].pose->key += "/invalid";
    else
      invalid.object_attributes[0].attributes[0].value.x = std::numeric_limits<float>::quiet_NaN();
    bool refused = false;
    try {
      candidate.apply(invalid, true);
    } catch (const SceneUpdateRejected &) {
      refused = true;
    }
    require(refused && candidate.status().generation == before_reject.generation && candidate.frame() == before_image &&
                candidate.status().direct_attribute_updates == 2 &&
                candidate.status().object_updates == before_reject.object_updates &&
                candidate.status().object_resources == before_reject.object_resources &&
                candidate.status().owned_geometry_bytes == before_reject.owned_geometry_bytes,
            "rejected direct fields/pose mutated image, generation, resources or accepted metric");
    ++rejected;
  }
  require(candidate.apply(fields_only(scene), true) == before_reject.generation && candidate.frame() == before_image &&
              candidate.status().direct_attribute_updates == 0 &&
              candidate.status().object_updates == before_reject.object_updates,
          "identical fields advanced DLSS history/image/generation or retained a stale direct metric");
  require(candidate.apply(combined(scene), true) == before_reject.generation && candidate.frame() == before_image &&
              candidate.status().object_updates == before_reject.object_updates,
          "identical combined fields and pose advanced DLSS history");
  scene.camera->transform[3] += .12f;
  SceneUpdate camera;
  camera.camera = scene.camera;
  compare(candidate, oracle, camera, camera, "camera after rejected and unchanged attributes", 0);
  scene.objects[0].attributes[0].value = {.4f, .7f, .2f};
  compare(candidate, oracle, fields_only(scene), full_objects(scene), "attribute after camera", 2);
  // Retain the paired full-update oracle's exact DLSS history while other
  // scene commands coexist with one attached field/pose command.
  LightData moving_light;
  moving_light.key = "dlss/moving-light";
  moving_light.transform[11] = 2;
  scene.lights = {moving_light};
  for (int command = 0; command < 3; ++command) {
    scene.objects[0].attributes[0].value.x += .08f;
    scene.objects[0].transform[7] += .04f;
    SceneUpdate compact;
    const auto &target = scene.objects[0];
    compact.object_attributes = {{target.key, target.attributes, ObjectPoseData{target.key, target.transform}}};
    scene.lights[0].transform[3] += .3f;
    compact.lights = scene.lights;
    if (command == 0) {
      scene.objects[1].transform[3] -= .1f;
      compact.object_poses = {{scene.objects[1].key, scene.objects[1].transform}};
    } else if (command == 1) {
      auto added = scene.objects[1];
      added.key = "dlss/disjoint-added";
      added.transform[7] += .35f;
      scene.objects.push_back(added);
      compact.objects = {added};
    } else {
      compact.deleted_objects = {scene.objects.back().key};
      scene.objects.pop_back();
    }
    auto full = full_objects(scene);
    full.lights = scene.lights;
    full.deleted_objects = compact.deleted_objects;
    compare(candidate, oracle, compact, full, "mixed DLSS light/disjoint command " + std::to_string(command), 1, true,
            true, command == 2 ? 0 : 1);
  }
  scene.objects[0].attributes[0].value.z += .1f;
  scene.objects[1].attributes[0].value.y += .1f;
  scene.objects[1].nominal_transform = scene.objects[1].transform;
  scene.objects[1].nominal_transform->at(7) += .05f;
  compare(candidate, oracle, combined(scene), full_objects(scene), "DLSS per-record nominal fallback", 1, true, true,
          1);
  require(candidate.status().expanded_attribute_updates == 1 && !candidate.status().attribute_fallback_reason.empty(),
          "DLSS partition lost normal-use fallback diagnostics");
  const auto mixed_status = candidate.status();
  const auto mixed_frame = candidate.frame();
  auto invalid_mixed = combined(scene);
  invalid_mixed.object_attributes[0].attributes[0].value.x += .1f;
  invalid_mixed.object_attributes[1].pose->transform[0] = std::numeric_limits<float>::quiet_NaN();
  invalid_mixed.lights = scene.lights;
  invalid_mixed.lights[0].transform[7] += .4f;
  bool mixed_refused = false;
  try {
    candidate.apply(invalid_mixed, true);
  } catch (const SceneUpdateRejected &) {
    mixed_refused = true;
  }
  require(mixed_refused && candidate.frame() == mixed_frame &&
              candidate.status().generation == mixed_status.generation &&
              candidate.status().object_updates == mixed_status.object_updates &&
              candidate.status().direct_attribute_updates == mixed_status.direct_attribute_updates &&
              candidate.status().expanded_attribute_updates == mixed_status.expanded_attribute_updates &&
              candidate.status().attribute_fallback_count == mixed_status.attribute_fallback_count &&
              candidate.status().attribute_fallback_reason == mixed_status.attribute_fallback_reason,
          "mixed rejection changed DLSS image/history or accepted diagnostics");
  ++rejected;
  scene.camera->transform[3] += .05f;
  camera.camera = scene.camera;
  compare(candidate, oracle, camera, camera, "DLSS history after mixed rejection", 0);
  // Restore ordinary poses before the explicit sampled-backend policy cases.
  scene.objects[1].nominal_transform.reset();
  candidate.close();
  oracle.close();
  require(candidate.status().owned_geometry_bytes == 0 && oracle.status().owned_geometry_bytes == 0,
          "DLSS sessions retained geometry after close");
  for (bool blur : {false, true}) {
    auto conventional = config;
    if (!blur)
      conventional.passes.push_back("motion");
    RenderSession fallback(conventional), full(conventional);
    auto sampled = scene;
    sampled.motion_blur = blur;
    previous.reset();
    compare(fallback, full, sampled, sampled, blur ? "shutter blur backend fallback" : "motion pass backend fallback",
            0, false, false);
    const auto fallback_status = fallback.status();
    require(std::any_of(fallback_status.source_diagnostics.begin(), fallback_status.source_diagnostics.end(),
                        [](const auto &issue) { return issue.code == "render.dlss_motion"; }),
            "explicit motion fallback lost its diagnostic");
    sampled.objects[0].attributes[0].value = {.9f, .1f, .6f};
    compare(fallback, full, fields_only(sampled), full_objects(sampled), "sampled policy attribute fallback", 0, false);
    fallback.close();
    full.close();
    require(fallback.status().owned_geometry_bytes == 0 && full.status().owned_geometry_bytes == 0,
            "sampled fallback sessions retained geometry");
  }
  std::cout << "{\"object_attribute_dlss_contracts\":true,\"checks\":" << checks
            << ",\"paired_full_references\":" << pairs << ",\"rejected\":" << rejected
            << ",\"maximum_rgba_position_depth_rmse\":" << maximum_rmse << ",\"actual_backend\":\"dlss\"}\n";
}
} // namespace cycles_houdini::harness
