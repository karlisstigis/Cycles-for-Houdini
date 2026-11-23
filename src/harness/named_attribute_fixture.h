// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "shader_fixture.h"
#include "cycles_houdini/snapshot.h"
#include <iostream>
#include <limits>

namespace cycles_houdini::harness {
inline void named_attribute_contract_test(const std::string &device) {
  SessionConfig config;
  config.device_id = device;
  config.width = 16;
  config.height = 16;
  config.samples = 1;
  config.interactive = false;
  RenderSession session(config);
  auto scene = shader_reference_fixture();
  auto mesh = std::make_shared<MeshData>(*scene.meshes.front());
  mesh->attributes = {{"rest", 3, std::vector<Float3>(6, {.2f, .4f, .8f})}};
  scene.meshes = {mesh};
  auto point = std::make_shared<PointData>();
  point->key = "field-points";
  point->revision = 1;
  point->material = "reference";
  point->positions = {{0, 0, 0}};
  point->radii = {.1f};
  point->attributes = {{"count", 1, {{1, 0, 0}}}};
  scene.points = {point};
  auto curve = std::make_shared<CurveData>();
  curve->key = "field-curves";
  curve->revision = 1;
  curve->material = "reference";
  curve->positions = {{0, 0, 0}, {0, 1, 0}};
  curve->radii = {.1f, .1f};
  curve->first_key = {0};
  curve->attributes = {{"uv2", 2, {{.2f, .4f, 0}, {.8f, .4f, 0}}}};
  scene.curves = {curve};
  WorkerUpdate packet;
  packet.scene = scene;
  const auto wire = encode_worker_update(packet);
  const auto restored = decode_worker_update(wire);
  if (restored.scene.meshes[0]->attributes != mesh->attributes ||
      restored.scene.points[0]->attributes != point->attributes ||
      restored.scene.curves[0]->attributes != curve->attributes)
    throw std::runtime_error("Named fields changed in worker transport");
  const auto generation = session.apply(restored.scene);
  unsigned rejected = 0;
  const auto corrupt = [](auto &fields, int index) {
    switch (index) {
    case 0:
      fields[0].name.clear();
      break;
    case 1:
      fields[0].name = std::string(257, 'x');
      break;
    case 2:
      fields.push_back(fields[0]);
      break;
    case 3:
      fields[0].components = 4;
      break;
    case 4:
      fields[0].values.pop_back();
      break;
    case 5:
      fields[0].values[0].x = std::numeric_limits<float>::quiet_NaN();
      break;
    case 6:
      fields[0].name = std::string("bad\0name", 8);
      break;
    }
  };
  for (int kind = 0; kind < 3; ++kind)
    for (int issue = 0; issue < 7; ++issue) {
      SceneUpdate invalid;
      if (kind == 0) {
        auto data = std::make_shared<MeshData>(*mesh);
        ++data->revision;
        corrupt(data->attributes, issue);
        invalid.meshes = {data};
      } else if (kind == 1) {
        auto data = std::make_shared<PointData>(*point);
        ++data->revision;
        corrupt(data->attributes, issue);
        invalid.points = {data};
      } else {
        auto data = std::make_shared<CurveData>(*curve);
        ++data->revision;
        corrupt(data->attributes, issue);
        invalid.curves = {data};
      }
      try {
        session.apply(invalid);
        throw std::runtime_error("Invalid owned field was accepted");
      } catch (const SceneUpdateRejected &) {
        ++rejected;
      }
      if (session.status().generation != generation)
        throw std::runtime_error("Rejected field changed the accepted generation");
    }
  for (int issue = 0; issue < 2; ++issue) {
    auto invalid = wire;
    if (issue == 0)
      invalid.pop_back();
    else
      invalid[4] ^= std::byte{1};
    try {
      decode_worker_update(invalid);
      throw std::runtime_error("Malformed worker packet accepted");
    } catch (const std::invalid_argument &) {
      ++rejected;
    }
  }
  for (int issue = 0; issue < 7; ++issue) {
    auto data = std::make_shared<MeshData>(*mesh);
    ++data->revision;
    data->nominal_pose =
        MeshNominalPose{data->positions, data->corner_normals, data->corner_uv, data->corner_colors, data->attributes};
    corrupt(data->nominal_pose->attributes, issue);
    SceneUpdate invalid;
    invalid.meshes = {data};
    try {
      session.apply(invalid);
      throw std::runtime_error("Invalid nominal field was accepted");
    } catch (const SceneUpdateRejected &) {
      ++rejected;
    }
    if (session.status().generation != generation)
      throw std::runtime_error("Rejected nominal field changed the accepted generation");
  }
  for (int kind = 0; kind < 3; ++kind)
    for (int issue = 0; issue < 4; ++issue) {
      const auto sampled = [&](auto &data) {
        data.motion.positions.assign(2, data.positions);
        auto &field = data.attributes[0];
        field.motion_values.assign(2, field.values);
        if (issue == 0)
          field.motion_values.pop_back();
        else if (issue == 1)
          field.motion_values[0].pop_back();
        else if (issue == 2)
          field.motion_values[0][0].x = std::numeric_limits<float>::infinity();
        else
          field.motion_complete = false;
      };
      SceneUpdate invalid;
      if (kind == 0) {
        auto data = std::make_shared<MeshData>(*mesh);
        ++data->revision;
        sampled(*data);
        invalid.meshes = {data};
      } else if (kind == 1) {
        auto data = std::make_shared<PointData>(*point);
        ++data->revision;
        sampled(*data);
        invalid.points = {data};
      } else {
        auto data = std::make_shared<CurveData>(*curve);
        ++data->revision;
        sampled(*data);
        invalid.curves = {data};
      }
      try {
        session.apply(invalid);
        throw std::runtime_error("Invalid sampled field accepted");
      } catch (const SceneUpdateRejected &) {
        ++rejected;
      }
      if (session.status().generation != generation)
        throw std::runtime_error("Rejected sampled field changed accepted generation");
    }
  auto sampled_scene = scene;
  // Source recovery must not weaken the strict owned-buffer contract. Hair
  // root UVs and key Cd deliberately use different element counts here.
  for (int kind = 0; kind < 2; ++kind)
    for (int channel = 0; channel < 2; ++channel)
      for (int issue = 0; issue < 4; ++issue) {
        const auto sampled = [&](auto &data) {
          data.motion.positions.assign(2, data.positions);
          const auto corrupt_stream = [&](auto &values, bool &complete) {
            if (issue == 0)
              values.pop_back();
            else if (issue == 1)
              values[0].pop_back();
            else if (issue == 2)
              values[0][0].x = std::numeric_limits<float>::infinity();
            else
              complete = false;
          };
          if (channel == 0) {
            data.colors.assign(data.positions.size(), {.2f, .4f, .8f});
            data.motion_colors.assign(2, data.colors);
            corrupt_stream(data.motion_colors, data.motion_color_complete);
          } else {
            data.uv.assign(1, {.2f, .4f});
            data.motion_uv.assign(2, data.uv);
            corrupt_stream(data.motion_uv, data.motion_uv_complete);
          }
        };
        SceneUpdate invalid;
        if (kind == 0) {
          auto data = std::make_shared<PointData>(*point);
          ++data->revision;
          sampled(*data);
          invalid.points = {data};
        } else {
          auto data = std::make_shared<CurveData>(*curve);
          ++data->revision;
          sampled(*data);
          invalid.curves = {data};
        }
        try {
          session.apply(invalid);
          throw std::runtime_error("Invalid sampled UV/color stream accepted");
        } catch (const SceneUpdateRejected &) {
          ++rejected;
        }
        if (session.status().generation != generation)
          throw std::runtime_error("Rejected sampled UV/color changed accepted generation");
      }
  const auto sample = [](auto data) {
    auto copy = std::make_shared<std::remove_const_t<typename decltype(data)::element_type>>(*data);
    copy->motion.positions.assign(2, copy->positions);
    for (auto &field : copy->attributes)
      field.motion_values.assign(2, field.values);
    if constexpr (requires { copy->colors; }) {
      copy->colors.assign(copy->positions.size(), {.2f, .4f, .8f});
      copy->uv.assign(1, {.2f, .4f});
      copy->motion_colors.assign(2, copy->colors);
      copy->motion_uv.assign(2, copy->uv);
    }
    return copy;
  };
  sampled_scene.meshes = {sample(scene.meshes[0])};
  sampled_scene.points = {sample(scene.points[0])};
  sampled_scene.curves = {sample(scene.curves[0])};
  WorkerUpdate sampled_packet;
  sampled_packet.scene = sampled_scene;
  const auto sample_wire = encode_worker_update(sampled_packet);
  const auto sample_read = decode_worker_update(sample_wire);
  if (sample_read.scene.meshes[0]->attributes != sampled_scene.meshes[0]->attributes ||
      sample_read.scene.points[0]->attributes != sampled_scene.points[0]->attributes ||
      sample_read.scene.curves[0]->attributes != sampled_scene.curves[0]->attributes)
    throw std::runtime_error("Sampled fields changed in worker transport");
  if (sample_read.scene.points[0]->motion_colors != sampled_scene.points[0]->motion_colors ||
      sample_read.scene.points[0]->motion_uv != sampled_scene.points[0]->motion_uv ||
      sample_read.scene.curves[0]->motion_colors != sampled_scene.curves[0]->motion_colors ||
      sample_read.scene.curves[0]->motion_uv != sampled_scene.curves[0]->motion_uv)
    throw std::runtime_error("Sampled point/hair UV/color changed in worker transport");
  session.close();
  std::cout << "{\"result\":\"passed\",\"rejected_inputs\":" << rejected
            << ",\"wire_roundtrip\":true,\"sampled_wire_roundtrip\":true}\n";
}
} // namespace cycles_houdini::harness
