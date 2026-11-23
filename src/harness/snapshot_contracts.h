// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/snapshot.h"
#include "../core/coarse_subdivision_mesh.h"
#include "../core/mesh_payload.h"
#include <iostream>
#include <stdexcept>

namespace cycles_houdini::harness {
inline void snapshot_contracts() {
  const auto require = [](bool value, const char *message) {
    if (!value)
      throw std::runtime_error(message);
  };
  MeshData control;
  control.positions = {{-1.5f, -1.5f, 0}, {1.5f, -1.5f, 0}, {1.5f, -.5f, 0},
                       {-.5f, -.5f, 0},   {-.5f, 1.5f, 0},  {-1.5f, 1.5f, 0}};
  control.subdivision = SubdivisionData{};
  control.subdivision->face_sizes = {6};
  control.subdivision->face_corners = {0, 1, 2, 3, 4, 5};
  NamedAttribute field;
  field.name = "corner";
  for (int n = 0; n < 6; ++n) {
    control.corner_uv.push_back({float(n), float(n + 1)});
    control.corner_colors.push_back({float(n), 2, 3});
    field.values.push_back({float(n), 4, 5});
  }
  control.motion_corner_uv = {static_cast<const std::vector<Float2> &>(control.corner_uv)};
  control.motion_corner_colors = {static_cast<const std::vector<Float3> &>(control.corner_colors)};
  field.motion_values = {static_cast<const std::vector<Float3> &>(field.values)};
  control.attributes.push_back(field);
  std::vector<int> ownership;
  const auto coarse = coarse_subdivision_mesh(control, ownership);
  require(!coarse.subdivision && coarse.positions == control.positions && coarse.triangles.size() == 4 &&
              ownership == std::vector<int>(4, 0),
          "Original-polygon preview changed control positions or lost source-face ownership");
  double area = 0;
  for (std::size_t triangle = 0; triangle < coarse.triangles.size(); ++triangle) {
    const auto t = coarse.triangles[triangle];
    const auto a = coarse.positions[t.a], b = coarse.positions[t.b], c = coarse.positions[t.c];
    area += .5 * ((b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x));
    int corner = 0;
    for (int point : {t.a, t.b, t.c}) {
      const auto index = triangle * 3 + corner++;
      require(coarse.corner_uv[index] == control.corner_uv[point] &&
                  coarse.corner_colors[index] == control.corner_colors[point] &&
                  coarse.motion_corner_uv[0][index] == control.corner_uv[point] &&
                  coarse.motion_corner_colors[0][index] == control.corner_colors[point] &&
                  coarse.attributes[0].values[index] == field.values[point] &&
                  coarse.attributes[0].motion_values[0][index] == field.values[point],
              "Original-polygon preview lost static or sampled shading corners");
    }
  }
  require(std::abs(area - 5) < 1e-9 && control.corner_uv.size() == 6 && control.attributes[0].values.size() == 6,
          "Concave polygon triangulation changed area or mutated its owned source");
  SceneUpdate initial;
  initial.objects.reserve(100000);
  for (int n = 0; n < 100000; ++n) {
    ObjectData object;
    object.key = "instance:" + std::to_string(n);
    object.geometry_key = "prototype";
    object.transform[3] = float(n) * .01f;
    object.properties.cryptomatte_object = "asset/" + object.key;
    initial.objects.push_back(std::move(object));
  }
  auto baseline = snapshot_state({}, initial);
  require(baseline->objects->size() == 100000, "Incomplete owned object table");

  SceneUpdate camera;
  camera.camera = CameraData{};
  camera.camera->transform[3] = .25f;
  camera.camera->image_iris = "owned/colored-iris.exr";
  camera.camera->image_iris_method = 1;
  camera.camera->image_iris_color = true;
  auto camera_state = snapshot_state(baseline, camera);
  require(camera_state->objects == baseline->objects, "Camera snapshot copied the object table");
  auto camera_delta = snapshot_difference(baseline.get(), *camera_state);
  require(camera_delta.objects.empty() && camera_delta.object_poses.empty() && camera_delta.deleted_objects.empty() &&
              camera_delta.camera == camera.camera,
          "Camera-only difference changed objects or lost the camera");

  SceneUpdate shading;
  MaterialData material;
  material.key = "surface";
  material.color = {.2f, .6f, .3f};
  shading.materials.push_back(material);
  auto shaded = snapshot_state(camera_state, shading);
  require(shaded->objects == baseline->objects, "Material snapshot copied the object table");

  SceneUpdate moved;
  auto object = baseline->objects->at("instance:0");
  object.transform[3] = 2;
  object.properties.lightgroup = "emission_group";
  moved.objects.push_back(object);
  auto transformed = snapshot_state(shaded, moved);
  require(transformed->objects != baseline->objects && baseline->objects->at(object.key).transform[3] == 0 &&
              baseline->objects->at(object.key).properties.lightgroup.empty(),
          "Object edit mutated an accepted snapshot");
  const auto transform_delta = snapshot_difference(shaded.get(), *transformed);
  require(transform_delta.objects.size() == 1 && transform_delta.objects.front() == object &&
              transform_delta.deleted_objects.empty(),
          "Sparse object edit did not produce its exact delta");

  SceneUpdate removed;
  removed.deleted_objects.push_back(object.key);
  auto erased = snapshot_state(transformed, removed);
  require(erased->objects->size() == 99999 && transformed->objects->contains(object.key),
          "Deletion mutated an older table");
  const auto deletion = snapshot_difference(transformed.get(), *erased);
  require(deletion.objects.empty() && deletion.deleted_objects == removed.deleted_objects,
          "Deletion delta is incorrect");

  SceneUpdate equivalent;
  equivalent.objects.push_back(baseline->objects->at(object.key));
  auto equal_state = snapshot_state(baseline, equivalent);
  require(equal_state->objects != baseline->objects &&
              snapshot_difference(baseline.get(), *equal_state).objects.empty(),
          "Different object tables bypassed complete value comparison");
  auto previous = camera_state;
  for (int n = 0; n < 200; ++n) {
    camera.camera->transform[3] = float(n);
    previous = snapshot_state(previous, camera);
    require(previous->objects == baseline->objects, "Repeated camera edits lost object ownership sharing");
  }

  SceneUpdate malformed;
  malformed.objects = {object, object};
  bool rejected = false;
  try {
    snapshot_state(baseline, malformed);
  } catch (const std::invalid_argument &) {
    rejected = true;
  }
  require(rejected && baseline->objects->size() == 100000 && baseline->objects->at(object.key).transform[3] == 0,
          "Duplicate object keys were accepted or changed the baseline");

  WorkerUpdate wire;
  wire.scene = camera_delta;
  wire.scene.dicing = DicingSettings{};
  wire.scene.dicing->update = false;
  wire.scene.dicing->refresh_revision = 7;
  const auto decoded = decode_worker_update(encode_worker_update(wire));
  require(decoded.scene.objects.empty() && decoded.scene.object_poses.empty() &&
              decoded.scene.deleted_objects.empty() && decoded.scene.camera == camera_delta.camera,
          "Camera-only wire roundtrip included objects or lost camera values");
  require(decoded.scene.dicing == wire.scene.dicing, "Wire roundtrip lost tessellation update policy");
  auto invalid = std::make_shared<SceneSnapshotState>(*baseline);
  invalid->objects.reset();
  int rejected_ownership = 0;
  try {
    snapshot_state(invalid, {});
  } catch (const std::logic_error &) {
    ++rejected_ownership;
  }
  try {
    snapshot_difference(invalid.get(), *baseline);
  } catch (const std::logic_error &) {
    ++rejected_ownership;
  }
  require(rejected_ownership == 2, "Missing owned object tables were silently accepted");
  auto splats = std::make_shared<PointData>();
  splats->key = "owned/splats";
  splats->revision = 11;
  splats->positions = {{1, 2, 3}, {4, 5, 6}};
  splats->radii = {1, 2};
  splats->splats.emplace();
  auto &parameters = *splats->splats;
  parameters.radiance = {{.1f, .2f, .3f, .4f}, {-.5f, .6f, .7f, .8f}};
  parameters.scales = {{.1f, .2f, .3f}, {.4f, .5f, .6f}};
  parameters.rotations = {{0, 0, 0, 1}, {1, 0, 0, 0}};
  parameters.harmonics.resize(2);
  for (int p = 0; p < 2; ++p)
    for (int n = 0; n < 15; ++n)
      parameters.harmonics[p][n] = {float(p * 45 + n * 3 - 45) / 100, float(p * 45 + n * 3 - 44) / 100,
                                    float(p * 45 + n * 3 - 43) / 100};
  std::unordered_set<const void *> gaussian_allocations;
  require(mesh_payload_heap(parameters) == 448 && mesh_payload_owned_heap(parameters, gaussian_allocations) >= 448,
          "Gaussian heap accounting omitted raw radiance, shape or SH arrays");
  WorkerUpdate splat_wire;
  splat_wire.scene.points = {splats};
  const auto splat_packet = encode_worker_update(splat_wire);
  const auto splat_copy = decode_worker_update(splat_packet).scene.points.at(0);
  require(splat_copy->key == splats->key && splat_copy->revision == 11 && splat_copy->positions == splats->positions &&
              splat_copy->splats == splats->splats,
          "Wire lost native Gaussian shape, radiance, or SH ownership");
  splats->splats->radiance[0][3] = .9f;
  require(splat_copy->splats->radiance[0][3] == .4f, "Decoded Gaussian arrays alias their source");
  SceneUpdate gaussian_update;
  gaussian_update.points = {splat_copy};
  const auto gaussian_state = snapshot_state({}, gaussian_update);
  auto same_splats = std::make_shared<PointData>(*splat_copy);
  same_splats->revision = 12;
  gaussian_update.points = {same_splats};
  const auto gaussian_same = snapshot_state(gaussian_state, gaussian_update);
  require(gaussian_same->points.at(same_splats->key) == splat_copy &&
              snapshot_difference(gaussian_state.get(), *gaussian_same).points.empty(),
          "Equivalent Gaussian payload failed immutable snapshot reuse");
  same_splats = std::make_shared<PointData>(*same_splats);
  same_splats->splats->harmonics[0][14].z += .1f;
  gaussian_update.points = {same_splats};
  const auto gaussian_changed = snapshot_state(gaussian_state, gaussian_update);
  require(snapshot_difference(gaussian_state.get(), *gaussian_changed).points.size() == 1,
          "Changed Gaussian higher SH coefficient was lost by snapshot equality");
  auto sphere = std::make_shared<PointData>(*splat_copy);
  sphere->splats.reset();
  gaussian_update.points = {sphere};
  const auto sphere_state = snapshot_state(gaussian_state, gaussian_update);
  require(snapshot_difference(gaussian_state.get(), *sphere_state).points.size() == 1 &&
              !decode_worker_update(encode_worker_update(WorkerUpdate{.scene = gaussian_update}))
                   .scene.points.at(0)
                   ->splats,
          "Sphere transition retained Gaussian parameters in snapshot or wire");
  auto moving = std::make_shared<PointData>(*splat_copy);
  moving->revision = 13;
  moving->motion.positions = {moving->positions, moving->positions};
  auto &moving_parameters = *moving->splats;
  moving_parameters.motion_scales = {moving_parameters.scales, moving_parameters.scales};
  moving_parameters.motion_rotations = {moving_parameters.rotations, moving_parameters.rotations};
  moving_parameters.motion_radiance = {moving_parameters.radiance, moving_parameters.radiance};
  gaussian_update.points = {moving};
  const auto moving_packet = encode_worker_update(WorkerUpdate{.scene = gaussian_update});
  const auto moving_copy = decode_worker_update(moving_packet).scene.points.at(0);
  require(moving_copy->splats == moving->splats && moving_copy->motion.positions == moving->motion.positions &&
              mesh_payload_heap(*moving->splats) > 448,
          "Gaussian sampled shape/radiance lost wire ownership or heap accounting");
  const auto moving_state = snapshot_state(gaussian_state, gaussian_update);
  auto changed_motion = std::make_shared<PointData>(*moving);
  changed_motion->splats->motion_radiance[0][0][3] += .1f;
  gaussian_update.points = {changed_motion};
  const auto motion_changed = snapshot_state(moving_state, gaussian_update);
  require(snapshot_difference(moving_state.get(), *motion_changed).points.size() == 1,
          "Changed Gaussian motion radiance was lost by snapshot equality");
  bool limited = false;
  try {
    decode_worker_update(splat_packet, nullptr, 32);
  } catch (const std::invalid_argument &) {
    limited = true;
  }
  require(limited, "Gaussian wire payload bypassed bounded allocation");
  std::cout << "{\"result\":\"passed\",\"contracts\":23,\"objects\":100000,\"retained_camera_states\":200,\"wire_"
               "objects\":0}\n";
}
} // namespace cycles_houdini::harness
