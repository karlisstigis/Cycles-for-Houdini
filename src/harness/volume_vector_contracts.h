// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/volume.h"
#include "cycles_houdini/snapshot.h"
#include <chrono>
#include <iostream>
#include <stdexcept>
#include <thread>

namespace cycles_houdini::harness {
inline void volume_vector_contracts(const std::string &device) {
  int checks = 0, rejected = 0;
  const auto require = [&](bool value, const char *message) {
    ++checks;
    if (!value)
      throw std::runtime_error(message);
  };
  VolumeData data;
  data.key = "vector_contract";
  data.material = "medium";
  data.revision = 1;
  auto density = openvdb::FloatGrid::create(0);
  density->tree().fill(openvdb::CoordBBox(openvdb::Coord(-8), openvdb::Coord(8)), .35f, true);
  data.fields.push_back({"density", density, 1});
  std::array<openvdb::FloatGrid::Ptr, 3> grids;
  for (int axis = 0; axis < 3; ++axis) {
    grids[axis] = openvdb::FloatGrid::create(.1f * (axis + 1));
    // Include compressed tiles, different topology and inactive values.
    grids[axis]->tree().fill(openvdb::CoordBBox(openvdb::Coord(0), openvdb::Coord(7)), float(axis + 1), true);
    grids[axis]->tree().setValueOff(openvdb::Coord(12), float(axis + 4));
    grids[axis]->tree().setValueOn(openvdb::Coord(axis - 3, 2, 3), float(axis + 7));
    data.fields.push_back({std::string("tint.") + "xyz"[axis], grids[axis], std::uint64_t(axis + 2)});
  }
  const auto source = volume_vector_sources(data, "tint");
  require(source.usable(), "Complete scalar triplet was not resolved");
  const auto vector = merge_volume_vector(source, "tint");
  require(vector->getName() == "tint" && vector->getVectorType() == openvdb::VEC_INVARIANT,
          "Derived vector metadata changed tuple semantics");
  for (const auto p : {openvdb::Coord(0), openvdb::Coord(7), openvdb::Coord(12), openvdb::Coord(-20),
                       openvdb::Coord(-3, 2, 3), openvdb::Coord(-2, 2, 3), openvdb::Coord(-1, 2, 3)}) {
    const auto value = vector->tree().getValue(p);
    bool active = false;
    for (int axis = 0; axis < 3; ++axis) {
      require(value[axis] == grids[axis]->tree().getValue(p),
              "Sparse assembly changed active/inactive/background values");
      active |= grids[axis]->tree().isValueOn(p);
    }
    require(vector->tree().isValueOn(p) == active, "Sparse assembly changed topology union");
  }
  require(data.fields.size() == 4 && data.fields[1].grid == grids[0], "Vector assembly consumed source scalar fields");
  require(volume_shader_field_error(data, "tint").empty() &&
              volume_shader_field_error(data, "tint", false, true).empty(),
          "Vector demand rejected a resolvable triplet");
  require(volume_shader_field_error(data, "tint.x", true, false).empty(),
          "Original scalar component became unavailable");
  require(!volume_shader_field_error(data, "tint", true, false).empty(), "Scalar demand guessed a vector value");
  auto partial = data;
  partial.fields.pop_back();
  require(volume_vector_sources(partial, "tint").issue.find("tint.z") != std::string::npos,
          "Missing component diagnostic omitted its field name");
  auto mismatched = data;
  auto translated = grids[2]->copyGridReplacingTransform(openvdb::math::Transform::createLinearTransform(2));
  mismatched.fields[3].grid = translated;
  require(!volume_vector_sources(mismatched, "tint").usable(), "Mismatched component maps were silently combined");
  auto wrong = data;
  wrong.fields[2].grid = openvdb::Vec3SGrid::create(openvdb::Vec3s(1));
  require(volume_vector_sources(wrong, "tint").issue.find("tint.y") != std::string::npos,
          "Component type diagnostic omitted its field name");
  auto shadow = data;
  shadow.fields.push_back({"tint", grids[0]});
  require(!volume_vector_sources(shadow, "tint").usable() &&
              !volume_shader_field_error(shadow, "tint", false, true).empty(),
          "Explicit scalar field did not retain precedence");
  auto reserved = data;
  for (int axis = 0; axis < 3; ++axis)
    reserved.fields[axis + 1].name = std::string("uv.") + "xyz"[axis];
  require(!volume_vector_sources(reserved, "uv").usable(), "Reserved surface attribute was synthesized on a volume");
  try {
    merge_volume_vector(volume_vector_sources(partial, "tint"), "tint");
  } catch (const std::invalid_argument &) {
    ++rejected;
  }
  require(rejected == 1, "Malformed assembly source was accepted");
  WorkerReply reply;
  reply.status.volume_vector_merges = 17;
  reply.status.volume_vector_fields = 3;
  reply.status.volume_vector_source_bytes = std::size_t(1ull << 37);
  reply.diagnostics.volume_vector_assembly_seconds = .125;
  auto packet = encode_worker_reply(reply);
  const auto decoded = decode_worker_reply(packet, {});
  require(decoded.status.volume_vector_merges == 17 && decoded.status.volume_vector_fields == 3 &&
              decoded.status.volume_vector_source_bytes == (1ull << 37) &&
              decoded.diagnostics.volume_vector_assembly_seconds == .125,
          "Vector cache telemetry changed in worker transport");
  packet[4] = std::byte{42};
  try {
    decode_worker_reply(packet, {});
  } catch (const std::invalid_argument &) {
    ++rejected;
  }
  require(rejected == 2, "Old worker status schema was accepted as protocol 44");
  WorkerUpdate transport;
  transport.scene.volumes = {std::make_shared<VolumeData>(data)};
  const auto transported = decode_worker_update(encode_worker_update(transport)).scene.volumes.at(0);
  for (std::size_t n = 0; n < data.fields.size(); ++n)
    require(transported->fields[n].revision == data.fields[n].revision &&
                transported->fields[n].name == data.fields[n].name &&
                transported->fields[n].grid != data.fields[n].grid,
            "Volume transport lost field identity or retained a borrowed grid");

  SessionConfig config;
  config.device_id = device;
  config.width = config.height = 8;
  config.samples = 4;
  RenderSession session(config);
  ShaderGraphData graph;
  graph.nodes = {{"field", "cycles/attribute/1", {{"attribute", std::string("tint")}}},
                 {"emit", "cycles/emission/1", {{"strength", 1.0f}}}};
  graph.links = {{{"field", "color"}, {"emit", "color"}}};
  graph.volume = ShaderEndpoint{"emit", "emission"};
  MaterialData material;
  material.key = "medium";
  material.kind = MaterialKind::Graph;
  material.graph = graph;
  SceneUpdate initial;
  initial.materials = {material};
  initial.volumes = {std::make_shared<VolumeData>(partial)};
  initial.objects = {{"volume", data.key}};
  initial.camera = CameraData{};
  initial.camera->transform[11] = 5;
  try {
    session.apply(initial);
  } catch (const std::invalid_argument &) {
    ++rejected;
  }
  require(rejected == 3 && session.status().generation == 0 && session.status().geometry_resources == 0,
          "Strict unresolved volume demand changed engine state");
  const auto wait = [&](const SceneUpdate &update) {
    const auto generation = session.apply(update);
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(40);
    while (true) {
      const auto status = session.status();
      if (status.state == SessionState::Failed)
        throw std::runtime_error(status.error);
      const auto frame = session.frame();
      if (frame && frame->generation == generation && frame->complete)
        return status;
      if (std::chrono::steady_clock::now() > deadline)
        throw std::runtime_error("Vector contract render timed out");
      std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
  };
  initial.volumes = {std::make_shared<VolumeData>(data)};
  const auto valid = wait(initial);
  require(valid.volume_vector_merges == 1 && valid.volume_vector_fields == 1 && valid.volume_vector_source_bytes > 0,
          "Demanded vector was not owned by the engine cache");
  SceneUpdate camera;
  camera.camera = initial.camera;
  camera.camera->transform[3] = .01f;
  const auto navigated = wait(camera);
  require(navigated.volume_vector_merges == valid.volume_vector_merges &&
              navigated.geometry_conversions == valid.geometry_conversions,
          "Camera edit assembled or uploaded retained vector fields");
  auto changed = *transported;
  changed.revision = 2;
  changed.fields[0].revision = 5;
  SceneUpdate density_only;
  density_only.volumes = {std::make_shared<VolumeData>(changed)};
  const auto reused = wait(density_only);
  require(reused.volume_vector_merges == valid.volume_vector_merges && reused.volume_vector_fields == 1,
          "Fresh transported headers rebuilt unchanged vector fields during an unrelated density edit");
  changed.revision = 3;
  changed.fields[1].revision = 6;
  auto replacement = openvdb::gridPtrCast<openvdb::FloatGrid>(changed.fields[1].grid->deepCopyGrid());
  replacement->tree().setValueOn(openvdb::Coord(0), .6f);
  changed.fields[1].grid = replacement;
  density_only.volumes = {std::make_shared<VolumeData>(changed)};
  const auto reassembled = wait(density_only);
  require(reassembled.volume_vector_merges == valid.volume_vector_merges + 1,
          "Changed vector field version did not rebuild its alias");
  SceneUpdate scalar;
  material.graph->nodes[0].parameters["attribute"] = std::string("tint.x");
  scalar.materials = {material};
  const auto direct = wait(scalar);
  require(direct.volume_vector_fields == 0 && direct.volume_vector_source_bytes == 0 &&
              direct.volume_vector_merges == 2,
          "Unused vector cache survived scalar-only shader demand");
  material.graph = graph;
  scalar.materials = {material};
  const auto restored = wait(scalar);
  require(restored.volume_vector_fields == 1 && restored.volume_vector_merges == 3,
          "Shader-only repair did not restore a vector from retained source fields");
  auto malformed = data;
  malformed.revision = 4;
  malformed.fields.push_back(malformed.fields[1]);
  SceneUpdate bad;
  bad.volumes = {std::make_shared<VolumeData>(malformed)};
  const auto before = session.frame();
  try {
    session.apply(bad);
  } catch (const std::invalid_argument &) {
    ++rejected;
  }
  require(rejected == 4 && session.frame() == before && session.status().volume_vector_merges == 3,
          "Malformed internal fields changed an accepted vector cache");
  malformed = data;
  malformed.revision = 4;
  malformed.fields[2].revision = malformed.fields[1].revision;
  bad.volumes = {std::make_shared<VolumeData>(malformed)};
  try {
    session.apply(bad);
  } catch (const std::invalid_argument &) {
    ++rejected;
  }
  require(rejected == 5 && session.frame() == before && session.status().volume_vector_merges == 3,
          "Conflicting internal field ownership changed an accepted vector cache");
  partial.recover_material = true;
  partial.revision = 4;
  bad.volumes = {std::make_shared<VolumeData>(partial)};
  const auto recovered = wait(bad);
  require(recovered.geometry_resources == 1 && recovered.volume_vector_fields == 0 &&
              recovered.source_diagnostics.size() == 1 &&
              recovered.source_diagnostics[0].code == "volume.shader_field_unavailable",
          "Native component failure did not retain a renderable volume");
  data.recover_material = true;
  data.revision = 5;
  bad.volumes = {std::make_shared<VolumeData>(data)};
  const auto repaired = wait(bad);
  require(repaired.volume_vector_fields == 1 && repaired.source_diagnostics.empty(),
          "Native source repair failed to restore the demanded vector");
  session.close();
  const auto closed = session.status();
  require(closed.volume_vector_fields == closed.volume_vector_source_bytes && closed.volume_vector_fields == 0 &&
              closed.owned_geometry_bytes == closed.device_allocation_bytes && closed.owned_geometry_bytes == 0,
          "Vector cache survived close");
  std::cout << "{\"result\":\"passed\",\"checks\":" << checks << ",\"strict_rejections\":" << rejected
            << ",\"worker_protocol\":" << worker_protocol_version << "}\n";
}
} // namespace cycles_houdini::harness
