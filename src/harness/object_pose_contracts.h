// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/snapshot.h"
#include <algorithm>
#include <bit>
#include <chrono>
#include <cmath>
#include <cstring>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <thread>

namespace cycles_houdini::harness {
inline void object_pose_contracts(const std::string &device) {
  int checks = 0, rejected = 0, references = 0;
  const auto require = [&](bool value, const char *message) {
    ++checks;
    if (!value)
      throw std::runtime_error(message);
  };
  SceneUpdate initial;
  initial.objects.reserve(100000);
  for (int index = 0; index < 100000; ++index) {
    ObjectData object;
    object.key = "instance/" + std::to_string(index);
    object.geometry_key = "prototype/shared/high-detail-mesh";
    object.random_id = index;
    object.transform[3] = float(index) * .001f;
    object.properties.cryptomatte_object = "asset/" + object.key;
    object.properties.cryptomatte_asset = "asset/crowd";
    object.properties.pass_index = index % 11;
    initial.objects.push_back(std::move(object));
  }
  auto baseline = snapshot_state({}, initial);
  auto moved = initial;
  for (auto &object : moved.objects) {
    object.transform[3] += .25f;
    object.nominal_transform = object.transform;
  }
  auto desired = snapshot_state(baseline, moved);
  const auto delta = snapshot_difference(baseline.get(), *desired);
  require(delta.objects.empty() && delta.object_poses.size() == 100000, "Transform delta repeated full bindings");
  WorkerUpdate compact;
  compact.scene = delta;
  const auto packet = encode_worker_update(compact);
  WorkerUpdate full;
  full.scene = moved;
  const auto full_bytes = encode_worker_update(full).size();
  require(packet.size() * 2 < full_bytes, "Pose transport did not remove repeated binding data");
  const auto decoded = decode_worker_update(packet);
  require(decoded.scene.objects.empty() && decoded.scene.object_poses == delta.object_poses,
          "Pose wire roundtrip changed data");
  const auto restored = snapshot_state(baseline, decoded.scene);
  require(*restored->objects == *desired->objects, "Pose application changed retained object properties");
  require(baseline->objects->begin()->second.transform[3] != restored->objects->begin()->second.transform[3],
          "Pose application mutated old ownership");
  auto field_edit = moved;
  field_edit.objects.front().attributes = {{"rand", 1, {.25f, 0, 0}}, {"counter", 1, {7, 0, 0}}};
  const auto with_fields = snapshot_state(baseline, field_edit);
  const auto field_delta = snapshot_difference(baseline.get(), *with_fields);
  require(field_delta.objects.empty() && field_delta.object_poses.size() == 99999 &&
              field_delta.object_attributes.size() == 1 && field_delta.object_attributes.front().pose,
          "Instance field edit was lost as a pose-only command");
  const auto field_wire = decode_worker_update(encode_worker_update(WorkerUpdate{field_delta}));
  require(field_wire.scene.object_attributes.front().attributes == field_edit.objects.front().attributes &&
              field_wire.scene.object_attributes.front().pose->transform == field_edit.objects.front().transform,
          "Instance field transport changed owned values");
  // Shader socket constants and packed-instance values must preserve float
  // representation as well as type through comparison, delta and transport.
  {
    float negative{};
    const std::uint32_t bits = 0x80000000u;
    std::memcpy(&negative, &bits, sizeof(bits));
    require(!shader_value_equal(ShaderValue{0.f}, ShaderValue{negative}),
            "Scalar shader parameter bits were discarded");
    require(!shader_value_equal(ShaderValue{std::array<float, 3>{0, 1, 2}},
                                ShaderValue{std::array<float, 3>{negative, 1, 2}}),
            "Vector shader parameter bits were discarded");
    require(!shader_value_equal(ShaderValue{0}, ShaderValue{0.f}), "Shader parameter types were merged");
    ShaderNodeData before{"noise", "cycles/white_noise_texture/1", {{"w", 0.f}}};
    auto after = before;
    after.parameters["w"] = negative;
    require(before != after, "Shader node comparison ignored hash-sensitive parameters");
    SceneUpdate state;
    ObjectData object;
    object.key = "bits/object";
    object.geometry_key = "bits/prototype";
    object.attributes = {{"seed", 1, {0, 0, 0}}};
    state.objects = {object};
    const auto old = snapshot_state({}, state);
    state.objects[0].attributes[0].value.x = negative;
    const auto next = snapshot_state(old, state);
    const auto change = snapshot_difference(old.get(), *next);
    require(change.objects.empty() && change.object_attributes.size() == 1 && change.object_poses.empty(),
            "Packed value bits were lost as an unchanged/pose-only update");
    const auto restored_bits = decode_worker_update(encode_worker_update(WorkerUpdate{change}));
    require(std::bit_cast<std::uint32_t>(restored_bits.scene.object_attributes[0].attributes[0].value.x) == bits,
            "Packed field wire changed authored bits");
  }
  SceneUpdate payloads;
  auto payload_mesh = std::make_shared<MeshData>();
  payload_mesh->key = "owned/mesh";
  payload_mesh->positions = {{0, 0, 0}, {1, 0, 0}, {0, 1, 0}};
  payload_mesh->triangles = {{0, 1, 2}};
  payloads.meshes = {payload_mesh};
  auto payload_points = std::make_shared<PointData>();
  payload_points->key = "owned/points";
  payload_points->positions = {{0, 0, 0}};
  payload_points->radii = {.1f};
  payloads.points = {payload_points};
  auto payload_curves = std::make_shared<CurveData>();
  payload_curves->key = "owned/curves";
  payload_curves->positions = {{0, 0, 0}, {1, 0, 0}};
  payload_curves->radii = {.1f, .1f};
  payload_curves->first_key = {0};
  payloads.curves = {payload_curves};
  const auto payload_base = snapshot_state({}, payloads);
  payload_mesh = std::make_shared<MeshData>(*payload_mesh);
  payload_mesh->revision = 2;
  payloads.meshes = {payload_mesh};
  payload_points = std::make_shared<PointData>(*payload_points);
  payload_points->revision = 2;
  payloads.points = {payload_points};
  payload_curves = std::make_shared<CurveData>(*payload_curves);
  payload_curves->revision = 2;
  payloads.curves = {payload_curves};
  const auto payload_same = snapshot_state(payload_base, payloads);
  auto payload_delta = snapshot_difference(payload_base.get(), *payload_same);
  require(payload_delta.meshes.empty() && payload_delta.points.empty() && payload_delta.curves.empty(),
          "Equivalent newly cooked geometry repeated render buffers");
  require(payload_same->meshes.at("owned/mesh") == payload_base->meshes.at("owned/mesh"),
          "Equivalent geometry did not retain its immutable owner");
  payload_mesh->positions[0].x = .1f;
  payload_points->radii[0] = .2f;
  payload_curves->instance_attributes = {{"rand", 1}};
  const auto payload_changed = snapshot_state(payload_same, payloads);
  payload_delta = snapshot_difference(payload_same.get(), *payload_changed);
  require(payload_delta.meshes.size() == 1 && payload_delta.points.size() == 1 && payload_delta.curves.size() == 1,
          "Changed positions/radii/instance layout were incorrectly reused");
  moved.objects.front().visibility = 0;
  auto mixed = snapshot_state(baseline, moved);
  const auto mixed_delta = snapshot_difference(baseline.get(), *mixed);
  require(mixed_delta.objects.size() == 1 && mixed_delta.object_poses.size() == 99999,
          "Binding edits were misclassified as poses");
  SceneUpdate unordered;
  auto first_new = initial.objects.front(), last_new = first_new, rebound = first_new;
  first_new.key = "000/new";
  last_new.key = "zzz/new";
  rebound.geometry_key = "replacement/prototype";
  unordered.objects = {last_new, rebound, first_new};
  unordered.deleted_objects = {"instance/4", "instance/54", "absent"};
  for (const auto *key : {"instance/92", "instance/17", "instance/810"}) {
    ObjectPoseData pose{key};
    pose.transform[7] = .7f;
    unordered.object_poses.push_back(pose);
  }
  auto expected_table = *baseline->objects;
  for (const auto &key : unordered.deleted_objects)
    expected_table.erase(key);
  for (const auto &object : unordered.objects)
    expected_table[object.key] = object;
  for (const auto &pose : unordered.object_poses) {
    auto &object = expected_table.at(pose.key);
    object.transform = pose.transform;
    object.motion = pose.motion;
    object.nominal_transform = pose.nominal_transform;
  }
  const auto ordered_result = snapshot_state(baseline, unordered);
  require(*ordered_result->objects == expected_table,
          "Unsorted mixed additions/deletions/rebindings/poses changed state");
  const auto ordered_delta = snapshot_difference(baseline.get(), *ordered_result);
  require(ordered_delta.objects.size() == 3 && ordered_delta.object_poses.size() == 3 &&
              ordered_delta.deleted_objects.size() == 2,
          "Ordered difference dropped a mixed update or included an absent deletion");
  require(*snapshot_state(baseline, ordered_delta)->objects == expected_table,
          "Mixed sparse difference did not replay exactly");
  SceneUpdate bad;
  bad.object_poses = {delta.object_poses.front(), delta.object_poses.front()};
  const auto invalid_snapshot = [&](const SceneUpdate &update) {
    bool refused = false;
    try {
      snapshot_state(baseline, update);
    } catch (const std::invalid_argument &) {
      refused = true;
    }
    require(refused, "Malformed snapshot poses were accepted");
  };
  invalid_snapshot(bad);
  bad.object_poses.resize(1);
  bad.objects = {moved.objects.front()};
  invalid_snapshot(bad);
  bad.objects.clear();
  bad.deleted_objects = {bad.object_poses.front().key};
  invalid_snapshot(bad);
  bad.deleted_objects.clear();
  bad.object_poses.front().key = "missing/object";
  invalid_snapshot(bad);
  WorkerUpdate single;
  single.scene.object_poses = {{"pose_contract_nominal"}};
  const auto absent = encode_worker_update(single);
  single.scene.object_poses.front().nominal_transform = identity_matrix;
  const auto equal = encode_worker_update(single);
  require(equal.size() == absent.size() &&
              decode_worker_update(equal).scene.object_poses.front().nominal_transform == identity_matrix,
          "Equal nominal pose lost its presence or duplicated the matrix");
  single.scene.object_poses.front().nominal_transform->at(3) = -0.f;
  auto signed_zero = encode_worker_update(single);
  const auto signed_pose = decode_worker_update(signed_zero).scene.object_poses.front();
  require(signed_zero.size() == equal.size() + sizeof(Matrix) && std::signbit(signed_pose.nominal_transform->at(3)),
          "Pose transport canonicalized signed-zero matrix bits");
  const auto &key = single.scene.object_poses.front().key;
  const auto begin = reinterpret_cast<const std::byte *>(key.data());
  const auto found = std::search(signed_zero.begin(), signed_zero.end(), begin, begin + key.size());
  require(found != signed_zero.end(), "Nominal-tag fixture key is missing");
  const auto tag = std::size_t(found - signed_zero.begin()) + key.size() + sizeof(Matrix) + sizeof(std::uint64_t);
  signed_zero.at(tag) = std::byte{3};
  bool refused_tag = false;
  try {
    decode_worker_update(signed_zero);
  } catch (const std::invalid_argument &) {
    refused_tag = true;
  }
  require(refused_tag, "Unknown pose nominal-transform tag was accepted");

  const auto wait = [&](RenderSession &session, std::uint64_t generation) {
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(40);
    while (true) {
      const auto status = session.status();
      if (status.state == SessionState::Failed)
        throw std::runtime_error(status.error);
      const auto frame = session.frame();
      if (frame && frame->generation == generation && frame->complete)
        return frame;
      if (std::chrono::steady_clock::now() > deadline)
        throw std::runtime_error("Pose render timed out");
      std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }
  };
  const auto plane = [&](const FrameEvent &frame, const std::string &name) -> const ImagePlane & {
    const auto found =
        std::find_if(frame.planes.begin(), frame.planes.end(), [&](const auto &p) { return p.pass == name; });
    require(found != frame.planes.end(), "Required pose reference pass is missing");
    return *found;
  };
  const auto bounds = [&](const FrameEvent &frame) {
    std::array<int, 4> result{frame.width, -1, frame.height, -1};
    const auto &depth = plane(frame, "depth").pixels;
    for (int y = 0; y < frame.height; ++y)
      for (int x = 0; x < frame.width; ++x) {
        const auto d = depth[std::size_t(y) * frame.width + x];
        if (d > 0 && d < 10) {
          result[0] = std::min(result[0], x);
          result[1] = std::max(result[1], x);
          result[2] = std::min(result[2], y);
          result[3] = std::max(result[3], y);
        }
      }
    return result;
  };
  for (bool persistent : {false, true}) {
    SessionConfig config;
    config.device_id = device;
    config.width = 96;
    config.height = 64;
    config.samples = 4;
    config.interactive = false;
    config.persistent_updates = persistent;
    config.collect_diagnostics = true;
    config.passes = {"combined", "depth", "normal", "object_id", "motion"};
    config.cryptomatte.object = config.cryptomatte.material = config.cryptomatte.asset = true;
    SceneUpdate scene;
    MaterialData material;
    material.key = "pose/emission";
    material.kind = MaterialKind::Emission;
    material.color = {.2f, .5f, .8f};
    material.cryptomatte_name = "materials/emission";
    scene.materials = {material};
    auto mesh = std::make_shared<MeshData>();
    mesh->key = "pose/quad";
    mesh->revision = 1;
    mesh->positions = {{-.5f, -.5f, 0}, {.5f, -.5f, 0}, {.5f, .5f, 0}, {-.5f, .5f, 0}};
    mesh->triangles = {{0, 1, 2}, {0, 2, 3}};
    mesh->materials = {material.key};
    scene.meshes = {mesh};
    scene.camera = CameraData{};
    scene.camera->orthographic = true;
    scene.camera->ortho_scale = 8.f / 3;
    scene.camera->transform = {1, 0, 0, 0, 0, 1, 0, 0, 0, 0, -1, 3};
    scene.rendering = RenderSettings{};
    scene.rendering->samples = 4;
    scene.rendering->adaptive_sampling = false;
    scene.rendering->pixel_filter = "box";
    scene.rendering->filter_width = 1;
    ObjectData object;
    object.key = "pose/object";
    object.geometry_key = mesh->key;
    object.random_id = 37;
    object.properties.pass_index = 7;
    object.properties.cryptomatte_object = "objects/preserved";
    object.properties.cryptomatte_asset = "assets/preserved";
    object.properties.caustics_caster = true;
    object.properties.shadow_terminator_shading = .1f;
    scene.objects = {object};
    RenderSession session(config);
    auto first = wait(session, session.apply(scene));
    require(bounds(*first) == std::array<int, 4>{36, 59, 20, 43}, "Independent orthographic baseline is misplaced");
    const auto conversions = session.status().geometry_conversions;
    const auto metadata = first->output_metadata;
    std::vector<std::pair<SceneUpdate, std::shared_ptr<const FrameEvent>>> comparisons;
    ObjectPoseData pose{object.key};
    pose.transform[3] = .25f;
    const auto apply = [&]() {
      SceneUpdate update;
      update.object_poses = {pose};
      const auto decoded_pose = decode_worker_update(encode_worker_update(WorkerUpdate{update}));
      auto image = wait(session, session.apply(decoded_pose.scene));
      object.transform = pose.transform;
      object.motion = pose.motion;
      object.nominal_transform = pose.nominal_transform;
      scene.objects = {object};
      comparisons.emplace_back(scene, image);
      require(session.status().geometry_conversions == conversions, "Pose edit converted geometry");
      require(image->output_metadata && metadata && *image->output_metadata == *metadata,
              "Pose changed Cryptomatte identity");
      const auto &ids = plane(*image, "object_id").pixels;
      require(std::any_of(ids.begin(), ids.end(), [](float v) { return v == 7; }), "Pose lost the object pass index");
      return image;
    };
    require(bounds(*apply()) == std::array<int, 4>{42, 65, 20, 43},
            "Pose did not move the independently projected object");
    pose.transform[0] = -1.2f;
    pose.transform[5] = .8f;
    apply();
    pose.transform = identity_matrix;
    pose.transform[3] = .25f;
    auto before = pose.transform, after = pose.transform;
    before[3] -= .5f;
    after[3] += .5f;
    pose.motion = {before, pose.transform, after};
    pose.nominal_transform = identity_matrix;
    apply();
    const auto invalid = [&](const SceneUpdate &update) {
      const auto old = session.status();
      const auto image = session.frame();
      bool refused = false;
      try {
        session.apply(update);
      } catch (const SceneUpdateRejected &) {
        refused = true;
      }
      require(refused && session.status().state != SessionState::Failed &&
                  session.status().generation == old.generation &&
                  session.status().owned_geometry_bytes == old.owned_geometry_bytes,
              "Invalid pose mutated the accepted renderer");
      require(session.frame() == image, "Rejected pose replaced accepted pixels");
      ++rejected;
    };
    bad = {};
    bad.object_poses = {pose, pose};
    invalid(bad);
    bad.object_poses = {pose};
    bad.object_poses[0].key.clear();
    invalid(bad);
    bad.object_poses = {pose};
    bad.object_poses[0].key = "missing";
    invalid(bad);
    bad = {};
    bad.object_poses = {pose};
    bad.deleted_objects = {pose.key};
    invalid(bad);
    bad = {};
    bad.object_poses = {pose};
    bad.objects = {object};
    invalid(bad);
    bad = {};
    bad.object_poses = {pose};
    bad.deleted_geometry = {mesh->key};
    invalid(bad);
    bad = {};
    bad.object_poses = {pose};
    bad.object_poses[0].transform[3] = std::numeric_limits<float>::quiet_NaN();
    invalid(bad);
    bad.object_poses = {pose};
    bad.object_poses[0].transform[0] = 0;
    invalid(bad);
    bad.object_poses = {pose};
    bad.object_poses[0].motion.resize(2);
    invalid(bad);
    bad.object_poses = {pose};
    bad.object_poses[0].motion[1][3] += .1f;
    invalid(bad);
    bad.object_poses = {pose};
    bad.object_poses[0].nominal_transform->at(3) = std::numeric_limits<float>::infinity();
    invalid(bad);
    pose.motion.clear();
    pose.nominal_transform.reset();
    pose.transform[3] = -.25f;
    apply();
    ObjectData second = object;
    second.key = "pose/second";
    second.transform[3] = 1;
    second.properties.pass_index = 18;
    second.properties.cryptomatte_object = "objects/second";
    SceneUpdate add;
    add.objects = {second};
    wait(session, session.apply(add));
    ObjectPoseData first_pose{object.key}, second_pose{second.key};
    const auto bulk = [&](bool ordered) {
      first_pose.transform[3] = ordered ? .2f : -.1f;
      second_pose.transform[3] = ordered ? 1.4f : 1.2f;
      SceneUpdate update;
      update.object_poses = ordered ? std::vector<ObjectPoseData>{first_pose, second_pose}
                                    : std::vector<ObjectPoseData>{second_pose, first_pose};
      const auto owned_input = encode_worker_update(WorkerUpdate{update});
      auto image = wait(session, session.apply(update));
      require(encode_worker_update(WorkerUpdate{update}) == owned_input,
              "Applying a borrowed pose command mutated caller-owned data");
      object.transform = first_pose.transform;
      object.motion.clear();
      object.nominal_transform.reset();
      second.transform = second_pose.transform;
      scene.objects = {object, second};
      comparisons.emplace_back(scene, image);
      require(session.status().geometry_conversions == conversions, "Bulk pose edit converted shared geometry");
      const auto &ids = plane(*image, "object_id").pixels;
      require(std::any_of(ids.begin(), ids.end(), [](float v) { return v == 7; }) &&
                  std::any_of(ids.begin(), ids.end(), [](float v) { return v == 18; }),
              "Bulk pose edit lost a retained object property");
    };
    bulk(false);
    bulk(true);
    bad = {};
    bad.object_poses = {second_pose, first_pose, second_pose};
    invalid(bad);
    bad.object_poses = {first_pose, first_pose, second_pose};
    invalid(bad);
    bad = {};
    auto invalid_object = object;
    invalid_object.attributes = {{"rand", 1, {.2f, 0, 0}}, {"rand", 1, {.3f, 0, 0}}};
    bad.objects = {invalid_object};
    invalid(bad);
    invalid_object.attributes = {{"rand", 4, {.2f, 0, 0}}};
    bad.objects = {invalid_object};
    invalid(bad);
    invalid_object.attributes = {{"rand", 1, {std::numeric_limits<float>::quiet_NaN(), 0, 0}}};
    bad.objects = {invalid_object};
    invalid(bad);
    auto declared = std::make_shared<MeshData>(*mesh);
    declared->revision = 2;
    declared->instance_attributes = {{"rand", 1}};
    bad = {};
    bad.meshes = {declared};
    invalid(bad); // Existing objects cannot lose required user-data ownership.
    declared = std::make_shared<MeshData>(*declared);
    declared->attributes = {{"rand", 1, std::vector<Float3>(6, Float3{.2f, 0, 0})}};
    bad.meshes = {declared};
    invalid(bad); // A declaration cannot shadow real geometry storage.
    ObjectData field_object = object;
    field_object.attributes = {{"rand", 1, {.25f, 0, 0}}};
    SceneUpdate field_values;
    field_values.objects = {field_object};
    const auto no_attribute_bytes = session.status().owned_geometry_bytes;
    const auto one_attribute_bytes = sizeof(UniformAttribute) + std::string("rand").size();
    auto count = session.status().object_updates;
    wait(session, session.apply(field_values, true));
    require(session.status().object_updates == count + 1, "IPR reuse skipped an instance attribute binding");
    require(session.status().owned_geometry_bytes == no_attribute_bytes + one_attribute_bytes,
            "Instance attribute addition has incorrect owned memory");
    field_values.objects[0].attributes[0].value.x = .75f;
    count = session.status().object_updates;
    wait(session, session.apply(field_values, true));
    require(session.status().object_updates == count + 1, "IPR reuse skipped an instance attribute value edit");
    count = session.status().object_updates;
    wait(session, session.apply(field_values, true));
    require(session.status().object_updates == count, "Identical instance attributes bypassed unchanged-scene reuse");
    require(session.status().owned_geometry_bytes == no_attribute_bytes + one_attribute_bytes,
            "Instance value edit or unchanged reuse accumulated owned memory");
    field_values.objects[0].attributes[0].motion_values = {{.1f, 0, 0}, {.5f, 0, 0}, {.9f, 0, 0}};
    wait(session, session.apply(field_values));
    require(session.status().owned_geometry_bytes == no_attribute_bytes + one_attribute_bytes + 3 * sizeof(Float3),
            "Instance shutter attributes have incorrect owned memory");
    field_values.objects[0].attributes.clear();
    wait(session, session.apply(field_values));
    require(session.status().owned_geometry_bytes == no_attribute_bytes,
            "Instance attribute removal retained owned memory");
    field_values.objects[0].attributes = field_object.attributes;
    auto attributed_second = second;
    attributed_second.attributes = field_object.attributes;
    field_values.objects.push_back(attributed_second);
    wait(session, session.apply(field_values));
    require(session.status().owned_geometry_bytes == no_attribute_bytes + 2 * one_attribute_bytes,
            "Multiple instance attributes have incorrect owned memory");
    // Keep the prototype bound while testing the attribute total. Removing its
    // last owner also changes shader/geometry storage (the unbound palette),
    // which is independent of retained object attribute accounting.
    auto witness = second;
    witness.key = "pose/unattributed-witness";
    witness.attributes.clear();
    field_values.objects = {witness};
    wait(session, session.apply(field_values));
    SceneUpdate remove_instances;
    remove_instances.deleted_objects = {object.key, second.key, "already/absent"};
    wait(session, session.apply(remove_instances));
    require(session.status().object_resources == 1 && session.status().owned_geometry_bytes == no_attribute_bytes,
            "Batched instance deletion retained owned attribute memory");
    remove_instances.deleted_objects = {witness.key};
    wait(session, session.apply(remove_instances));
    require(session.status().object_resources == 0, "Last unattributed instance deletion retained its owner");
    session.close();
    require(session.status().owned_geometry_bytes == 0, "Closed pose session retains geometry");
    config.persistent_updates = false;
    for (const auto &[expected_scene, actual] : comparisons) {
      RenderSession reference(config);
      const auto expected = wait(reference, reference.apply(expected_scene));
      require(expected->planes.size() == actual->planes.size(), "Fresh pose reference pass set differs");
      for (const auto &p : expected->planes) {
        const auto &value = plane(*actual, p.pass);
        require(value.pixels.size() == p.pixels.size(), "Fresh pose reference storage differs");
        // IDs may contain non-finite bit patterns. Compare their bits;
        // radiance/depth use finite, independently captured values.
        if (p.pass.starts_with("Crypto"))
          require(std::memcmp(value.pixels.data(), p.pixels.data(), p.pixels.size() * sizeof(float)) == 0,
                  "Pose altered fresh Cryptomatte IDs");
        else
          for (std::size_t i = 0; i < p.pixels.size(); ++i)
            require(value.pixels[i] == p.pixels[i] || std::abs(value.pixels[i] - p.pixels[i]) < 2e-5f,
                    "Pose differs from fresh full-object render");
      }
      ++references;
      reference.close();
    }
  }
  std::cout << "{\"result\":\"passed\",\"checks\":" << checks << ",\"safe_rejections\":" << rejected
            << ",\"fresh_comparisons\":" << references << ",\"objects\":100000,\"pose_bytes\":" << packet.size()
            << ",\"full_object_bytes\":" << full_bytes << ",\"worker_protocol\":" << worker_protocol_version << "}\n";
}
} // namespace cycles_houdini::harness
