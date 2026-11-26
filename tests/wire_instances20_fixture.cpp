// SPDX-License-Identifier: Apache-2.0
// Included after the complete production snapshot_wire.cpp by the test driver.
#include <iostream>
#include <fstream>
#include <chrono>
#include <thread>
#include <numeric>
#include <tbb/global_control.h>
using namespace cycles_houdini;
namespace {
int checks{};
void require(bool ok, const char *message) {
  ++checks;
  if (!ok)
    throw std::runtime_error(message);
}
template <class F> void invalid(F &&f) {
  bool caught{};
  try {
    f();
  } catch (const std::invalid_argument &) {
    caught = true;
  }
  require(caught, "malformed input was accepted");
}
void env(const char *name, bool on) { _putenv_s(name, on ? "1" : "0"); }
bool matrix(const Matrix &a, const Matrix &b) { return std::memcmp(a.data(), b.data(), sizeof(Matrix)) == 0; }
void pose_equal(const ObjectPoseData &a, const ObjectPoseData &b) {
  require(a.key == b.key, "pose key");
  require(matrix(a.transform, b.transform), "pose transform bits");
  require(a.motion.size() == b.motion.size(), "pose motion count");
  for (std::size_t i = 0; i < a.motion.size(); ++i)
    require(matrix(a.motion[i], b.motion[i]), "pose motion bits");
  require(bool(a.nominal_transform) == bool(b.nominal_transform), "nominal presence");
  if (a.nominal_transform)
    require(matrix(*a.nominal_transform, *b.nominal_transform), "nominal bits");
}
void attributes_equal(const ObjectAttributeData &a, const ObjectAttributeData &b) {
  require(a.key == b.key && a.attributes.size() == b.attributes.size(), "attribute key/count");
  for (std::size_t i = 0; i < a.attributes.size(); ++i) {
    const auto &x = a.attributes[i], &y = b.attributes[i];
    require(x.name == y.name && x.components == y.components, "uniform name/components");
    require(std::memcmp(&x.value, &y.value, sizeof(Float3)) == 0, "uniform float bits");
    require(x.motion_complete == y.motion_complete && x.motion_values.size() == y.motion_values.size(),
            "uniform motion metadata");
    require(x.motion_values.empty() || std::memcmp(x.motion_values.data(), y.motion_values.data(),
                                                   x.motion_values.size() * sizeof(Float3)) == 0,
            "uniform motion bits");
  }
  require(bool(a.pose) == bool(b.pose), "attribute pose presence");
  if (a.pose)
    pose_equal(*a.pose, *b.pose);
}
WorkerUpdate fixture(std::size_t count) {
  WorkerUpdate u;
  // These maps are deliberately inserted in reverse order. Their generic
  // traversal must retain the same complete packet order around instance data.
  u.color.context.emplace("z-last", "z");
  u.color.context.emplace("a-first", "a");
  auto points = std::make_shared<PointData>();
  points->key = "preceding-external";
  points->positions = std::vector<Float3>(2048, Float3{1, 2, 3});
  u.scene.points.push_back(points);
  auto image = std::make_shared<TextureImageData>();
  image->source = "following-external";
  image->pixels = std::vector<float>(8192, 0.5f);
  u.image_upserts.push_back(image);
  u.root_poses.token = 0xfedcba9876543210ull;
  u.root_poses.reset_origins = true;
  PoseOriginRecord origin;
  origin.key = "instance/root-origin";
  origin.root = "/obj/root";
  origin.local = identity_matrix;
  origin.local[3] = -17.25f;
  origin.motion = {origin.local, identity_matrix};
  origin.nominal = true;
  u.root_poses.origins.push_back(origin);
  RootPoseRecord root;
  root.key = origin.root;
  root.centre = origin.local;
  root.nominal = identity_matrix;
  root.motion = {identity_matrix, origin.local};
  u.root_poses.roots.push_back(root);
  for (std::size_t i = 0; i < count; ++i) {
    ObjectPoseData p;
    p.key = i % 23 == 0 ? "" : (i % 17 ? "instance/" + std::to_string(i) : std::string("embedded\0key", 12));
    p.transform[0] = std::bit_cast<float>(std::uint32_t(0x80000000));
    p.transform[1] = std::bit_cast<float>(std::uint32_t(0x7fc12345));
    p.transform[3] = float(i);
    if (i % 4)
      p.nominal_transform = p.transform;
    if (i % 4 == 2)
      (*p.nominal_transform)[2] = -0.0f;
    const auto motion = i % 11 == 0 ? 129 : (i % 3 == 0 ? 3 : 0);
    for (int j = 0; j < motion; ++j) {
      auto m = p.transform;
      m[7] = float(j);
      p.motion.push_back(m);
    }
    u.scene.object_poses.push_back(p);
    ObjectAttributeData a;
    a.key = p.key;
    if (i % 3)
      a.pose = p;
    for (std::size_t j = 0; j < i % 4; ++j) {
      UniformAttribute v;
      v.name = j == 0 ? "z_field" : "a_field";
      v.components = int(j + 1);
      v.value = {float(i), -0.0f, std::bit_cast<float>(std::uint32_t(0x7fa12345))};
      v.motion_complete = i % 2 == 0;
      std::vector<Float3> values;
      for (int k = 0; k < motion; ++k)
        values.push_back({float(k), float(j), float(i)});
      v.motion_values = std::move(values);
      a.attributes.push_back(std::move(v));
    }
    u.scene.object_attributes.push_back(std::move(a));
  }
  return u;
}
std::vector<std::byte> encode_mode(const WorkerUpdate &u, bool reference, bool gathered, bool serial) {
  env("CYCLES_HOUDINI_WIRE_INSTANCE_SERIAL", reference);
  env("CYCLES_HOUDINI_SERIAL_CONVERSION", serial);
  if (!gathered)
    return encode_worker_update(u);
  auto packet = encode_worker_update_gathered(std::make_shared<WorkerUpdate>(u));
  std::vector<std::byte> data(std::size_t(packet.size()));
  packet.write(data.data());
  return data;
}
void run(std::size_t count) {
  const auto u = fixture(count);
  const auto reference = encode_mode(u, true, false, true);
  for (bool gathered : {false, true})
    for (bool serial : {false, true}) {
      const auto fast = encode_mode(u, false, gathered, serial);
      require(fast == reference, "complete protocol92 packet bytes differ");
      const auto d = decode_worker_update(fast);
      require(d.scene.object_poses.size() == u.scene.object_poses.size(), "decoded pose count");
      require(d.scene.object_attributes.size() == u.scene.object_attributes.size(), "decoded attribute count");
      require(d.color.context == u.color.context, "surrounding sorted map contents");
      require(d.root_poses.token == u.root_poses.token && d.root_poses.reset_origins, "root pose token/reset");
      require(d.root_poses.origins.size() == 1 && d.root_poses.roots.size() == 1, "root pose record counts");
      const auto &x = d.root_poses.origins[0], &y = u.root_poses.origins[0];
      require(x.key == y.key && x.root == y.root && x.nominal == y.nominal && matrix(x.local, y.local),
              "root origin fields");
      require(x.motion.size() == 2 && matrix(x.motion[0], y.motion[0]) && matrix(x.motion[1], y.motion[1]),
              "root origin motion");
      const auto &r = d.root_poses.roots[0], &s = u.root_poses.roots[0];
      require(r.key == s.key && matrix(r.centre, s.centre) && matrix(r.nominal, s.nominal), "root record fields");
      require(r.motion.size() == 2 && matrix(r.motion[0], s.motion[0]) && matrix(r.motion[1], s.motion[1]),
              "root record motion");
      for (std::size_t i = 0; i < count; ++i) {
        pose_equal(u.scene.object_poses[i], d.scene.object_poses[i]);
        attributes_equal(u.scene.object_attributes[i], d.scene.object_attributes[i]);
      }
      require(encode_mode(d, true, false, true) == reference, "decoded packet roundtrip differs");
    }
}
} // namespace
int main(int argc, char **argv) {
  try {
    static_assert(worker_protocol_version == 93, "review fixture before advancing protocol");
    if (argc >= 2) {
      require(argc == 2 || argc == 4, "replay expects packet and optional expected pose/attribute counts");
      std::ifstream input(argv[1], std::ios::binary | std::ios::ate);
      if (!input)
        throw std::runtime_error("cannot open live wire fixture");
      const auto size = input.tellg();
      input.seekg(0);
      std::vector<std::byte> packet(static_cast<std::size_t>(size));
      input.read(reinterpret_cast<char *>(packet.data()), size);
      if (!input)
        throw std::runtime_error("cannot read live wire fixture");
      auto update = std::make_shared<WorkerUpdate>(decode_worker_update(packet));
      if (argc == 4) {
        require(update->scene.object_poses.size() == std::stoull(argv[2]), "actual artist pose count");
        require(update->scene.object_attributes.size() == std::stoull(argv[3]), "actual artist attribute count");
      }
      using Clock = std::chrono::steady_clock;
      std::cout << "{\"result\":\"passed\",\"scope\":\"actual live decoded WorkerUpdate; full gathered wire encoding, "
                   "snapshot_difference excluded\",\"payload_bytes\":"
                << packet.size() << ",\"poses\":" << update->scene.object_poses.size()
                << ",\"attributes\":" << update->scene.object_attributes.size() << ",\"rows\":[";
      bool first = true;
      for (int pair = -1; pair < 6; ++pair)
        for (int turn = 0; turn < 2; ++turn) {
          const bool reference = (pair % 2 == 0) == (turn == 0);
          env("CYCLES_HOUDINI_WIRE_INSTANCE_SERIAL", reference);
          env("CYCLES_HOUDINI_SERIAL_CONVERSION", false);
          const auto start = Clock::now();
          auto gathered = encode_worker_update_gathered(update);
          const auto staged = Clock::now();
          std::vector<std::byte> target(static_cast<std::size_t>(gathered.size()));
          const auto copy_start = Clock::now();
          gathered.write(target.data());
          const auto end = Clock::now();
          require(target == packet, "live replay complete wire bytes");
          if (pair >= 0) {
            if (!first)
              std::cout << ',';
            first = false;
            std::cout << "{\"pair\":" << pair << ",\"mode\":\"" << (reference ? "reference" : "candidate")
                      << "\",\"encode_seconds\":" << std::chrono::duration<double>(staged - start).count()
                      << ",\"write_seconds\":" << std::chrono::duration<double>(end - copy_start).count()
                      << ",\"materialize_inclusive_seconds\":" << std::chrono::duration<double>(end - staged).count()
                      << ",\"total_encode_materialize_seconds\":" << std::chrono::duration<double>(end - start).count()
                      << "}";
          }
        }
      std::cout << "]}\n";
      return 0;
    }
    const auto default_cores = tbb::global_control::active_value(tbb::global_control::max_allowed_parallelism);
    for (std::size_t cores : std::array<std::size_t, 4>{1, 2, 8, default_cores}) {
      tbb::global_control control(tbb::global_control::max_allowed_parallelism, cores);
      for (std::size_t n : {0, 1, 2047, 2048, 2049, 4097})
        run(n);
    }
    WorkerReply reply;
    auto &mem = reply.diagnostics.device_update_memory;
    mem.available = true;
    mem.update_serial = 0x100000001ull;
    mem.generation = 73;
    mem.outcome = "cancelled";
    mem.scope = "scene_device_update";
    DeviceUpdateMemoryDevice device;
    device.id = "OPTIX_test";
    device.physical_id = "0000:01:00";
    device.backend = "OPTIX";
    device.description = "fixture only";
    device.start_bytes = 1;
    device.peak_bytes = 2;
    device.end_bytes = 3;
    device.max_build_temp_bytes = 4;
    device.max_build_temp_allocation_bytes = 5;
    device.build_count = 6;
    device.reused_temp_build_count = 7;
    device.local_temp_build_count = 8;
    device.kept_temp_bytes_before_attributes = 9;
    mem.devices.push_back(device);
    const auto reply_bytes = encode_worker_reply(reply);
    const auto decoded_reply = decode_worker_reply(reply_bytes, {});
    const auto &m = decoded_reply.diagnostics.device_update_memory;
    require(m.available && m.update_serial == mem.update_serial && m.generation == 73 && m.outcome == mem.outcome &&
                m.scope == mem.scope,
            "memory diagnostic metadata");
    require(m.devices.size() == 1, "memory diagnostic device count");
    const auto &v = m.devices[0];
    require(v.id == device.id && v.physical_id == device.physical_id && v.backend == device.backend &&
                v.description == device.description,
            "memory diagnostic device identity");
    require(v.start_bytes == 1 && v.peak_bytes == 2 && v.end_bytes == 3 && v.max_build_temp_bytes == 4 &&
                v.max_build_temp_allocation_bytes == 5 && v.build_count == 6 && v.reused_temp_build_count == 7 &&
                v.local_temp_build_count == 8 && v.kept_temp_bytes_before_attributes == 9,
            "memory diagnostic numeric fields");
    require(encode_worker_reply(decoded_reply) == reply_bytes, "complete memory diagnostic reply roundtrip");
    for (std::size_t n : {341, 342, 343}) {
      auto u = fixture(2048);
      u.scene.object_poses[17].motion.resize(n, identity_matrix);
      UniformAttribute attr;
      attr.name = "large-motion";
      attr.motion_values = std::vector<Float3>(n * 4, Float3{1, 2, 3});
      u.scene.object_attributes[17].attributes.push_back(attr);
      const auto reference = encode_mode(u, true, true, true);
      require(encode_mode(u, false, true, false) == reference, "16KiB borrowed-array boundary");
      auto owner = std::make_shared<WorkerUpdate>(u);
      env("CYCLES_HOUDINI_WIRE_INSTANCE_SERIAL", false);
      auto gathered = encode_worker_update_gathered(owner);
      owner.reset();
      std::vector<std::byte> data(std::size_t(gathered.size()));
      gathered.write(data.data());
      require(data == reference, "gathered retained source lifetime");
      const auto decoded = decode_worker_update(data);
      pose_equal(u.scene.object_poses[17], decoded.scene.object_poses[17]);
      attributes_equal(u.scene.object_attributes[17], decoded.scene.object_attributes[17]);
    }
    // Actual production private writer overflow guards, without huge allocation.
    InstanceWriter count{nullptr, 0, 16};
    invalid([&] { count.raw(nullptr, 17); });
    require(count.position == 0, "failed count mutated cursor");
    InstanceWriter near_limit{nullptr, 15, 16};
    invalid([&] { near_limit.raw(nullptr, std::numeric_limits<std::size_t>::max()); });
    require(near_limit.position == 15, "overflow mutated cursor");
    InstanceWriter short_slot{nullptr, 0, 1};
    invalid([&] { short_slot(std::string("abc")); });
    const auto bounded = fixture(2048);
    env("CYCLES_HOUDINI_WIRE_INSTANCE_SERIAL", false);
    Gather exhausted;
    exhausted.size = maximum_packet - 8;
    Writer limited;
    limited.gather = &exhausted;
    invalid([&] { limited(bounded.scene.object_poses); });
    require(exhausted.staging.size() == 8, "count refusal allocated record bytes");
    Gather prefix;
    prefix.size = maximum_packet - 8 - 65 * 2048;
    Writer prefix_writer;
    prefix_writer.gather = &prefix;
    invalid([&] { prefix_writer(bounded.scene.object_poses); });
    require(prefix.staging.size() == 8, "prefix refusal allocated record bytes");
    invalid([&] { prefix_writer.stage(std::numeric_limits<std::size_t>::max()); });
    const auto valid = encode_mode(fixture(2048), false, false, false);
    auto broken = valid;
    broken.resize(11);
    invalid([&] { decode_worker_update(broken); });
    broken = valid;
    broken.push_back(std::byte{0});
    invalid([&] { decode_worker_update(broken); });
    broken = valid;
    broken[4] = std::byte{0};
    invalid([&] { decode_worker_update(broken); });
    invalid([&] { decode_worker_update(valid, nullptr, 1); });
    invalid([&] { decode_worker_update(valid, nullptr, maximum_packet + 1); });
    // Direct full production Reader checks of corrupt string/vector lengths,
    // duplicate map keys, booleans, and invalid optional nominal tags.
    Writer strings;
    strings(std::uint64_t(-1));
    Reader rs(strings.bytes);
    std::string text;
    invalid([&] { rs(text); });
    Writer vectors;
    vectors(std::uint64_t(-1));
    Reader rv(vectors.bytes);
    std::vector<Matrix> motion;
    invalid([&] { rv(motion); });
    Writer maps;
    maps(std::uint64_t(2), std::string("duplicate"), std::string("one"), std::string("duplicate"), std::string("two"));
    Reader rm(maps.bytes);
    std::map<std::string, std::string> map;
    invalid([&] { rm(map); });
    Writer flag;
    flag(std::uint8_t(2));
    Reader rf(flag.bytes);
    bool boolean{};
    invalid([&] { rf(boolean); });
    Writer nominal;
    nominal(std::string("key"), identity_matrix, std::vector<Matrix>{}, std::uint8_t(3));
    Reader rn(nominal.bytes);
    ObjectPoseData p;
    invalid([&] { rn(p); });
    std::cout << "{\"result\":\"passed\",\"checks\":" << checks << ",\"protocol\":" << worker_protocol_version
              << ",\"default_tbb_max\":" << default_cores
              << ",\"hardware_threads\":" << std::thread::hardware_concurrency() << "}\n";
    return 0;
  } catch (const std::exception &e) {
    std::cerr << e.what() << '\n';
    return 1;
  }
}
