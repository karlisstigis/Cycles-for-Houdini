// SPDX-License-Identifier: Apache-2.0
#pragma once
// Protocol 88 volume transport: the raw sparse form must decode to exactly the
// source grid (topology, tiles, every leaf value active or inactive, background,
// transform and metadata), serial and parallel encodes must be byte-identical,
// and unsupported grids must keep the OpenVDB stream form.
#include "cycles_houdini/snapshot.h"
#include "cycles_houdini/volume.h"
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>

namespace cycles_houdini::harness {
inline void volume_wire_env(const char *name, const char *value) {
#ifdef _WIN32
  _putenv_s(name, value ? value : "");
#else
  if (value)
    setenv(name, value, 1);
  else
    unsetenv(name);
#endif
}
template <class Grid>
void volume_wire_compare_typed(const Grid &a, const Grid &b, bool stream_metadata, const std::string &label) {
  const auto fail = [&](const std::string &what) { throw std::runtime_error(label + ": " + what); };
  using Value = typename Grid::ValueType;
  const auto same = [](const Value &x, const Value &y) { return std::memcmp(&x, &y, sizeof(Value)) == 0; };
  if (!same(a.background(), b.background()))
    fail("background differs");
  if (a.transform() != b.transform())
    fail("transform differs");
  const auto ma = a.transform().baseMap()->getAffineMap()->getMat4(),
             mb = b.transform().baseMap()->getAffineMap()->getMat4();
  if (std::memcmp(ma.asPointer(), mb.asPointer(), sizeof(double) * 16) != 0)
    fail("transform matrix bits differ");
  if (a.getName() != b.getName() || a.getGridClass() != b.getGridClass() || a.getVectorType() != b.getVectorType())
    fail("name, class or vector type differs");
  std::size_t source_meta = 0;
  for (auto it = a.beginMeta(); it != a.endMeta(); ++it) {
    ++source_meta;
    const auto other = b[it->first];
    if (!other || other->typeName() != it->second->typeName() || other->str() != it->second->str())
      fail("metadata differs: " + it->first);
  }
  std::size_t decoded_meta = 0;
  for (auto it = b.beginMeta(); it != b.endMeta(); ++it)
    if (!(stream_metadata && it->first.rfind("file_", 0) == 0) &&
        !(stream_metadata && it->first == "is_saved_as_half_float"))
      ++decoded_meta;
  if (decoded_meta != source_meta)
    fail("metadata entry count differs");
  if (!a.tree().hasSameTopology(b.tree()) || a.tree().leafCount() != b.tree().leafCount() ||
      a.tree().activeTileCount() != b.tree().activeTileCount())
    fail("topology differs");
  auto ia = a.tree().cbeginValueAll();
  auto ib = b.tree().cbeginValueAll();
  std::size_t values = 0;
  for (; ia && ib; ++ia, ++ib, ++values)
    if (ia.getCoord() != ib.getCoord() || ia.getDepth() != ib.getDepth() || ia.isValueOn() != ib.isValueOn() ||
        !same(*ia, *ib))
      fail("value or state differs at " + std::to_string(ia.getCoord().x()) + "," + std::to_string(ia.getCoord().y()) +
           "," + std::to_string(ia.getCoord().z()));
  if (ia || ib)
    fail("value iteration length differs");
  if (!values && a.tree().leafCount())
    fail("no values compared");
}
inline void volume_wire_compare(const openvdb::GridBase::ConstPtr &a, const openvdb::GridBase::ConstPtr &b,
                                bool stream_metadata, const std::string &label) {
  if (!a || !b || a->type() != b->type())
    throw std::runtime_error(label + ": grid type differs");
  if (const auto x = openvdb::gridConstPtrCast<openvdb::FloatGrid>(a))
    volume_wire_compare_typed(*x, *openvdb::gridConstPtrCast<openvdb::FloatGrid>(b), stream_metadata, label);
  else if (const auto x = openvdb::gridConstPtrCast<openvdb::Vec3SGrid>(a))
    volume_wire_compare_typed(*x, *openvdb::gridConstPtrCast<openvdb::Vec3SGrid>(b), stream_metadata, label);
  else if (const auto x = openvdb::gridConstPtrCast<openvdb::DoubleGrid>(a))
    volume_wire_compare_typed(*x, *openvdb::gridConstPtrCast<openvdb::DoubleGrid>(b), stream_metadata, label);
  else
    throw std::runtime_error(label + ": unexpected grid type");
}
inline void volume_wire_contracts() {
  openvdb::initialize();
  int checks = 0;
  const auto require = [&](bool value, const std::string &message) {
    ++checks;
    if (!value)
      throw std::runtime_error(message);
  };
  // 1. Float fog volume, 128^3 voxels (4096 leaves: enough for the parallel path).
  auto fog = openvdb::FloatGrid::create(0.f);
  {
    auto accessor = fog->getAccessor();
    for (int z = 0; z < 128; ++z)
      for (int y = 0; y < 128; ++y)
        for (int x = 0; x < 128; ++x)
          accessor.setValueOn(openvdb::Coord(x - 64, y - 40, z), 0.001f * float((x * 7 + y * 13 + z * 31) % 997));
  }
  fog->setName("density");
  fog->setGridClass(openvdb::GRID_FOG_VOLUME);
  fog->insertMeta("houdini_custom", openvdb::StringMetadata("kept"));
  // 2. Vec3 velocity grid with an invariant vector type.
  auto velocity = openvdb::Vec3SGrid::create(openvdb::Vec3s(0.f));
  {
    auto accessor = velocity->getAccessor();
    for (int z = 0; z < 24; ++z)
      for (int y = 0; y < 20; ++y)
        for (int x = 0; x < 30; ++x)
          accessor.setValueOn(openvdb::Coord(x, y, z), openvdb::Vec3s(0.1f * x, -0.2f * y, 0.05f * z - 0.3f));
  }
  velocity->setName("velocity");
  velocity->setVectorType(openvdb::VEC_INVARIANT);
  // 3. Sparse grid with root tiles, internal tiles (active and inactive) and leaves.
  auto sparse = openvdb::FloatGrid::create(0.25f);
  sparse->tree().fill(openvdb::CoordBBox(openvdb::Coord(-8192), openvdb::Coord(-4097)), 0.5f, true);
  sparse->tree().fill(openvdb::CoordBBox(openvdb::Coord(0), openvdb::Coord(255)), 0.75f, true);
  sparse->tree().fill(openvdb::CoordBBox(openvdb::Coord(512), openvdb::Coord(639)), 1.5f, false);
  sparse->tree().setValueOn(openvdb::Coord(300, 1, 2), 2.f);
  sparse->tree().setValueOn(openvdb::Coord(-17, 5, 900), -3.f);
  sparse->setName("sparse");
  // 4. Empty grid with a non-zero background.
  auto empty = openvdb::FloatGrid::create(0.125f);
  empty->setName("empty");
  // 5. Inactive voxels holding non-background values, -0.0 and a denormal.
  auto inactive = openvdb::FloatGrid::create(0.f);
  {
    auto accessor = inactive->getAccessor();
    for (int i = 0; i < 64; ++i)
      accessor.setValueOn(openvdb::Coord(i, i / 2, 3), float(i));
    for (int i = 0; i < 64; ++i)
      accessor.setValueOff(openvdb::Coord(i, 1 + i / 2, 3), 3.5f + float(i));
    accessor.setValueOff(openvdb::Coord(5, 9, 4), -0.0f);
    accessor.setValueOff(openvdb::Coord(6, 9, 4), std::numeric_limits<float>::denorm_min());
  }
  inactive->setName("inactive");
  // 6. Non-uniform, rotated, sheared and translated transform.
  auto skewed = fog->deepCopy();
  {
    openvdb::Mat4d m = openvdb::Mat4d::identity();
    m.setToRotation(openvdb::math::Vec3d(0.3, 0.7, 0.2).unit(), 0.6);
    m.preScale(openvdb::Vec3d(0.5, 1.0, 2.0));
    m[1][0] += 0.25; // shear
    m.setTranslation(openvdb::Vec3d(1.5, -2.25, 0.125));
    skewed->setTransform(openvdb::math::Transform::createLinearTransform(m));
    skewed->setName("skewed");
  }
  // 7. Fallbacks: half-float storage and an unsupported value type keep the stream form.
  auto half = openvdb::FloatGrid::create(0.f);
  half->tree().setValueOn(openvdb::Coord(1, 2, 3), 1.0f / 3.0f);
  half->setName("half");
  half->setSaveFloatAsHalf(true);
  auto real = openvdb::DoubleGrid::create(0.0);
  real->tree().setValueOn(openvdb::Coord(4, 5, 6), 1.0 / 3.0);
  real->setName("double");

  auto data = std::make_shared<VolumeData>();
  data->key = "volume_wire";
  data->material = "medium";
  data->revision = 3;
  std::uint64_t revision = 10;
  for (const auto &grid :
       std::initializer_list<openvdb::GridBase::ConstPtr>{fog, velocity, sparse, empty, inactive, skewed, half, real})
    data->fields.push_back({grid->getName(), grid, revision++});
  WorkerUpdate update;
  update.scene.volumes.push_back(data);
  // OpenVDB's stream form writes a fresh archive UUID on every write, so byte
  // identity is checked on a packet holding only raw-form fields.
  auto raw_data = std::make_shared<VolumeData>(*data);
  raw_data->fields.resize(6);
  WorkerUpdate raw_update;
  raw_update.scene.volumes.push_back(raw_data);
  const auto decode_fields = [&](const std::vector<std::byte> &bytes) {
    auto decoded = decode_worker_update(bytes);
    require(decoded.scene.volumes.size() == 1 && decoded.scene.volumes[0]->fields.size() == data->fields.size(),
            "Decoded volume field count differs");
    return decoded.scene.volumes[0];
  };
  const auto check_all = [&](const std::shared_ptr<const VolumeData> &decoded, bool stream, const std::string &label) {
    for (std::size_t i = 0; i < data->fields.size(); ++i) {
      const auto &source = data->fields[i];
      const auto &field = decoded->fields[i];
      require(field.name == source.name && field.revision == source.revision, label + ": field identity differs");
      if (source.grid == half) {
        // Stream form quantizes to half precision on the wire, as before.
        const auto value =
            openvdb::gridConstPtrCast<openvdb::FloatGrid>(field.grid)->tree().getValue(openvdb::Coord(1, 2, 3));
        require(value != 1.0f / 3.0f && std::abs(value - 1.0f / 3.0f) < 1e-3f,
                label + ": half-float grid did not keep the stream form");
        continue;
      }
      // Raw-form fields carry exactly the source metadata; stream-form fields
      // additionally carry OpenVDB's file_* statistics.
      const bool stream_field = stream || source.grid == real;
      volume_wire_compare(source.grid, field.grid, stream_field, label + "/" + source.name);
      ++checks;
    }
  };
  volume_wire_env("CYCLES_HOUDINI_SERIAL_CONVERSION", nullptr);
  volume_wire_env("CYCLES_HOUDINI_VOLUME_WIRE_STREAM", nullptr);
  volume_wire_env("CYCLES_HOUDINI_VOLUME_WIRE_CHUNK_LEAVES", nullptr);
  const auto parallel = encode_worker_update(update);
  check_all(decode_fields(parallel), false, "raw");
  const auto raw_parallel = encode_worker_update(raw_update);
  require(encode_worker_update(raw_update) == raw_parallel, "Repeated parallel volume encodes differ");
  volume_wire_env("CYCLES_HOUDINI_SERIAL_CONVERSION", "1");
  require(encode_worker_update(raw_update) == raw_parallel, "Serial and parallel volume encodes differ");
  check_all(decode_fields(encode_worker_update(update)), false, "raw serial encode and decode");
  volume_wire_env("CYCLES_HOUDINI_SERIAL_CONVERSION", nullptr);
  // Chunk boundaries with a small grid: 100-leaf and 1-leaf chunks.
  std::size_t chunked_bytes = 0;
  for (const char *leaves : {"100", "1"}) {
    volume_wire_env("CYCLES_HOUDINI_VOLUME_WIRE_CHUNK_LEAVES", leaves);
    const auto chunked = encode_worker_update(update);
    const auto chunked_raw = encode_worker_update(raw_update);
    volume_wire_env("CYCLES_HOUDINI_SERIAL_CONVERSION", "1");
    const auto chunked_serial = encode_worker_update(raw_update);
    volume_wire_env("CYCLES_HOUDINI_SERIAL_CONVERSION", nullptr);
    require(chunked_raw == chunked_serial, std::string("Chunked serial/parallel encodes differ: ") + leaves);
    require(chunked_raw != raw_parallel, "Chunk size hook had no effect");
    check_all(decode_fields(chunked), false, std::string("chunks of ") + leaves);
    chunked_bytes = chunked.size();
  }
  volume_wire_env("CYCLES_HOUDINI_VOLUME_WIRE_CHUNK_LEAVES", nullptr);
  // The previous OpenVDB stream form remains decodable and equal.
  volume_wire_env("CYCLES_HOUDINI_VOLUME_WIRE_STREAM", "1");
  const auto stream = encode_worker_update(update);
  volume_wire_env("CYCLES_HOUDINI_VOLUME_WIRE_STREAM", nullptr);
  require(stream != parallel, "Stream form hook had no effect");
  check_all(decode_fields(stream), true, "stream");
  // Corrupt packets are rejected, never partially accepted.
  int rejected = 0;
  for (const std::size_t cut : {std::size_t(1), std::size_t(4096), raw_parallel.size() / 2}) {
    std::vector<std::byte> truncated(raw_parallel.begin(), raw_parallel.end() - std::ptrdiff_t(cut));
    try {
      decode_worker_update(truncated);
    } catch (const std::invalid_argument &) {
      ++rejected;
    }
  }
  require(rejected == 3, "Truncated volume packets were accepted");
  // Opaque streams retain the old tag/u64-length format and can exceed the
  // generic container ceiling. Inject a small ceiling to prove this cheaply.
  auto opaque_data = std::make_shared<VolumeData>();
  opaque_data->key = "opaque_fixture";
  opaque_data->material = "medium";
  opaque_data->fields = {{"opaque_double", real, 1}, {"opaque_half", half, 2}};
  WorkerUpdate opaque_update;
  opaque_update.scene.volumes = {opaque_data};
  const auto opaque = encode_worker_update(opaque_update);
  volume_wire_env("CYCLES_HOUDINI_WIRE_TEST_CONTAINER_LIMIT", "1024");
  const auto opaque_decoded = decode_worker_update(opaque);
  bool generic_refused = false;
  try {
    decode_worker_update(raw_parallel);
  } catch (const std::invalid_argument &) {
    generic_refused = true;
  }
  volume_wire_env("CYCLES_HOUDINI_WIRE_TEST_CONTAINER_LIMIT", nullptr);
  require(generic_refused, "Opaque support accidentally relaxed ordinary container bounds");
  require(opaque_decoded.scene.volumes.size() == 1 && opaque_decoded.scene.volumes[0]->fields.size() == 2,
          "Opaque fields above the generic container ceiling failed to decode");
  volume_wire_compare(real, opaque_decoded.scene.volumes[0]->fields[0].grid, true, "opaque double");
  require(openvdb::gridConstPtrCast<openvdb::FloatGrid>(opaque_decoded.scene.volumes[0]->fields[1].grid)
                  ->tree()
                  .getValue(openvdb::Coord(1, 2, 3)) != 1.0f / 3.0f,
          "Opaque half-float fallback changed quantization");
  const std::string marker = "opaque_double";
  const auto found = std::search(opaque.begin(), opaque.end(), reinterpret_cast<const std::byte *>(marker.data()),
                                 reinterpret_cast<const std::byte *>(marker.data() + marker.size()));
  require(found != opaque.end(), "Opaque fixture field marker missing");
  const auto tag_offset = std::size_t(found - opaque.begin()) + marker.size() + sizeof(std::uint64_t);
  require(opaque.at(tag_offset) == std::byte(0), "Opaque fallback changed its compatible stream tag");
  const auto length_offset = tag_offset + 1;
  const auto body_offset = length_offset + sizeof(std::uint64_t);
  std::uint64_t blob_bytes{};
  std::memcpy(&blob_bytes, opaque.data() + length_offset, sizeof(blob_bytes));
  require(blob_bytes > 1024 && blob_bytes <= opaque.size() - body_offset,
          "Opaque fixture did not exceed the injected generic ceiling");
  int opaque_rejected = 0;
  const auto reject_opaque = [&](const std::vector<std::byte> &bytes) {
    try {
      decode_worker_update(bytes);
    } catch (const std::invalid_argument &) {
      ++opaque_rejected;
    }
  };
  for (const auto end : {length_offset + 7, body_offset + 1, body_offset + std::size_t(blob_bytes) - 1})
    reject_opaque(std::vector<std::byte>(opaque.begin(), opaque.begin() + std::ptrdiff_t(end)));
  for (const auto size : {std::uint64_t(opaque.size()), std::numeric_limits<std::uint64_t>::max()}) {
    auto malformed = opaque;
    std::memcpy(malformed.data() + length_offset, &size, sizeof(size));
    reject_opaque(malformed);
  }
  auto wrong_version = opaque;
  const std::uint32_t unsupported_version = worker_protocol_version + 1;
  std::memcpy(wrong_version.data() + sizeof(std::uint32_t), &unsupported_version, sizeof(unsupported_version));
  reject_opaque(wrong_version);
  require(opaque_rejected == 6, "Opaque length/truncation/version preflight accepted a malformed packet");
  volume_wire_env("CYCLES_HOUDINI_VOLUME_WIRE_TEST_LIMIT", "1024");
  std::string opaque_refusal;
  try {
    encode_worker_update(opaque_update);
  } catch (const std::invalid_argument &error) {
    opaque_refusal = error.what();
  }
  volume_wire_env("CYCLES_HOUDINI_VOLUME_WIRE_TEST_LIMIT", nullptr);
  require(opaque_refusal.find("Volume field 'opaque_double' is too large") == 0,
          "Opaque writer did not preflight the packet budget");
  // The size refusal: lowering the limit (test hook) makes the 128^3 fog field
  // too large; the encoder refuses before copying voxels, naming the field.
  std::string refusal;
  volume_wire_env("CYCLES_HOUDINI_VOLUME_WIRE_TEST_LIMIT", "1000000");
  try {
    encode_worker_update(raw_update);
  } catch (const std::invalid_argument &error) {
    refusal = error.what();
  }
  volume_wire_env("CYCLES_HOUDINI_VOLUME_WIRE_TEST_LIMIT", "100000000");
  const bool fits = encode_worker_update(raw_update) == raw_parallel;
  volume_wire_env("CYCLES_HOUDINI_VOLUME_WIRE_TEST_LIMIT", nullptr);
  require(refusal.find("Volume field 'density' is too large to send to the renderer") == 0 &&
              refusal.find("at most 1 MB") != std::string::npos,
          "Oversized volume refusal missing or unclear: " + refusal);
  require(fits, "A lowered limit above the packet size changed the packet");
  std::cout << "{\"result\":\"passed\",\"checks\":" << checks << ",\"rejected\":" << rejected
            << ",\"opaque_rejected\":" << opaque_rejected << ",\"opaque_bytes\":" << opaque.size()
            << ",\"raw_bytes\":" << parallel.size() << ",\"stream_bytes\":" << stream.size()
            << ",\"chunked_bytes\":" << chunked_bytes << ",\"refusal\":\"" << refusal << "\""
            << ",\"worker_protocol\":" << worker_protocol_version << "}\n";
}
} // namespace cycles_houdini::harness
