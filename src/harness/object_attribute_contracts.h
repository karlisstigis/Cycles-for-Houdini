// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "instance_motion_contracts.h"
#include "volume_fixture.h"
#include "cycles_houdini/snapshot.h"
#include <bit>
#include <chrono>
#include <cmath>
#include <cstring>
#include <iostream>
#include <limits>
#include <map>
#include <thread>

namespace cycles_houdini::harness {
inline void object_attribute_contracts(const std::string &device) {
  int checks = 0, rejected = 0, references = 0;
  float maximum_error = 0;
  std::size_t maximum_owned_bytes = 0, maximum_device_bytes = 0;
  const auto require = [&](bool value, const std::string &message) {
    ++checks;
    if (!value)
      throw std::runtime_error("Object attribute contract: " + message);
  };
  const auto fields = [](float value) {
    UniformAttribute temporal{"sampled", 1, {value, 0, 0}};
    temporal.motion_values = {{value * .5f, 0, 0}, {value, 0, 0}, {value * 1.5f, 0, 0}};
    return std::vector<UniformAttribute>{{"Cd", 3, {value, .2f, .8f}},
                                         {"index", 1, {value, 0, 0}},
                                         {"latent/\xc3\xbc", 1, {value * .3f, 0, 0}},
                                         temporal};
  };
  // Independent metadata-rich ownership fixture: the compact command must not
  // repeat binding/property strings, yet replay every authored field exactly.
  SceneUpdate initial;
  for (int n = 0; n < 256; ++n) {
    ObjectData object;
    object.key = "attributes/object/" + std::to_string(n);
    object.geometry_key = "attributes/prototype/" + std::string(96, 'g');
    object.properties.cryptomatte_object = "object/" + std::string(256, 'o') + std::to_string(n);
    object.properties.cryptomatte_asset = "asset/" + std::string(256, 'a');
    object.properties.linking_id = "link/" + std::string(256, 'l');
    object.properties.dielectric_priority = n % 7;
    object.random_id = n;
    object.transform[3] = float(n) * .01f;
    object.nominal_transform = object.transform;
    object.nominal_transform->at(7) = .125f;
    object.motion = {object.transform, object.transform, object.transform};
    object.attributes = fields(.25f);
    initial.objects.push_back(std::move(object));
  }
  const auto baseline = snapshot_state({}, initial);
  auto full = initial;
  for (auto &object : full.objects)
    object.attributes = fields(.75f);
  const auto desired = snapshot_state(baseline, full);
  const auto delta = snapshot_difference(baseline.get(), *desired);
  require(delta.objects.empty() && delta.object_poses.empty() && delta.object_attributes.size() == 256,
          "value edits did not select attributes-only transport");
  const auto packet = encode_worker_update(WorkerUpdate{delta});
  const auto full_bytes = encode_worker_update(WorkerUpdate{full}).size();
  require(packet.size() * 2 < full_bytes, "compact packet retained repeated model/binding data");
  const auto decoded = decode_worker_update(packet);
  require(decoded.scene.object_attributes == delta.object_attributes, "wire roundtrip changed complete fields");
  require(std::all_of(decoded.scene.object_attributes.begin(), decoded.scene.object_attributes.end(),
                      [](const auto &value) { return !value.pose; }),
          "ordinary two-field attribute records acquired an attached pose");
  auto obsolete_packet = packet;
  const std::uint32_t obsolete_version = 74;
  std::memcpy(obsolete_packet.data() + sizeof(std::uint32_t), &obsolete_version, sizeof(obsolete_version));
  bool obsolete_refused = false;
  try {
    decode_worker_update(obsolete_packet);
  } catch (const std::invalid_argument &) {
    obsolete_refused = true;
  }
  require(obsolete_refused, "protocol74 packet was accepted after optional-pose wire revision");
  ++rejected;
  const std::uint32_t previous_version = 75;
  std::memcpy(obsolete_packet.data() + sizeof(std::uint32_t), &previous_version, sizeof(previous_version));
  obsolete_refused = false;
  try {
    decode_worker_update(obsolete_packet);
  } catch (const std::invalid_argument &) {
    obsolete_refused = true;
  }
  require(obsolete_refused, "protocol75 packet was accepted after attribute diagnostics wire revision");
  ++rejected;
  require(*snapshot_state(baseline, decoded.scene)->objects == *desired->objects,
          "compact replay differs from full authored object state");
  require(baseline->objects->begin()->second.attributes == fields(.25f), "attribute replay mutated old snapshot");
  auto before = initial.objects.front(), after = before;
  after.attributes.clear();
  require(classify_object_update(before, after) == ObjectUpdateKind::Attributes,
          "public classifier must support complete field removal");
  SceneUpdate removed;
  removed.object_attributes = {{before.key, {}}};
  auto removed_state = snapshot_state(baseline, removed);
  require(removed_state->objects->at(before.key).attributes.empty() &&
              removed_state->objects->at(before.key).properties == before.properties,
          "empty replacement did not clear only the fields");
  after = before;
  after.attributes.push_back({"new-layout", 2, {.2f, .7f, 0}});
  require(classify_object_update(before, after) == ObjectUpdateKind::Attributes,
          "raw complete replacement unexpectedly rejects a new layout");
  after = before;
  after.attributes.back().motion_values.clear();
  after.attributes.back().motion_complete = false;
  require(classify_object_update(before, after) == ObjectUpdateKind::Attributes,
          "public attribute classifier dropped motion recovery policy");
  SceneUpdate policy;
  policy.object_attributes = {{after.key, after.attributes}};
  require(decode_worker_update(encode_worker_update(WorkerUpdate{policy})).scene.object_attributes ==
              policy.object_attributes,
          "complete recovery policy changed in transport");
  policy.object_attributes[0].attributes.back().motion_values.assign(129, Float3{.2f, 0, 0});
  policy.object_attributes[0].attributes.back().motion_complete = true;
  require(decode_worker_update(encode_worker_update(WorkerUpdate{policy})).scene.object_attributes ==
              policy.object_attributes,
          "maximum valid temporal field changed in transport");
  for (int kind = 0; kind < 9; ++kind) {
    after = before;
    after.attributes[1].value.x = .9f;
    if (kind == 0)
      after.transform[3] += .1f;
    else if (kind == 1)
      after.motion[0][7] += .1f;
    else if (kind == 2)
      after.nominal_transform.reset();
    else if (kind == 3)
      after.geometry_key += "/new";
    else if (kind == 4)
      after.properties.pass_index = 5;
    else if (kind == 5)
      ++after.random_id;
    else if (kind == 6)
      after.visibility = 0;
    else if (kind == 7)
      after.key += "/new";
    else
      after.properties.dielectric_priority += 2;
    require(classify_object_update(before, after) ==
                (kind < 3 ? ObjectUpdateKind::PoseAttributes : ObjectUpdateKind::Full),
            "mixed pose/model/binding edit selected the wrong transport");
  }
  after = before;
  after.attributes = fields(.8f);
  after.transform[3] += .5f;
  after.motion.clear();
  after.nominal_transform.reset();
  SceneUpdate combined_full;
  combined_full.objects = {after};
  const auto combined_state = snapshot_state(baseline, combined_full);
  const auto combined_delta = snapshot_difference(baseline.get(), *combined_state);
  require(combined_delta.objects.empty() && combined_delta.object_poses.empty() &&
              combined_delta.object_attributes.size() == 1 && combined_delta.object_attributes[0].pose &&
              combined_delta.object_attributes[0].pose->key == after.key,
          "mixed values and pose did not share one complete command");
  require(*snapshot_state(baseline, decode_worker_update(encode_worker_update(WorkerUpdate{combined_delta})).scene)
                  ->objects == *combined_state->objects,
          "combined wire replay lost complete pose/field ownership or clearing");
  after = before;
  after.attributes[1].value.x = std::bit_cast<float>(std::uint32_t{0x80000000u});
  SceneUpdate bits;
  bits.object_attributes = {{after.key, after.attributes}};
  const auto roundtrip_bits = decode_worker_update(encode_worker_update(WorkerUpdate{bits}));
  require(std::bit_cast<std::uint32_t>(roundtrip_bits.scene.object_attributes[0].attributes[1].value.x) == 0x80000000u,
          "attribute transport canonicalized signed zero");
  const auto invalid_snapshot = [&](SceneUpdate candidate) {
    bool refused = false;
    try {
      snapshot_state(baseline, candidate);
    } catch (const std::invalid_argument &) {
      refused = true;
    }
    require(refused && baseline->objects->at(before.key) == before, "invalid snapshot changed accepted ownership");
    ++rejected;
  };
  SceneUpdate conflict;
  conflict.object_attributes = {{before.key, fields(.8f)}, {before.key, fields(.9f)}};
  invalid_snapshot(conflict);
  conflict.object_attributes.resize(1);
  conflict.objects = {before};
  invalid_snapshot(conflict);
  conflict.objects.clear();
  conflict.object_poses = {{before.key}};
  invalid_snapshot(conflict);
  conflict.object_poses.clear();
  conflict.deleted_objects = {before.key};
  invalid_snapshot(conflict);
  conflict.deleted_objects.clear();
  conflict.object_attributes[0].key = "missing/object";
  invalid_snapshot(conflict);
  conflict.object_attributes[0].key.clear();
  invalid_snapshot(conflict);
  conflict = combined_delta;
  conflict.object_attributes[0].pose->key.clear();
  invalid_snapshot(conflict);
  conflict.object_attributes[0].pose->key = "mismatched/nested/key";
  invalid_snapshot(conflict);

  SessionConfig config;
  config.device_id = device;
  config.width = 48;
  config.height = 32;
  config.samples = 4;
  config.threads = 2;
  config.interactive = false;
  config.persistent_updates = true;
  config.collect_diagnostics = true;
  config.passes = {"combined", "position", "depth"};
  const auto wait = [&](RenderSession &session, std::uint64_t generation) {
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(45);
    while (true) {
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
  std::map<RenderSession *, std::shared_ptr<const FrameEvent>> previous_images;
  const auto compare = [&](RenderSession &session, const SceneUpdate &candidate, const SceneUpdate &expected,
                           const std::string &label, bool changed = false, std::uint64_t direct_updates = 0) {
    // Every tested command crosses the actual versioned worker serialization.
    const auto update = decode_worker_update(encode_worker_update(WorkerUpdate{candidate})).scene;
    const auto actual = wait(session, session.apply(update));
    require(session.status().direct_attribute_updates == direct_updates,
            label + ": unexpected direct attribute path count");
    RenderSession fresh(config);
    const auto reference = wait(fresh, fresh.apply(expected));
    require(actual->planes.size() == reference->planes.size(), label + ": pass inventory");
    double error = 0;
    std::size_t count = 0;
    for (const auto &plane : actual->planes) {
      const auto match = std::find_if(reference->planes.begin(), reference->planes.end(),
                                      [&](const auto &other) { return plane.pass == other.pass; });
      require(match != reference->planes.end() && plane.pixels.size() == match->pixels.size(),
              label + ": incompatible plane size");
      for (std::size_t i = 0; i < plane.pixels.size(); ++i) {
        require(std::isfinite(plane.pixels[i]) && std::isfinite(match->pixels[i]), label + ": nonfinite pixels");
        const double difference = double(plane.pixels[i]) - match->pixels[i];
        error += difference * difference;
        ++count;
      }
      if (plane.pass == "combined") {
        bool useful = false;
        for (std::size_t i = 0; i < plane.pixels.size(); ++i)
          useful |= i % std::size_t(plane.channels) < 3 && plane.pixels[i] > .02f;
        require(useful, label + ": empty RGB image");
      }
    }
    const auto rmse = float(std::sqrt(error / std::max<std::size_t>(1, count)));
    maximum_error = std::max(maximum_error, rmse);
    require(rmse < .00001f, label + ": differs from independent fresh full-object capture");
    if (changed) {
      const auto previous = previous_images.at(&session);
      double difference = 0;
      for (std::size_t p = 0; p < actual->planes.size(); ++p)
        for (std::size_t i = 0; i < actual->planes[p].pixels.size(); ++i)
          difference += std::abs(actual->planes[p].pixels[i] - previous->planes[p].pixels[i]);
      require(difference > .001, label + ": changed field produced no changed RGB/position/depth");
    }
    previous_images[&session] = actual;
    ++references;
    maximum_owned_bytes = std::max(maximum_owned_bytes, session.status().owned_geometry_bytes);
    maximum_device_bytes = std::max(maximum_device_bytes, session.status().device_allocation_peak_bytes);
    fresh.close();
    require(fresh.status().owned_geometry_bytes == 0, "fresh reference retained owned fields after close");
    return actual;
  };
  const auto patches = [](const SceneUpdate &scene) {
    SceneUpdate result;
    for (const auto &object : scene.objects)
      result.object_attributes.push_back({object.key, object.attributes});
    return result;
  };
  auto scene = instance_field_scene(0, {{"Cd", 3, {.2f, .4f, .8f}}, {"Cd", 3, {.7f, .3f, .1f}}}, 0,
                                    "attribute-contract", false, false);
  // Each object owns values the current shader does not request. They must
  // survive the compact update and become visible after a later shader edit.
  for (std::size_t i = 0; i < scene.objects.size(); ++i) {
    auto &object = scene.objects[i];
    object.properties.dielectric_priority = int(i) + 3;
    object.attributes.push_back({"index", 1, {.25f, 0, 0}});
    object.attributes.push_back({"latent", 1, {.1f, 0, 0}});
    object.attributes.push_back({dielectric_priority_attribute, 1, {float(i + 7), 0, 0}});
  }
  auto source = std::make_shared<MeshData>(*scene.meshes[0]);
  source->instance_attributes["index"] = 1;
  source->instance_attributes["latent"] = 1;
  scene.meshes = {source};
  scene.rendering = RenderSettings{};
  scene.rendering->samples = config.samples;
  scene.rendering->nested_dielectrics = true;
  RenderSession actual(config);
  compare(actual, scene, scene, "initial fields");
  const auto invalid = [&](const SceneUpdate &candidate, bool reuse = false) {
    const auto status = actual.status();
    const auto frame = actual.frame();
    bool refused = false;
    try {
      actual.apply(decode_worker_update(encode_worker_update(WorkerUpdate{candidate})).scene, reuse);
    } catch (const SceneUpdateRejected &) {
      refused = true;
    }
    require(refused && actual.status().generation == status.generation && actual.frame() == frame &&
                actual.status().object_resources == status.object_resources &&
                actual.status().object_updates == status.object_updates &&
                actual.status().geometry_resources == status.geometry_resources &&
                actual.status().owned_geometry_bytes == status.owned_geometry_bytes &&
                actual.status().direct_attribute_updates == status.direct_attribute_updates,
            "invalid attribute candidate changed generation, resources or image");
    ++rejected;
  };
  SceneUpdate bad = patches(scene);
  bad.object_attributes.push_back(bad.object_attributes[0]);
  invalid(bad);
  bad = patches(scene);
  bad.object_attributes[0].key = "missing/object";
  invalid(bad);
  bad = patches(scene);
  bad.objects = {scene.objects[0]};
  invalid(bad);
  bad = patches(scene);
  bad.object_poses = {{scene.objects[0].key}};
  invalid(bad);
  bad = patches(scene);
  bad.deleted_objects = {scene.objects[0].key};
  invalid(bad);
  bad = patches(scene);
  bad.object_attributes[0].attributes.clear();
  invalid(bad); // Required candidate geometry fields cannot be removed alone.
  for (int issue = 0; issue < 8; ++issue) {
    bad = patches(scene);
    auto &attribute = bad.object_attributes[0].attributes[0];
    if (issue == 0)
      attribute.value.x = std::numeric_limits<float>::quiet_NaN();
    else if (issue == 1)
      attribute.components = 4;
    else if (issue == 2)
      attribute.name.clear();
    else if (issue == 3)
      bad.object_attributes[0].attributes.push_back(attribute);
    else if (issue == 4)
      attribute.motion_values.assign(2, Float3{.2f, .4f, .8f});
    else if (issue == 5) {
      attribute.motion_values.assign(3, Float3{.2f, .4f, .8f});
      attribute.motion_complete = false;
    } else if (issue == 6)
      attribute.motion_values.assign(131, Float3{.2f, .4f, .8f});
    else {
      attribute.motion_values.assign(3, Float3{.2f, .4f, .8f});
      attribute.motion_values[1].x = std::numeric_limits<float>::infinity();
    }
    invalid(bad);
  }
  const auto before_same = actual.status().generation;
  require(actual.apply(patches(scene), true) == before_same && actual.status().generation == before_same,
          "identical attribute vector restarted the physical renderer");
  require(actual.status().direct_attribute_updates == 0, "no-op retained a stale direct attribute count");
  scene.objects[0].attributes[0].value = {.8f, .2f, .1f};
  scene.objects[1].attributes[0].value = {.1f, .7f, .3f};
  compare(actual, patches(scene), scene, "Cd replacement", true, 2);
  bad = patches(scene);
  bad.object_attributes.push_back(bad.object_attributes[0]);
  invalid(bad); // A rejected candidate must preserve the accepted path metric.
  const auto direct_generation = actual.status().generation;
  require(actual.apply(patches(scene), true) == direct_generation && actual.status().generation == direct_generation &&
              actual.status().direct_attribute_updates == 0,
          "identical direct fields restarted rendering or retained an old path count");
  const auto combined_patches = [&](const SceneUpdate &authored) {
    auto update = patches(authored);
    for (std::size_t index = 0; index < authored.objects.size(); ++index) {
      const auto &object = authored.objects[index];
      update.object_attributes[index].pose =
          ObjectPoseData{object.key, object.transform, object.motion, object.nominal_transform};
    }
    return update;
  };
  for (int variant = 0; variant < 4; ++variant) {
    const float angle = variant ? .25f * variant : 0;
    const float sx = variant == 3 ? -1.1f : variant == 2 ? 1.15f : 1;
    const float sy = variant >= 2 ? .75f : 1;
    for (std::size_t index = 0; index < scene.objects.size(); ++index) {
      auto &object = scene.objects[index];
      object.transform = {std::cos(angle) * sx,
                          -std::sin(angle) * sy,
                          0,
                          index ? .7f : -.7f,
                          std::sin(angle) * sx,
                          std::cos(angle) * sy,
                          0,
                          .06f * (variant + 1),
                          0,
                          0,
                          variant >= 2 ? 1.4f : 1.f,
                          0};
      object.attributes[0].value.x = .25f + .1f * variant + .1f * float(index);
    }
    const auto updates = actual.status().object_updates;
    compare(actual, combined_patches(scene), scene, "combined transform variant " + std::to_string(variant), true, 2);
    require(actual.status().object_updates == updates + 2, "combined record updated pose/attributes more than once");
  }
  scene.objects[0].transform[3] += .08f;
  compare(actual, combined_patches(scene), scene, "attached pose with identical fields", true, 2);
  const auto combined_generation = actual.status().generation;
  const auto combined_image = actual.frame();
  const auto combined_updates = actual.status().object_updates;
  require(actual.apply(combined_patches(scene), true) == combined_generation && actual.frame() == combined_image &&
              actual.status().object_updates == combined_updates && actual.status().direct_attribute_updates == 0,
          "identical attached pose/fields changed physical image, history or object count");
  for (int issue = 0; issue < 8; ++issue) {
    bad = combined_patches(scene);
    auto &pose = *bad.object_attributes[0].pose;
    if (issue == 0)
      pose.key.clear();
    else if (issue == 1)
      pose.key += "/wrong";
    else if (issue == 2)
      pose.transform[0] = std::numeric_limits<float>::quiet_NaN();
    else if (issue == 3)
      pose.motion.assign(2, pose.transform);
    else if (issue == 4) {
      pose.nominal_transform = pose.transform;
      pose.nominal_transform->at(7) = std::numeric_limits<float>::infinity();
    } else if (issue == 5)
      bad.objects = {scene.objects[0]};
    else if (issue == 6)
      bad.object_poses = {pose};
    else
      bad.deleted_objects = {pose.key};
    invalid(bad, true);
  }
  for (auto &object : scene.objects) {
    object.motion = {object.transform, object.transform, object.transform};
    object.motion.front()[3] -= .1f;
    object.motion.back()[3] += .1f;
    object.nominal_transform = object.transform;
  }
  compare(actual, combined_patches(scene), scene, "combined authored motion and nominal fallback");
  for (auto &object : scene.objects) {
    object.motion.clear();
    object.nominal_transform.reset();
  }
  compare(actual, combined_patches(scene), scene, "complete attached pose clears retained motion and nominal");
  for (auto &object : scene.objects)
    object.nominal_transform = object.transform;
  compare(actual, combined_patches(scene), scene, "attached nominal pose falls back");
  for (auto &object : scene.objects)
    object.nominal_transform.reset();
  SceneUpdate clear_nominal;
  clear_nominal.objects = scene.objects;
  compare(actual, clear_nominal, scene, "full clear restores ordinary current pose");
  scene.objects[0].attributes[1].value.x = .8f;
  scene.objects[1].attributes[1].value.x = .3f;
  compare(actual, patches(scene), scene, "unused index replacement retained", false, 2);
  scene.materials[0].graph = instance_field_graph("index", 1);
  SceneUpdate shader;
  shader.materials = scene.materials;
  compare(actual, shader, scene, "later shader consumes retained index", true);
  scene.objects[0].attributes[2].value.x = .65f;
  scene.objects[1].attributes[2].value.x = .35f;
  compare(actual, patches(scene), scene, "latent unused replacement retained", false, 2);
  scene.materials[0].graph = instance_field_graph("latent", 1);
  shader.materials = scene.materials;
  compare(actual, shader, scene, "later shader consumes latent field", true);
  require(actual.status().dielectric_priority_min == 7 && actual.status().dielectric_priority_max == 8,
          "implicit priority field was not retained");
  for (auto &object : scene.objects)
    std::erase_if(object.attributes, [](const auto &field) { return field.name == dielectric_priority_attribute; });
  compare(actual, patches(scene), scene, "priority field removal");
  require(actual.status().dielectric_priority_min == 3 && actual.status().dielectric_priority_max == 4,
          "priority removal did not restore retained object property");
  // Validate against the complete candidate fields when a new declaration is
  // added. Checking retained fields against this layout would reject the valid
  // transaction, while skipping the candidate check would admit missing data.
  for (auto &object : scene.objects)
    object.attributes.push_back({"new_required", 2, {.2f, .7f, 0}});
  source = std::make_shared<MeshData>(*scene.meshes[0]);
  source->revision++;
  source->instance_attributes["new_required"] = 2;
  scene.meshes = {source};
  auto addition = patches(scene);
  addition.meshes = scene.meshes;
  auto missing_added_field = addition;
  missing_added_field.object_attributes[0].attributes.pop_back();
  invalid(missing_added_field);
  compare(actual, addition, scene, "candidate geometry adds required field");
  // Layout changes are legal raw replacements when the candidate geometry
  // declaration changes in the same transaction, and illegal without it.
  for (auto &object : scene.objects)
    object.attributes.erase(object.attributes.begin() + 1); // Remove index.
  source = std::make_shared<MeshData>(*scene.meshes[0]);
  source->revision++;
  source->instance_attributes.erase("index");
  scene.meshes = {source};
  auto layout = patches(scene);
  layout.meshes = scene.meshes;
  compare(actual, layout, scene, "candidate geometry layout plus removal");
  // Empty is a replacement, not a sparse no-op. Clear declarations too and
  // use a constant shader so the expected image remains independently useful.
  for (auto &object : scene.objects)
    object.attributes.clear();
  source = std::make_shared<MeshData>(*scene.meshes[0]);
  source->revision++;
  source->instance_attributes.clear();
  scene.meshes = {source};
  scene.materials[0].kind = MaterialKind::Emission;
  scene.materials[0].graph.reset();
  scene.materials[0].color = {.3f, .6f, .2f};
  auto clear = patches(scene);
  clear.meshes = scene.meshes;
  clear.materials = scene.materials;
  compare(actual, clear, scene, "empty complete replacement");
  actual.close();
  require(actual.status().owned_geometry_bytes == 0, "attribute state retained after close");

  // Sparse ordered attributes merge with interleaved pose keys. Exercise both
  // ends and the middle, and the unordered fallback, through real worker DTOs.
  {
    auto ordered_scene = instance_field_scene(0, {{"Cd", 3, {.2f, .4f, .8f}}, {"Cd", 3, {.7f, .3f, .1f}}}, 0,
                                              "attribute-merge", false, false);
    const auto prototype = ordered_scene.objects.front();
    ordered_scene.objects.clear();
    for (int n = 0; n < 7; ++n) {
      auto object = prototype;
      object.key = "merge/" + std::to_string(n);
      object.transform[3] = float(n - 3) * .25f;
      ordered_scene.objects.push_back(std::move(object));
    }
    RenderSession merge_session(config);
    compare(merge_session, ordered_scene, ordered_scene, "ordered merge initial");
    SceneUpdate sparse;
    for (int n = 0; n < 7; ++n) {
      auto &object = ordered_scene.objects[n];
      if (n == 0 || n == 3 || n == 6) {
        object.attributes[0].value.x += .1f;
        sparse.object_attributes.push_back({object.key, object.attributes});
      } else {
        object.transform[7] += .1f;
        sparse.object_poses.push_back({object.key, object.transform});
      }
    }
    compare(merge_session, sparse, ordered_scene, "ordered interleaved merge", false, 3);
    std::reverse(sparse.object_poses.begin(), sparse.object_poses.end());
    std::reverse(sparse.object_attributes.begin(), sparse.object_attributes.end());
    compare(merge_session, sparse, ordered_scene, "unordered interleaved merge", false, 3);
    const auto accepted = merge_session.status();
    const auto image = merge_session.frame();
    const auto refuse = [&](const SceneUpdate &candidate) {
      bool refused = false;
      try {
        merge_session.apply(decode_worker_update(encode_worker_update(WorkerUpdate{candidate})).scene);
      } catch (const SceneUpdateRejected &) {
        refused = true;
      }
      const auto status = merge_session.status();
      require(refused && status.generation == accepted.generation && merge_session.frame() == image &&
                  status.object_updates == accepted.object_updates &&
                  status.object_resources == accepted.object_resources &&
                  status.geometry_resources == accepted.geometry_resources &&
                  status.owned_geometry_bytes == accepted.owned_geometry_bytes &&
                  status.direct_attribute_updates == accepted.direct_attribute_updates &&
                  status.expanded_attribute_updates == accepted.expanded_attribute_updates,
              "interleaved merge rejection changed accepted image, model or diagnostics");
      ++rejected;
    };
    for (const int n : {0, 3, 6}) {
      auto conflict = sparse;
      conflict.object_poses.push_back({ordered_scene.objects[n].key, ordered_scene.objects[n].transform});
      std::sort(conflict.object_poses.begin(), conflict.object_poses.end(),
                [](const auto &a, const auto &b) { return a.key < b.key; });
      refuse(conflict);
      std::reverse(conflict.object_poses.begin(), conflict.object_poses.end());
      refuse(conflict);
      conflict = sparse;
      conflict.deleted_objects = {ordered_scene.objects[n].key};
      refuse(conflict);
    }
    auto invalid = sparse;
    invalid.object_poses.push_back(invalid.object_poses.front());
    refuse(invalid);
    std::sort(invalid.object_poses.begin(), invalid.object_poses.end(),
              [](const auto &a, const auto &b) { return a.key < b.key; });
    refuse(invalid);
    invalid = sparse;
    invalid.object_attributes.push_back(invalid.object_attributes.front());
    refuse(invalid);
    invalid = sparse;
    invalid.object_attributes.front().key = "merge/missing";
    refuse(invalid);
    invalid = sparse;
    invalid.object_poses.front().key = "merge/missing";
    refuse(invalid);
    invalid = sparse;
    invalid.deleted_objects = {invalid.object_poses.front().key};
    refuse(invalid);
    invalid = sparse;
    invalid.object_attributes.front().attributes[0].value.x = std::numeric_limits<float>::quiet_NaN();
    refuse(invalid);
    invalid = sparse;
    invalid.object_poses.front().transform[0] = std::numeric_limits<float>::quiet_NaN();
    refuse(invalid);
    merge_session.close();
  }

  // Direct fields borrow retained records while the ordinary commands mutate
  // the same object map. Exercise insertion, erasure and poses on disjoint keys
  // sharing that prototype, with a genuinely animated light in the transaction.
  auto mixed = instance_field_scene(0, {{"Cd", 3, {.2f, .4f, .8f}}, {"Cd", 3, {.7f, .3f, .1f}}}, 0, "attribute-mixed",
                                    false, false);
  mixed.materials[0].graph->nodes[1].definition = "cycles/diffuse_bsdf/1";
  mixed.materials[0].graph->nodes[1].parameters = {{"roughness", 0.f}};
  mixed.materials[0].graph->surface = {"surface", "BSDF"};
  auto extra = mixed.objects[0];
  extra.key = "mixed/disjoint";
  extra.transform[7] = .5f;
  mixed.objects.push_back(extra);
  LightData moving_light;
  moving_light.key = "mixed/light";
  moving_light.transform[11] = 2;
  mixed.lights = {moving_light};
  RenderSession mixed_session(config);
  compare(mixed_session, mixed, mixed, "initial mixed commands");
  for (int operation = 0; operation < 3; ++operation) {
    mixed.objects[0].attributes[0].value.x += .12f;
    mixed.objects[0].transform[7] += .06f;
    SceneUpdate update;
    update.object_attributes = {{mixed.objects[0].key, mixed.objects[0].attributes,
                                 ObjectPoseData{mixed.objects[0].key, mixed.objects[0].transform}}};
    mixed.lights[0].transform[3] += .25f;
    update.lights = mixed.lights;
    if (operation == 0) {
      mixed.objects.back().transform[7] -= .2f;
      update.object_poses = {{mixed.objects.back().key, mixed.objects.back().transform}};
    } else if (operation == 1) {
      auto inserted = mixed.objects.back();
      inserted.key = "mixed/inserted";
      inserted.transform[3] = .2f;
      mixed.objects.push_back(inserted);
      update.objects = {inserted};
    } else {
      update.deleted_objects = {mixed.objects.back().key};
      mixed.objects.pop_back();
    }
    compare(mixed_session, update, mixed, "fields/light/disjoint command " + std::to_string(operation), true, 1);
  }
  mixed.lights[0].transform[3] += .6f;
  SceneUpdate light_only;
  light_only.lights = mixed.lights;
  compare(mixed_session, light_only, mixed, "animated light changes diffuse illumination", true);
  // A transaction may mutate a different geometry while compact fields retain
  // their exact accepted prototype/schema. Shared physical geometry is covered
  // by initially inserting an identical body, then breaking that sharing.
  auto neighbor_mesh = std::make_shared<MeshData>(*mixed.meshes[0]);
  neighbor_mesh->key = "mixed/disjoint-mesh";
  auto neighbor_object = mixed.objects[0];
  neighbor_object.key = "mixed/disjoint-owner";
  neighbor_object.geometry_key = neighbor_mesh->key;
  neighbor_object.transform[3] += .3f;
  mixed.meshes.push_back(neighbor_mesh);
  mixed.objects.push_back(neighbor_object);
  const auto disjoint_fields = [&]() {
    mixed.objects[0].attributes[0].value.x += .03f;
    SceneUpdate update;
    update.object_attributes = {{mixed.objects[0].key, mixed.objects[0].attributes}};
    return update;
  };
  auto disjoint = disjoint_fields();
  disjoint.meshes = {neighbor_mesh};
  disjoint.objects = {neighbor_object};
  compare(mixed_session, disjoint, mixed, "fields plus disjoint geometry insertion", true, 1);
  require(mixed_session.status().expanded_attribute_updates == 0,
          "disjoint geometry insertion expanded unrelated fields");
  neighbor_mesh = std::make_shared<MeshData>(*neighbor_mesh);
  neighbor_mesh->revision++;
  neighbor_mesh->positions[0].y += .1f;
  mixed.meshes.back() = neighbor_mesh;
  disjoint = disjoint_fields();
  disjoint.meshes = {neighbor_mesh};
  compare(mixed_session, disjoint, mixed, "fields plus disjoint geometry deformation", true, 1);
  // Invalid unrelated geometry must reject before any borrowed owner is edited.
  const auto before_disjoint_rejection = mixed_session.status();
  const auto before_disjoint_image = mixed_session.frame();
  auto invalid_neighbor = std::make_shared<MeshData>(*neighbor_mesh);
  invalid_neighbor->revision++;
  invalid_neighbor->triangles[0].a = int(invalid_neighbor->positions.size());
  SceneUpdate invalid_disjoint;
  invalid_disjoint.object_attributes = {{mixed.objects[0].key, mixed.objects[0].attributes}};
  invalid_disjoint.object_attributes[0].attributes[0].value.x += .3f;
  invalid_disjoint.meshes = {invalid_neighbor};
  bool disjoint_refused = false;
  try {
    mixed_session.apply(decode_worker_update(encode_worker_update(WorkerUpdate{invalid_disjoint})).scene);
  } catch (const SceneUpdateRejected &) {
    disjoint_refused = true;
  }
  const auto rejected_disjoint = mixed_session.status();
  require(disjoint_refused && rejected_disjoint.generation == before_disjoint_rejection.generation &&
              mixed_session.frame() == before_disjoint_image &&
              rejected_disjoint.object_updates == before_disjoint_rejection.object_updates &&
              rejected_disjoint.geometry_resources == before_disjoint_rejection.geometry_resources &&
              rejected_disjoint.owned_geometry_bytes == before_disjoint_rejection.owned_geometry_bytes &&
              rejected_disjoint.direct_attribute_updates == before_disjoint_rejection.direct_attribute_updates,
          "invalid disjoint geometry changed accepted fields/image/resources");
  ++rejected;
  disjoint = disjoint_fields();
  disjoint.deleted_objects = {neighbor_object.key};
  disjoint.deleted_geometry = {neighbor_mesh->key};
  mixed.meshes.pop_back();
  mixed.objects.pop_back();
  compare(mixed_session, disjoint, mixed, "fields plus disjoint object/geometry retirement", true, 1);
  require(mixed_session.status().expanded_attribute_updates == 0,
          "disjoint geometry retirement expanded unrelated fields");
  auto complex_point = std::make_shared<PointData>();
  complex_point->key = "mixed/complex-point";
  complex_point->revision = 1;
  complex_point->material = mixed.materials[0].key;
  complex_point->positions = {{0, .65f, 0}};
  complex_point->radii = {.15f};
  complex_point->instance_attributes = {{"Cd", 3}};
  mixed.points = {complex_point};
  ObjectData complex_owner{"mixed/complex-owner", complex_point->key};
  complex_owner.attributes = {{"Cd", 3, {.5f, .3f, .7f}}};
  mixed.objects.push_back(complex_owner);
  SceneUpdate add_complex;
  add_complex.points = mixed.points;
  add_complex.objects = {complex_owner};
  compare(mixed_session, add_complex, mixed, "add complex geometry neighbor", true);
  // One ineligible attached pose must expand only that record, not its
  // ordinary neighbor. A nominal pose deliberately requests full preparation.
  mixed.objects[0].attributes[0].value.z += .1f;
  mixed.objects[1].attributes[0].value.y += .1f;
  mixed.objects[1].nominal_transform = mixed.objects[1].transform;
  mixed.objects[1].nominal_transform->at(7) += .07f;
  SceneUpdate partition;
  for (int i = 0; i < 2; ++i) {
    const auto &object = mixed.objects[i];
    partition.object_attributes.push_back(
        {object.key, object.attributes, ObjectPoseData{object.key, object.transform, {}, object.nominal_transform}});
  }
  mixed.objects.back().attributes[0].value.x += .2f;
  partition.object_attributes.push_back({mixed.objects.back().key, mixed.objects.back().attributes});
  const auto fallbacks_before = mixed_session.status().attribute_fallback_count;
  compare(mixed_session, partition, mixed, "per-record nominal pose expansion", true, 1);
  require(mixed_session.status().expanded_attribute_updates == 2 &&
              !mixed_session.status().attribute_fallback_reason.empty() &&
              mixed_session.status().attribute_fallback_reason.size() <= 96 &&
              mixed_session.status().attribute_fallback_count == fallbacks_before + 1,
          "mixed expansion diagnostics did not count exactly one accepted fallback transaction");
  const auto mixed_accepted = mixed_session.status();
  const auto mixed_image = mixed_session.frame();
  WorkerReply diagnostic_reply;
  diagnostic_reply.status = mixed_accepted;
  const auto diagnostic_roundtrip = decode_worker_reply(encode_worker_reply(diagnostic_reply), {}).status;
  require(diagnostic_roundtrip.direct_attribute_updates == mixed_accepted.direct_attribute_updates &&
              diagnostic_roundtrip.expanded_attribute_updates == mixed_accepted.expanded_attribute_updates &&
              diagnostic_roundtrip.attribute_fallback_reason == mixed_accepted.attribute_fallback_reason &&
              diagnostic_roundtrip.attribute_fallback_count == mixed_accepted.attribute_fallback_count,
          "normal-use mixed diagnostics changed in worker status roundtrip");
  const auto reject_mixed = [&](const SceneUpdate &update) {
    bool refused = false;
    try {
      mixed_session.apply(decode_worker_update(encode_worker_update(WorkerUpdate{update})).scene, true);
    } catch (const SceneUpdateRejected &) {
      refused = true;
    }
    const auto status = mixed_session.status();
    require(refused && status.generation == mixed_accepted.generation && mixed_session.frame() == mixed_image &&
                status.object_updates == mixed_accepted.object_updates &&
                status.object_resources == mixed_accepted.object_resources &&
                status.geometry_resources == mixed_accepted.geometry_resources &&
                status.owned_geometry_bytes == mixed_accepted.owned_geometry_bytes &&
                status.direct_attribute_updates == mixed_accepted.direct_attribute_updates &&
                status.expanded_attribute_updates == mixed_accepted.expanded_attribute_updates &&
                status.attribute_fallback_reason == mixed_accepted.attribute_fallback_reason &&
                status.attribute_fallback_count == mixed_accepted.attribute_fallback_count,
            "rejected mixed candidate mutated accepted image, resources, counters or diagnostics");
    ++rejected;
  };
  auto rejected_mixed = partition;
  rejected_mixed.object_attributes[0].attributes[0].value.x += .2f;
  rejected_mixed.object_attributes[1].attributes[0].value.x = std::numeric_limits<float>::quiet_NaN();
  rejected_mixed.deleted_objects = {mixed.objects[2].key};
  rejected_mixed.lights = mixed.lights;
  rejected_mixed.lights[0].transform[7] += .3f;
  reject_mixed(rejected_mixed);
  rejected_mixed = partition;
  rejected_mixed.object_attributes[0].attributes[0].value.x += .2f;
  rejected_mixed.lights = mixed.lights;
  rejected_mixed.lights[0].strength.x = std::numeric_limits<float>::quiet_NaN();
  reject_mixed(rejected_mixed);
  rejected_mixed = partition;
  rejected_mixed.object_attributes[0].attributes[0].value.x += .2f;
  rejected_mixed.deleted_geometry = {mixed.meshes[0]->key};
  reject_mixed(rejected_mixed);
  // Replacement declarations must be checked against candidate complete
  // fields; neither borrowed records nor earlier direct commands may mutate.
  auto replacement = std::make_shared<MeshData>(*mixed.meshes[0]);
  replacement->revision++;
  replacement->instance_attributes["candidate_required"] = 1;
  rejected_mixed = partition;
  rejected_mixed.meshes = {replacement};
  reject_mixed(rejected_mixed);
  require(mixed_session.apply(partition, true) == mixed_accepted.generation && mixed_session.frame() == mixed_image &&
              mixed_session.status().direct_attribute_updates == 0 &&
              mixed_session.status().expanded_attribute_updates == 0 &&
              mixed_session.status().attribute_fallback_reason.empty() &&
              mixed_session.status().attribute_fallback_count == mixed_accepted.attribute_fallback_count,
          "mixed no-op changed image/history or retained last-apply diagnostics");
  // Successful shared resource replacement globally expands both commands.
  replacement->instance_attributes.erase("candidate_required");
  replacement->positions[0].x -= .1f;
  mixed.meshes = {replacement};
  mixed.objects[0].attributes[0].value.y += .15f;
  auto repaired = partition;
  repaired.object_attributes[0].attributes = mixed.objects[0].attributes;
  repaired.meshes = mixed.meshes;
  compare(mixed_session, repaired, mixed, "shared prototype replacement with fields", true);
  require(mixed_session.status().expanded_attribute_updates == 3,
          "shared prototype replacement did not expand all field commands");
  mixed_session.close();
  require(mixed_session.status().owned_geometry_bytes == 0, "mixed commands retained geometry after close");

  // A visible emitter is insufficient evidence: this center receiver patch is
  // diffuse and geometrically separate, and cannot see emitter pixels in the
  // camera. Its radiance must change through retained light-tree illumination.
  auto illuminated = instance_field_scene(0, {{"Cd", 3, {.5f, .4f, .3f}}}, 0, "attribute-illumination", false, false);
  illuminated.rendering = RenderSettings{};
  illuminated.rendering->samples = 64;
  illuminated.rendering->light_tree = true;
  illuminated.materials[0].graph->nodes[1].parameters["strength"] = 20.f;
  auto emitter_mesh = std::make_shared<MeshData>(*illuminated.meshes[0]);
  for (auto &position : emitter_mesh->positions) {
    position.x *= .4f;
    position.y *= .4f;
  }
  illuminated.meshes = {emitter_mesh};
  illuminated.objects[0].transform[3] = .85f;
  illuminated.objects[0].transform[11] = 1;
  MaterialData receiver_material;
  receiver_material.key = "illumination/diffuse";
  receiver_material.color = {.6f, .6f, .6f};
  illuminated.materials.push_back(receiver_material);
  auto receiver_mesh = std::make_shared<MeshData>();
  receiver_mesh->key = "illumination/receiver";
  receiver_mesh->revision = 1;
  receiver_mesh->materials = {receiver_material.key};
  receiver_mesh->positions = {{-2, -2, 0}, {2, -2, 0}, {2, 2, 0}, {-2, 2, 0}};
  receiver_mesh->triangles = {{0, 1, 2}, {0, 2, 3}};
  illuminated.meshes.push_back(receiver_mesh);
  illuminated.objects.push_back({"illumination/receiver-object", receiver_mesh->key});
  const auto receiver_rgb = [&](const FrameEvent &frame) {
    const auto &plane =
        *std::find_if(frame.planes.begin(), frame.planes.end(), [](const auto &p) { return p.pass == "combined"; });
    double sum = 0;
    for (int y = frame.height / 2 - 2; y < frame.height / 2 + 2; ++y)
      for (int x = frame.width / 2 - 2; x < frame.width / 2 + 2; ++x)
        for (int channel = 0; channel < 3; ++channel)
          sum += plane.pixels[(std::size_t(y) * frame.width + x) * plane.channels + channel];
    return sum / 48;
  };
  RenderSession illuminated_session(config);
  auto receiver_before =
      receiver_rgb(*compare(illuminated_session, illuminated, illuminated, "initial receiver illumination"));
  require(receiver_before > .002, "separate diffuse receiver has no emitter illumination");
  for (int edit = 0; edit < 3; ++edit) {
    auto &emitter = illuminated.objects[0];
    if (edit != 1)
      emitter.attributes[0].value = edit == 0 ? Float3{.9f, .6f, .4f} : Float3{.3f, .8f, .7f};
    if (edit != 0)
      emitter.transform[3] += .25f;
    SceneUpdate update;
    update.object_attributes = {{emitter.key, emitter.attributes}};
    if (edit != 0)
      update.object_attributes[0].pose = ObjectPoseData{emitter.key, emitter.transform};
    const auto frame = compare(illuminated_session, update, illuminated,
                               "emitter field/pose receiver edit " + std::to_string(edit), true, 1);
    const auto receiver_after = receiver_rgb(*frame);
    require(std::abs(receiver_after - receiver_before) > .0001,
            "emitter edit changed no independent diffuse receiver illumination");
    receiver_before = receiver_after;
  }
  illuminated_session.close();
  require(illuminated_session.status().owned_geometry_bytes == 0, "emissive receiver retained geometry after close");

  auto displaced =
      instance_height_scene({{"height", 1, {.1f, 0, 0}}, {"height", 1, {.3f, 0, 0}}}, 0, "attribute-displacement");
  RenderSession displaced_session(config);
  compare(displaced_session, displaced, displaced, "initial true displacement");
  const auto rebuilds = displaced_session.status().displacement_rebuilds;
  displaced.objects[0].attributes[0].value.x = .55f;
  displaced.objects[1].attributes[0].value.x = .2f;
  compare(displaced_session, patches(displaced), displaced, "attribute-only true displacement", true);
  require(displaced_session.status().displacement_rebuilds > rebuilds,
          "changed displacement field did not rebuild the needed variants");
  displaced_session.close();
  require(displaced_session.status().owned_geometry_bytes == 0, "displacement attribute state retained after close");

  auto temporal =
      instance_field_scene(0, {instance_field("Cd", 3, 3), instance_field("Cd", 3, 3, 1)}, 3, "attribute-motion");
  RenderSession temporal_session(config);
  compare(temporal_session, temporal, temporal, "initial temporal fields");
  temporal.objects[0].attributes[0].motion_values[0].x = .75f;
  temporal.objects[1].attributes[0].motion_values[2].z = .9f;
  compare(temporal_session, patches(temporal), temporal, "attribute-only temporal samples", true);
  temporal_session.close();
  require(temporal_session.status().owned_geometry_bytes == 0, "temporal attribute state retained after close");

  // Closed polygon media have no sparse-grid demands. Exercise the ordinary
  // surface instance field alongside a real absorbing interior, as used by
  // glass materials; an Attribute feeding Volume itself is a grid demand.
  SceneUpdate medium;
  medium.background = Float3{1, 1, 1};
  medium.motion_blur = false;
  CameraData medium_camera;
  medium_camera.orthographic = true;
  medium_camera.ortho_scale = 3;
  medium.camera = medium_camera;
  MaterialData medium_material;
  medium_material.key = "attribute-medium/material";
  medium_material.kind = MaterialKind::Graph;
  ShaderGraphData medium_graph;
  medium_graph.nodes = {
      {"field", "cycles/attribute/1", {{"attribute", std::string("transmission")}}},
      {"surface", "cycles/transparent_bsdf/1", {}},
      {"medium", "cycles/absorption_volume/1", {{"color", std::array<float, 3>{0, 0, 0}}, {"density", .7f}}}};
  medium_graph.links = {{{"field", "vector"}, {"surface", "color"}}};
  medium_graph.surface = {"surface", "BSDF"};
  medium_graph.volume = ShaderEndpoint{"medium", "volume"};
  medium_material.graph = medium_graph;
  medium.materials = {medium_material};
  auto shell = std::make_shared<MeshData>();
  shell->key = "attribute-medium/shell";
  shell->revision = 1;
  shell->materials = {medium_material.key};
  shell->positions = {{-1, -1, -.5f}, {1, -1, -.5f}, {1, 1, -.5f}, {-1, 1, -.5f},
                      {-1, -1, .5f},  {1, -1, .5f},  {1, 1, .5f},  {-1, 1, .5f}};
  shell->triangles = {{0, 3, 2}, {0, 2, 1}, {4, 5, 6}, {4, 6, 7}, {0, 1, 5}, {0, 5, 4},
                      {1, 2, 6}, {1, 6, 5}, {2, 3, 7}, {2, 7, 6}, {3, 0, 4}, {3, 4, 7}};
  shell->instance_attributes = {{"transmission", 3}};
  medium.meshes = {shell};
  ObjectData shell_object{"attribute-medium/object", shell->key};
  shell_object.attributes = {{"transmission", 3, {.7f, .8f, .9f}}};
  medium.objects = {shell_object};
  const auto center_rgb = [&](const FrameEvent &frame) {
    const auto plane = std::find_if(frame.planes.begin(), frame.planes.end(),
                                    [](const auto &value) { return value.pass == "combined"; });
    require(plane != frame.planes.end(), "medium has no combined pass");
    const auto offset = (std::size_t(frame.height / 2) * frame.width + frame.width / 2) * plane->channels;
    return Float3{plane->pixels[offset], plane->pixels[offset + 1], plane->pixels[offset + 2]};
  };
  const auto expect_medium = [&](const FrameEvent &frame, float density) {
    const auto transmission = medium.objects[0].attributes[0].value;
    const float attenuation = std::exp(-density); // One-unit closed shell, two transparent boundaries.
    const auto value = center_rgb(frame);
    require(std::abs(value.x - transmission.x * transmission.x * attenuation) < .025f &&
                std::abs(value.y - transmission.y * transmission.y * attenuation) < .025f &&
                std::abs(value.z - transmission.z * transmission.z * attenuation) < .025f,
            "closed medium did not preserve independent surface transmission and Beer-Lambert attenuation");
  };
  const auto has_mesh_grid_diagnostic = [](const SessionStatus &status) {
    return std::any_of(status.source_diagnostics.begin(), status.source_diagnostics.end(), [](const auto &issue) {
      return issue.code == "material.geometry_binding" &&
             issue.message.find("Polygon mesh media cannot read sparse volume fields") != std::string::npos;
    });
  };
  RenderSession medium_session(config);
  expect_medium(*compare(medium_session, medium, medium, "initial closed mesh medium"), .7f);
  require(!has_mesh_grid_diagnostic(medium_session.status()), "valid closed medium recovered as sparse-grid shader");
  medium.objects[0].attributes[0].value = {.9f, .4f, .6f};
  expect_medium(*compare(medium_session, patches(medium), medium, "closed medium direct transmission", true, 1), .7f);
  // A grid request on a polygon shell renders as Blender's Cycles renders it
  // (task L1, the user's rule): Volume Info Density finds no voxel attribute on
  // a mesh, returns the Attribute node's Missing value 0, so the medium has zero
  // density and only the surface transmission remains. No recovery diagnostic.
  auto grid_graph = medium_graph;
  grid_graph.nodes.push_back({"grid", "cycles/volume_info/1", {}});
  grid_graph.links.push_back({{"grid", "density"}, {"medium", "density"}});
  medium.materials[0].graph = grid_graph;
  SceneUpdate grid_request;
  grid_request.materials = medium.materials;
  expect_medium(*compare(medium_session, grid_request, medium, "mesh volume-field read as in Blender", true), 0);
  require(!has_mesh_grid_diagnostic(medium_session.status()),
          "mesh volume-field read still recovered as a sparse grid");
  medium_session.close();
  require(medium_session.status().owned_geometry_bytes == 0, "closed medium fields retained after close");

  // Actual sparse-volume objects remain outside the direct path even when
  // only a latent, otherwise valid object field changes.
  SceneUpdate sparse;
  sparse.background = Float3{1, 1, 1};
  sparse.motion_blur = false;
  sparse.camera = medium_camera;
  auto cloud = std::make_shared<VolumeData>();
  cloud->key = "attribute-medium/sparse";
  cloud->revision = 1;
  cloud->material = medium_material.key;
  cloud->fields = {{"density", density_grid()}};
  sparse.volumes = {cloud};
  grid_graph.surface = {};
  grid_graph.nodes.erase(grid_graph.nodes.begin(), grid_graph.nodes.begin() + 2);
  grid_graph.links.erase(grid_graph.links.begin());
  medium_material.graph = grid_graph;
  sparse.materials = {medium_material};
  ObjectData cloud_object{"attribute-medium/cloud", cloud->key};
  cloud_object.attributes = {{"latent", 1, {.2f, 0, 0}}};
  sparse.objects = {cloud_object};
  // Heterogeneous absorption uses stochastic free-flight sampling. Match the
  // existing independent sparse-volume integral fixture's 512 samples and
  // centered 6x6 mean instead of applying its tolerance to one four-sample ray.
  config.samples = 512;
  RenderSession sparse_session(config);
  const auto sparse_initial = compare(sparse_session, sparse, sparse, "initial sparse-volume field guard");
  const auto &sparse_plane = *std::find_if(sparse_initial->planes.begin(), sparse_initial->planes.end(),
                                           [](const auto &value) { return value.pass == "combined"; });
  Float3 sparse_rgb{};
  for (int y = sparse_initial->height / 2 - 3; y < sparse_initial->height / 2 + 3; ++y)
    for (int x = sparse_initial->width / 2 - 3; x < sparse_initial->width / 2 + 3; ++x) {
      const auto offset = (std::size_t(y) * sparse_initial->width + x) * sparse_plane.channels;
      sparse_rgb.x += sparse_plane.pixels[offset] / 36;
      sparse_rgb.y += sparse_plane.pixels[offset + 1] / 36;
      sparse_rgb.z += sparse_plane.pixels[offset + 2] / 36;
    }
  const float sparse_expected = std::exp(-9.f / 8);
  require(std::abs(sparse_rgb.x - sparse_expected) < .025f && std::abs(sparse_rgb.y - sparse_expected) < .025f &&
              std::abs(sparse_rgb.z - sparse_expected) < .025f,
          "sparse absorption RGB " + std::to_string(sparse_rgb.x) + "," + std::to_string(sparse_rgb.y) + "," +
              std::to_string(sparse_rgb.z) + " differs from independent integral " + std::to_string(sparse_expected));
  sparse.objects[0].attributes[0].value.x = .8f;
  compare(sparse_session, patches(sparse), sparse, "sparse-volume latent replacement falls back");
  sparse_session.close();
  require(sparse_session.status().owned_geometry_bytes == 0, "sparse medium fields retained after close");
  std::cout << "{\"object_attribute_contracts\":true,\"checks\":" << checks << ",\"rejected\":" << rejected
            << ",\"fresh_references\":" << references << ",\"maximum_rgba_position_depth_rmse\":" << maximum_error
            << ",\"compact_packet_bytes\":" << packet.size() << ",\"full_packet_bytes\":" << full_bytes
            << ",\"maximum_owned_geometry_bytes\":" << maximum_owned_bytes
            << ",\"peak_device_allocation_bytes\":" << maximum_device_bytes << "}\n";
}
} // namespace cycles_houdini::harness
