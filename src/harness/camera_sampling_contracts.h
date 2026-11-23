// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "motion_fixture.h"
#include "cycles_houdini/snapshot.h"
#include "scene/camera.h"
#include "util/math_cdf.h"

namespace cycles_houdini::harness {
inline int camera_uniform_shutter_contracts() {
  int checks = 0;
  const auto require = [&](bool value, const char *message) {
    ++checks;
    motion_require(value, message);
  };
  ccl::array<float> weights;
  ccl::vector<float> table, previous;
  for (int count : {0, 1, 3, 256}) {
    weights.resize(count);
    std::fill(weights.begin(), weights.end(), count == 1 ? .75f : .4f);
    ccl::camera_shutter_curve_table(weights, table);
    require(table.size() == SHUTTER_TABLE_SIZE, "Camera exposure table has the wrong size");
    for (int i = 0; i < SHUTTER_TABLE_SIZE; ++i)
      require(float_bits_equal(table[i], float(i) / float(SHUTTER_TABLE_SIZE - 1)),
              "Uniform camera exposure contracts/shifts the shutter interval");
    require(table.front() == 0 && table.back() == 1, "Uniform shutter lost exact open/close endpoints");
  }
  for (int shape = 0; shape < 4; ++shape) {
    if (shape == 0)
      weights = ccl::vector<float>{0.f, 1.f};
    if (shape == 1)
      weights = ccl::vector<float>{1.f, 0.f};
    if (shape == 2)
      weights = ccl::vector<float>{0.f, 1.f, 0.f};
    if (shape == 3)
      weights = ccl::vector<float>{.4f, .4f, std::nextafter(.4f, 1.f)};
    ccl::camera_shutter_curve_table(weights, table);
    ccl::util_cdf_inverted(
        SHUTTER_TABLE_SIZE, 0.f, 1.f, [&](float x) { return ccl::camera_shutter_curve_eval(x, weights); }, false,
        previous);
    require(table.size() == previous.size(), "Authored nonuniform exposure table changed size");
    for (std::size_t i = 0; i < table.size(); ++i)
      require(float_bits_equal(table[i], previous[i]),
              "Camera uniform correction changed authored nonuniform exposure");
  }
  std::cout << "{\"check\":\"Camera uniform shutter table uses complete "
               "interval\",\"uniform_tables\":4,\"nonuniform_parity_tables\":4,\"entries_per_table\":"
            << SHUTTER_TABLE_SIZE << ",\"checks\":" << checks << "}" << std::endl;
  return checks;
}
inline void camera_sampling_contracts(const std::string &device) {
  int checks = camera_uniform_shutter_contracts();
  const auto require = [&](bool value, const char *text) {
    motion_require(value, text);
    ++checks;
  };
  ccl::array<float> weights;
  require(ccl::camera_shutter_curve_eval(0, weights) == 1, "Empty shutter curve is not uniform");
  weights.resize(3);
  weights[0] = .2f;
  weights[1] = .8f;
  weights[2] = .4f;
  for (const auto [x, y] :
       {std::pair{-1.f, .2f}, {0.f, .2f}, {.25f, .5f}, {.5f, .8f}, {.75f, .6f}, {1.f, .4f}, {2.f, .4f}})
    require(std::abs(ccl::camera_shutter_curve_eval(x, weights) - y) < 1e-6,
            "Shutter curve interpolation or endpoint is incorrect");
  weights.resize(1);
  weights[0] = .75f;
  require(ccl::camera_shutter_curve_eval(.5f, weights) == .75f, "Single engine curve sample is unsafe");
  WorkerUpdate packet;
  CameraData camera;
  camera.sampling = {true, .25f, {0, 1, 0}};
  packet.scene.camera = camera;
  require(decode_worker_update(encode_worker_update(packet)).scene.camera == packet.scene.camera,
          "Camera sampling did not retain wire ownership");
  SessionConfig config;
  config.device_id = device;
  config.width = 8;
  config.height = 8;
  config.samples = 1;
  config.interactive = false;
  RenderSession session(config);
  SceneUpdate good;
  good.camera = camera;
  motion_wait(session, session.apply(good));
  const auto generation = session.status().generation;
  for (int kind = 0; kind < 6; ++kind) {
    SceneUpdate bad;
    bad.camera = camera;
    auto &data = bad.camera->sampling;
    if (kind == 0)
      data.rolling_duration = std::numeric_limits<float>::quiet_NaN();
    if (kind == 1)
      data.rolling_duration = -.1f;
    if (kind == 2)
      data.shutter_curve = {0, 0};
    if (kind == 3)
      data.shutter_curve = {0, -1, 1};
    if (kind == 4)
      data.shutter_curve.resize(257, 1);
    if (kind == 5)
      data.shutter_curve = {0, std::numeric_limits<float>::infinity()};
    bool rejected = false;
    try {
      session.apply(bad);
    } catch (const std::invalid_argument &) {
      rejected = true;
    }
    require(rejected && session.status().generation == generation && session.status().state != SessionState::Failed,
            "Malformed camera sampling modified the accepted scene");
  }
  session.close();
  require(session.status().owned_geometry_bytes == session.status().device_allocation_bytes &&
              session.status().owned_geometry_bytes == 0,
          "Sampling contracts retained renderer resources");
  std::cout << "{\"result\":\"passed\",\"checks\":" << checks << ",\"worker_protocol\":" << worker_protocol_version
            << "}\n";
}
} // namespace cycles_houdini::harness
