// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/snapshot.h"
#include "../core/mesh_payload.h"
#include <bit>
#include <iostream>
#include <iterator>
#include <stdexcept>

namespace cycles_houdini::harness {
inline void mesh_payload_contracts() {
  int checks = 0, rejected = 0;
  const auto require = [&](bool condition, const char *message) {
    ++checks;
    if (!condition)
      throw std::runtime_error(message);
  };
  const auto refuses = [&](auto &&action) {
    bool caught = false;
    try {
      action();
    } catch (const std::invalid_argument &) {
      caught = true;
    }
    require(caught, "Malformed mesh payload ownership was accepted");
    ++rejected;
  };
  // Copy-on-write alone is unsafe when a mutable reference escapes before
  // the snapshot copy. Exercise each public mutable access route.
  for (int kind = 0; kind < 10; ++kind) {
    SharedArray<Float3> editable{{1, 2, 3}, {4, 5, 6}};
    Float3 *borrowed{};
    switch (kind) {
    case 0:
      borrowed = &editable[0];
      break;
    case 1:
      borrowed = &editable.at(0);
      break;
    case 2:
      borrowed = &editable.front();
      break;
    case 3:
      borrowed = &editable.back();
      break;
    case 4:
      borrowed = editable.data();
      break;
    case 5:
      borrowed = &*editable.begin();
      break;
    case 6:
      borrowed = &*(editable.end() - 1);
      break;
    case 7:
      borrowed = &*editable.rbegin();
      break;
    case 8:
      borrowed = &editable.emplace_back(Float3{7, 8, 9});
      break;
    case 9:
      borrowed = static_cast<std::vector<Float3> &>(editable).data();
      break;
    }
    const auto preserved = editable;
    const auto old = preserved;
    borrowed->x = 99;
    require(mesh_payload_equal(preserved, old), "Escaped mutable reference changed an accepted field");
    require(preserved.data() != std::as_const(editable).data(), "Exposed field storage was shared");
  }
  {
    SharedArray<Float3> editable;
    editable.push_back({1, 2, 3});
    editable.push_back({4, 5, 6});
    const auto accepted_field = editable;
    require(accepted_field.data() == std::as_const(editable).data(),
            "Unexposed immutable fields did not share storage");
    editable[0].x = 9;
    require(accepted_field[0].x == 1, "Later mutable access changed immutable shared storage");
    auto other = accepted_field;
    auto inserted = other.insert(other.cbegin() + 1, Float3{7, 8, 9});
    const auto insertion_snapshot = other;
    inserted->x = 88;
    require(insertion_snapshot[1].x == 7 && accepted_field.size() == 2, "Inserted reference changed a snapshot");
    auto erased = other.erase(other.cbegin());
    const auto erase_snapshot = other;
    erased->x = 77;
    require(erase_snapshot[0].x == 88, "Erase return iterator changed an immutable snapshot");
    std::vector<Float3> external{{1, 2, 3}};
    auto *escaped = external.data();
    SharedArray<Float3> moved(std::move(external));
    const auto moved_snapshot = moved;
    escaped->x = 9;
    require(moved_snapshot[0].x == 1, "External vector move exposed an accepted snapshot");
    SharedArray<std::vector<Float3>> nested{{{1, 2, 3}}};
    auto &inner = nested.front().front();
    const auto nested_snapshot = nested;
    inner.x = 9;
    require(nested_snapshot[0][0].x == 1, "Escaped inner reference changed accepted shutter data");
    auto moved_again = std::move(moved);
    const auto final_snapshot = moved_again;
    escaped->x = 17;
    require(final_snapshot[0].x == 9, "Managed move lost mutable exposure history");
  }
  {
    using Inner = std::vector<Float3>;
    using Outer = SharedArray<Inner>;
    const auto imported = [&](auto import) {
      Inner external{{1, 2, 3}};
      auto *borrowed = external.data();
      auto editable = import(external);
      const auto accepted_field = editable;
      borrowed->x = 99;
      require(accepted_field[0][0].x == 1, "Moved inner storage exposed an accepted shutter field");
      require(accepted_field[0].data() != std::as_const(editable)[0].data(),
              "Imported inner mutable storage was shared");
    };
    imported([](Inner &v) {
      Outer result;
      result.push_back(std::move(v));
      return result;
    });
    imported([](Inner &v) {
      std::vector<Inner> rows;
      rows.push_back(std::move(v));
      return Outer(std::move(rows));
    });
    imported([](Inner &v) {
      std::vector<Inner> rows;
      rows.push_back(std::move(v));
      return Outer(std::make_move_iterator(rows.begin()), std::make_move_iterator(rows.end()));
    });
    imported([](Inner &v) {
      std::vector<Inner> rows;
      rows.push_back(std::move(v));
      Outer result;
      result.assign(std::make_move_iterator(rows.begin()), std::make_move_iterator(rows.end()));
      return result;
    });
    imported([](Inner &v) {
      Outer result;
      result.emplace_back(std::move(v));
      return result;
    });
    imported([](Inner &v) {
      Outer result;
      result.push_back(std::move(v));
      Outer moved(std::move(result)), swapped;
      swapped.swap(moved);
      return swapped;
    });
  }
  auto source = std::make_shared<MeshData>();
  source->key = "mesh/accepted-source";
  source->revision = 7;
  for (int i = 0; i < 4096; ++i)
    source->positions.push_back({float(i), float(i % 7), float(i % 11)});
  for (int i = 0; i < 2048; ++i)
    source->triangles.push_back({i, i + 1, i + 2});
  source->corner_normals.assign(6144, {0, 0, 1});
  source->corner_uv.assign(6144, {.2f, .4f});
  source->corner_colors.assign(6144, {.3f, .5f, .7f});
  source->triangle_material.assign(2048, 0);
  source->materials = {"material/original"};
  source->motion.positions = {source->positions};
  source->motion.radii = {{.2f, .4f}};
  source->motion_corner_normals = {source->corner_normals};
  source->motion_corner_uv = {source->corner_uv};
  source->motion_corner_colors = {source->corner_colors};
  source->attributes = {{"field", 3, {{.1f, .2f, .3f}}, {{{.4f, .5f, .6f}}}, false}};
  source->nominal_pose = MeshNominalPose{source->positions, source->corner_normals, source->corner_uv,
                                         source->corner_colors, source->attributes};
  source->pointiness.assign(4096, .1f);
  source->instance_attributes = {{"instance", 3}};
  source = std::make_shared<MeshData>(*source); // Freeze any construction-time mutable borrows.
  MeshPayloadSources accepted{{source->key, source}};
  const auto target = [&]() {
    auto m = std::make_shared<MeshData>(*source);
    m->key = "mesh/new-binding";
    m->revision = 11;
    m->materials = {"material/red", "material/blue"};
    m->triangle_material.assign(2048, 1);
    m->instance_attributes = {{"other-instance", 2}};
    return m;
  };
  const auto packet = [&](std::shared_ptr<const MeshData> mesh, const MeshPayloadSources *prior = nullptr) {
    WorkerUpdate v;
    v.scene.meshes = {std::move(mesh)};
    return encode_worker_update(v, prior);
  };
  const auto tag_at = [&](const auto &bytes, const std::string &key) {
    const std::string raw(reinterpret_cast<const char *>(bytes.data()), bytes.size());
    const auto pos = raw.find(key);
    require(pos != std::string::npos && pos + key.size() + sizeof(std::uint64_t) < bytes.size(),
            "Missing independent mesh wire marker");
    return pos + key.size() + sizeof(std::uint64_t);
  };
  const auto verify = [&](const MeshData &expected, const MeshData &actual) {
    require(same_mesh_wire_payload(expected, actual), "Referenced mesh arrays or shutter fields changed");
    require(expected.key == actual.key && expected.revision == actual.revision &&
                expected.materials == actual.materials && expected.triangle_material == actual.triangle_material &&
                expected.instance_attributes == actual.instance_attributes,
            "Geometry reuse borrowed material identity, face assignments or instance declarations");
    require(bool(expected.subdivision) == bool(actual.subdivision) &&
                (!expected.subdivision || expected.subdivision->face_material == actual.subdivision->face_material),
            "Subdivision face bindings changed");
  };
  auto changed = target();
  const auto warm = packet(changed, &accepted), full = packet(changed);
  require(std::to_integer<int>(warm[tag_at(warm, changed->key)]) == 2, "Accepted source was not referenced");
  require(warm.size() < 4096 && full.size() > 500000, "Binding-only mesh edit still transports its dense arrays");
  verify(*changed, *decode_worker_update(warm, &accepted).scene.meshes.front());
  const auto preserved = *source;
  require(same_mesh_wire_payload(*source, preserved), "Encoding mutated accepted source");
  refuses([&]() { decode_worker_update(warm); });
  MeshPayloadSources absent;
  refuses([&]() { decode_worker_update(warm, &absent); });
  auto stale = std::make_shared<MeshData>(*source);
  ++stale->revision;
  MeshPayloadSources stale_state{{source->key, stale}};
  refuses([&]() { decode_worker_update(warm, &stale_state); });
  auto wrong = std::make_shared<MeshData>(*source);
  wrong->key = "wrong-owner";
  MeshPayloadSources wrong_state{{source->key, wrong}};
  refuses([&]() { decode_worker_update(warm, &wrong_state); });
  refuses([&]() { packet(changed, &wrong_state); });
  refuses([&]() { decode_worker_update(warm, &accepted, 65536); });
  refuses([&]() { decode_worker_update(warm, &accepted, 0); });
  auto bad_tag = warm;
  bad_tag[tag_at(warm, changed->key)] = std::byte{99};
  refuses([&]() { decode_worker_update(bad_tag, &accepted); });
  auto bad_binding = warm;
  const std::string raw_binding(reinterpret_cast<const char *>(bad_binding.data()), bad_binding.size());
  const auto binding_tag = raw_binding.find("material/blue") + std::string("material/blue").size();
  require(binding_tag < bad_binding.size(), "Missing independent material binding wire marker");
  bad_binding[binding_tag] = std::byte{99};
  refuses([&]() { decode_worker_update(bad_binding, &accepted); });
  for (const auto length : {std::size_t(0), warm.size() / 2, warm.size() - 1})
    refuses([&]() { decode_worker_update(std::span(warm.data(), length), &accepted); });
  auto trailing = warm;
  trailing.push_back(std::byte{});
  refuses([&]() { decode_worker_update(trailing, &accepted); });
  WorkerUpdate cold;
  cold.scene.meshes = {source, changed};
  const auto shared = encode_worker_update(cold);
  require(std::to_integer<int>(shared[tag_at(shared, changed->key)]) == 1,
          "Packet-local common arrays were duplicated");
  require(shared.size() < full.size() + 4096, "Packet-local payload reuse did not bound transport");
  const auto restored = decode_worker_update(shared);
  verify(*source, *restored.scene.meshes[0]);
  verify(*changed, *restored.scene.meshes[1]);
  const auto control = encode_worker_update(cold, &accepted, false);
  require(control.size() > full.size() * 1.9, "Full-payload measurement control reused arrays");
  const auto control_read = decode_worker_update(control);
  verify(*source, *control_read.scene.meshes[0]);
  verify(*changed, *control_read.scene.meshes[1]);
  auto forward = shared;
  const auto index = tag_at(forward, changed->key) + 1;
  const std::uint64_t invalid = 99;
  std::memcpy(forward.data() + index, &invalid, sizeof(invalid));
  refuses([&]() { decode_worker_update(forward); });
  // Every changed field is transported without borrowing its stale value.
  int mutation = 0;
  const auto distinct = [&](auto &&mutate) {
    ++mutation;
    auto data = target();
    data->key = source->key;
    data->revision = source->revision + 1;
    mutate(*data);
    const auto bytes = packet(data, &accepted);
    require(bytes.size() < packet(data).size(), "Field edit unnecessarily repeated the full mesh payload");
    verify(*data, *decode_worker_update(bytes, &accepted).scene.meshes.front());
  };
  distinct([](auto &m) { m.positions[4].x += .1f; });
  distinct([](auto &m) { m.triangles[4].b = 17; });
  distinct([](auto &m) { m.corner_normals[7].x = .2f; });
  distinct([](auto &m) { m.corner_uv[9].x = .7f; });
  distinct([](auto &m) { m.corner_colors[9].z = .8f; });
  distinct([](auto &m) { m.smooth = true; });
  distinct([](auto &m) { m.motion.positions[0][4].y += .1f; });
  distinct([](auto &m) { m.motion.radii[0][0] = .8f; });
  distinct([](auto &m) { m.motion_corner_normals[0][8].x = .2f; });
  distinct([](auto &m) { m.motion_corner_uv[0][8].x = .8f; });
  distinct([](auto &m) { m.motion_corner_colors[0][8].x = .9f; });
  distinct([](auto &m) { m.motion_uv_complete = false; });
  distinct([](auto &m) { m.motion_color_complete = false; });
  distinct([](auto &m) { m.motion_blur = false; });
  distinct([](auto &m) { m.pointiness[4] = .3f; });
  distinct([](auto &m) { m.attributes[0].values[0].x = .9f; });
  distinct([](auto &m) { m.attributes[0].name = "renamed"; });
  distinct([](auto &m) { m.attributes[0].components = 2; });
  distinct([](auto &m) { m.attributes[0].motion_values[0][0].x = .9f; });
  distinct([](auto &m) { m.attributes[0].motion_complete = true; });
  distinct([](auto &m) { m.nominal_pose->positions[4].x = .9f; });
  distinct([](auto &m) { m.nominal_pose->corner_normals[4].x = .9f; });
  distinct([](auto &m) { m.nominal_pose->corner_uv[4].x = .9f; });
  distinct([](auto &m) { m.nominal_pose->corner_colors[4].x = .9f; });
  distinct([](auto &m) { m.nominal_pose->attributes[0].values[0].x = .9f; });
  distinct([](auto &m) { m.nominal_pose.reset(); });
  distinct([](auto &m) {
    const std::uint32_t bits = 0x80000000u;
    std::memcpy(&m.corner_normals[4].x, &bits, sizeof(bits));
  });
  distinct([](auto &m) { m.corner_uv.clear(); });
  distinct([](auto &m) { m.corner_colors.clear(); });
  distinct([](auto &m) { m.attributes.clear(); });
  distinct([](auto &m) { m.motion.positions.clear(); });
  distinct([](auto &m) {
    m.attributes.push_back({"second", 1, {{.3f, 0, 0}}});
    std::reverse(m.attributes.begin(), m.attributes.end());
  });
  // UV-only nominal edits and named-field edits retain unrelated dense buffers.
  {
    auto data = target();
    data->nominal_pose->corner_uv[0].x = .8f;
    const auto wire = packet(data, &accepted);
    require(wire.size() < 60000, "Nominal UV edit repeated positions");
    const auto decoded = decode_worker_update(wire, &accepted).scene.meshes.front();
    require(decoded->positions.storage_identity() == source->positions.storage_identity(),
            "Unchanged positions were deep-copied");
    require(decoded->triangles.storage_identity() == source->triangles.storage_identity(),
            "Unchanged triangles were deep-copied");
    require(decoded->nominal_pose->positions.storage_identity() == source->nominal_pose->positions.storage_identity(),
            "Nominal UV edit copied nominal positions");
    verify(*data, *decoded);
    auto invalid_mask = warm;
    const auto mask_at = tag_at(warm, changed->key) + 1 + 8 + source->key.size() + 8;
    invalid_mask[mask_at + 7] |= std::byte{128};
    refuses([&]() { decode_worker_update(invalid_mask, &accepted); });
    auto no_base = full;
    const auto inline_mask = tag_at(full, changed->key) + 1;
    no_base[inline_mask] = std::byte{};
    refuses([&]() { decode_worker_update(no_base); });
  }
  // Subdivision topology/creases/provenance are shape; face material is a binding.
  source = std::make_shared<MeshData>(*source);
  source->revision = 17;
  source->subdivision.emplace();
  auto &sub = *source->subdivision;
  sub.settings.type = SubdivisionType::CatmullClark;
  sub.face_sizes = {4};
  sub.face_corners = {0, 1, 2, 3};
  sub.face_provenance = {{{4, 2}}};
  sub.face_material = {0};
  sub.edge_creases = {{0, 1, .5f}};
  sub.vertex_creases = {{1, .3f}};
  accepted = {{source->key, source}};
  changed = target();
  changed->subdivision->face_material = {1};
  const auto sub_wire = packet(changed, &accepted);
  require(std::to_integer<int>(sub_wire[tag_at(sub_wire, changed->key)]) == 2, "Subdivision binding reused no shape");
  verify(*changed, *decode_worker_update(sub_wire, &accepted).scene.meshes.front());
  distinct([](auto &m) { m.subdivision->settings.dicing_rate = .7f; });
  distinct([](auto &m) { m.subdivision->face_corners[0] = 3; });
  distinct([](auto &m) { m.subdivision->face_sizes[0] = 3; });
  distinct([](auto &m) { m.subdivision->face_provenance[0][0] = 8; });
  distinct([](auto &m) { m.subdivision->edge_creases[0].sharpness = .8f; });
  distinct([](auto &m) { m.subdivision->vertex_creases[0].sharpness = .8f; });
  WorkerUpdate replace;
  replace.scene.deleted_geometry = {source->key};
  replace.scene.meshes = {changed};
  const auto read = decode_worker_update(encode_worker_update(replace, &accepted), &accepted);
  const auto next = mesh_payload_sources(accepted, read.scene);
  require(next.size() == 1 && next.contains(changed->key) && !next.contains(source->key),
          "Accepted payload ledger did not retire obsolete geometry");
  verify(*changed, *next.at(changed->key));
  require(accepted.size() == 1 && accepted.contains(source->key), "Candidate payload ledger changed accepted state");
  auto no_revision = std::make_shared<MeshData>(*source);
  no_revision->positions[3].x = .9f;
  SceneUpdate invalid_revision;
  invalid_revision.meshes = {no_revision};
  refuses([&]() { mesh_payload_sources(accepted, invalid_revision); });
  SceneUpdate duplicate;
  duplicate.meshes = {source, source};
  refuses([&]() { mesh_payload_sources(accepted, duplicate); });
  auto material_only = std::make_shared<MeshData>(*source);
  material_only->materials = {"replacement"};
  material_only->triangle_material.clear();
  SceneUpdate binding;
  binding.meshes = {material_only};
  const auto rebound = mesh_payload_sources(accepted, binding);
  require(rebound.at(source->key)->materials == material_only->materials, "Binding-only accepted update was rejected");
  // The receiver's transport ledger reuses the renderer's independent,
  // immutable source owner rather than retaining a second decoded payload.
  SessionConfig config;
  config.device_id = "CPU";
  config.width = 8;
  config.height = 8;
  config.samples = 1;
  config.threads = 2;
  RenderSession renderer(config);
  auto input = std::make_shared<MeshData>();
  input->key = "owned/mesh";
  input->revision = 1;
  input->positions = {{-1, -1, 0}, {1, -1, 0}, {0, 1, 0}};
  input->triangles = {{0, 1, 2}};
  input->materials = {"owned/material"};
  SceneUpdate initial;
  initial.meshes = {input};
  MaterialData material;
  material.key = "owned/material";
  material.kind = MaterialKind::Emission;
  initial.materials = {material};
  ObjectData object;
  object.key = "owned/object";
  object.geometry_key = input->key;
  initial.objects = {object};
  renderer.apply(initial);
  const auto owners = renderer.retained_mesh_sources();
  require(owners.size() == 1 && owners.at(input->key).get() != input.get(),
          "Renderer did not isolate the caller's mutable mesh owner");
  require(same_mesh_wire_payload(*owners.at(input->key), *input), "Renderer source changed received payload");
  input->positions[0].x = 2;
  ++input->revision;
  require(owners.at(input->key)->positions[0].x == -1, "Caller mutation changed accepted geometry");
  material.color = {.2f, .4f, .6f};
  SceneUpdate shade;
  shade.materials = {material};
  renderer.apply(shade);
  require(renderer.retained_mesh_sources().at(input->key) == owners.at(input->key),
          "Material edit copied the retained transport source");
  SceneUpdate geometry;
  geometry.meshes = {input};
  renderer.apply(geometry);
  require(renderer.retained_mesh_sources().at(input->key) != owners.at(input->key) &&
              renderer.retained_mesh_sources().at(input->key)->positions[0].x == 2,
          "Real geometry update did not replace its immutable source");
  auto renamed = std::make_shared<MeshData>(*input);
  renamed->key = "owned/rebound";
  ++renamed->revision;
  SceneUpdate handoff;
  handoff.deleted_geometry = {input->key};
  handoff.meshes = {renamed};
  object.geometry_key = renamed->key;
  handoff.objects = {object};
  renderer.apply(handoff);
  const auto transferred = renderer.retained_mesh_sources();
  require(transferred.size() == 1 && transferred.contains(renamed->key) &&
              transferred.at(renamed->key).get() != renamed.get(),
          "Binding handoff borrowed the sender's mutable source");
  renamed->positions[0].x = 3;
  ++renamed->revision;
  require(transferred.at(renamed->key)->positions[0].x == 2, "Caller mutation changed a binding handoff source");
  SceneUpdate remove;
  remove.deleted_objects = {object.key};
  remove.deleted_geometry = {renamed->key};
  renderer.apply(remove);
  require(renderer.retained_mesh_sources().empty() && owners.at(input->key)->positions[0].x == -1,
          "Retirement broke a retained source owner");
  renderer.close();
  require(renderer.status().owned_geometry_bytes == 0 && renderer.status().device_allocation_bytes == 0,
          "Source-owner lifecycle leaked renderer allocations");
  std::cout << "{\"result\":\"passed\",\"checks\":" << checks << ",\"strict_rejections\":" << rejected
            << ",\"full_bytes\":" << full.size() << ",\"binding_bytes\":" << warm.size()
            << ",\"packet_shared_bytes\":" << shared.size() << ",\"worker_protocol\":" << worker_protocol_version << "}"
            << std::endl;
}
} // namespace cycles_houdini::harness
