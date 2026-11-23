// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "../core/render_devices.h"
#include "scene/mesh.h"
#include "scene/object.h"
#include "scene/scene.h"
#include "scene/stats.h"
#include "util/boundbox_overlap.h"
#include "util/progress.h"
#include "util/profiling.h"
#include <array>
#include <bit>
#include <cmath>
#include <iostream>
#include <limits>
#include <random>
#include <stdexcept>
#include <vector>

namespace cycles_houdini::harness {
inline void object_volume_bounds_contracts(const std::string &selection) {
  std::size_t checks = 0, queries = 0, flag_passes = 0;
  const auto require = [&](bool value, const char *message) {
    ++checks;
    if (!value)
      throw std::runtime_error(message);
  };
  using Box = ccl::BoundBox;
  using Index = ccl::BoundBoxOverlapIndex;
  const auto point = [](float x, float y, float z) { return ccl::make_float3(x, y, z); };
  const auto box = [&](float lo, float hi) { return Box(point(lo, lo, lo), point(hi, hi, hi)); };
  const auto compare = [&](const std::vector<Box> &volumes, const std::vector<Box> &tests) {
    ccl::vector<Index::Entry> entries;
    for (const auto &volume : volumes)
      entries.push_back({&volume, &volume});
    const Index index(entries);
    const auto query = [&](Box bounds, const void *excluded) {
      bool expected = false;
      for (const auto &volume : volumes)
        if (&volume != excluded && bounds.intersects(volume)) {
          expected = true;
          break;
        }
      require(index.intersects(bounds, excluded) == expected, "Volume broad phase differs from linear oracle");
      ++queries;
    };
    for (const auto &test : tests)
      query(test, nullptr);
    for (const auto &volume : volumes)
      query(volume, &volume);
  };
  const float inf = std::numeric_limits<float>::infinity();
  const float nan = std::numeric_limits<float>::quiet_NaN();
  const float huge = std::numeric_limits<float>::max();
  const float tiny = std::numeric_limits<float>::denorm_min();
  std::vector<Box> special{box(0, 1),        box(1, 2),           box(-1, 0),
                           box(3, 3),        Box(Box::empty),     box(4, -4),
                           box(-huge, huge), box(huge / 2, huge), box(-huge, -huge / 2),
                           box(-inf, inf),   box(inf, inf),       box(-inf, -inf),
                           box(nan, 1),      box(0, nan),         box(-tiny, tiny),
                           box(-0.f, 0.f)};
  compare({}, special);
  for (const auto &value : special)
    compare({value}, special);
  compare(special, special);
  compare({box(0, 1), box(0, 1)}, {box(.5f, .5f)}); // Coincident distinct identities.
  const Box face_touch(point(1, .25f, .25f), point(2, .75f, .75f));
  const Box edge_touch(point(1, 1, .25f), point(2, 2, .75f));
  const Box corner_touch(point(1, 1, 1), point(2, 2, 2));
  compare({box(0, 1)}, {face_touch, edge_touch, corner_touch});
  std::array<float, 6> adjacent;
  adjacent[0] = 1;
  for (std::size_t i = 1; i < adjacent.size(); ++i)
    adjacent[i] = std::nextafter(adjacent[i - 1], inf);
  Box rounded_left = box(adjacent[0], adjacent[3]);
  const Box rounded_right = box(adjacent[4], adjacent[5]);
  require(rounded_left.max.x < rounded_right.min.x && rounded_left.intersects(rounded_right),
          "Rounding-touch fixture no longer distinguishes endpoint overlap");
  std::vector<Box> rounded_volumes{rounded_left};
  for (int i = 0; i < 32; ++i)
    rounded_volumes.push_back(box(float(i) * 10 + 20, float(i) * 10 + 21));
  compare(rounded_volumes, {rounded_right, box(0, 1000), box(-1000, -1)});

  std::mt19937 random(0x71ac830u);
  std::uniform_real_distribution<float> position(-1000, 1000), radius(0, 4);
  std::vector<Box> volumes, tests;
  for (int i = 0; i < 4096; ++i) {
    const auto center = point(position(random), position(random), position(random));
    const auto size = point(radius(random), radius(random), radius(random));
    volumes.emplace_back(center - size, center + size);
    tests.emplace_back(center, center);
    tests.emplace_back(center + size, center + size); // Exact stored corner.
    tests.emplace_back(center + size + point(.01f, .01f, .01f), center + size + point(1, 1, 1));
  }
  compare(volumes, tests);
  // Exhaust nearby float endpoints across exponent ranges. The rounded center/size
  // test can accept an endpoint gap, so a geometric union-box oracle is insufficient.
  volumes.clear();
  tests.clear();
  for (int exponent = -145; exponent <= 120; exponent += 5) {
    const float origin = std::ldexp(1.f, exponent);
    float edge = origin;
    for (int step = 0; step < 12; ++step)
      edge = std::nextafter(edge, inf);
    volumes.push_back(box(origin, edge));
    for (int delta = 0; delta < 5; ++delta) {
      tests.push_back(box(edge, edge));
      edge = std::nextafter(edge, inf);
    }
  }
  compare(volumes, tests);
  // Arbitrary float bit patterns cover invalid intervals, signed zero and nonfinite
  // values independently of the index's finite-box construction assumptions.
  volumes.clear();
  tests.clear();
  const auto random_point = [&]() {
    return point(std::bit_cast<float>(std::uint32_t(random())), std::bit_cast<float>(std::uint32_t(random())),
                 std::bit_cast<float>(std::uint32_t(random())));
  };
  for (int i = 0; i < 1024; ++i) {
    volumes.emplace_back(random_point(), random_point());
    tests.emplace_back(random_point(), random_point());
  }
  compare(volumes, tests);

  // Exercise the actual production flag routine across valid, pre-bounds and
  // separated updates. Preserve the legacy packed bit's OR-only semantics while
  // the host intersects_volume boolean resets on every valid-bounds pass.
  SessionConfig config;
  config.device_id = selection;
  config.threads = 2;
  std::vector<std::string> ids;
  const auto info = resolve_render_devices(config, ids);
  ccl::Stats stats;
  ccl::Profiler profiler;
  auto device = ccl::Device::create(info, stats, profiler, true);
  require(device && !device->have_error(), "Volume flag contract device creation failed");
  // MEM_GLOBAL copies publish their pointers through CUDA's utility module.
  // This direct device fixture bypasses Scene::device_update's normal loader.
  require(device->load_kernels(0) && !device->have_error(), "Volume flag contract utility kernel load failed");
  {
    ccl::Scene scene(ccl::SceneParams{}, device.get());
    auto *volume = scene.create_node<ccl::Mesh>();
    auto *surface = scene.create_node<ccl::Mesh>();
    volume->has_volume = true;
    surface->has_volume = false;
    for (int i = 0; i < 24; ++i) {
      auto *object = scene.create_node<ccl::Object>();
      object->set_geometry(i % 3 ? volume : surface);
      object->index = i;
      object->bounds = i < int(special.size()) ? special[i] : box(float(i), float(i) + .25f);
      object->intersects_volume = i % 2;
    }
    auto *flags = scene.dscene.object_flag.alloc(scene.objects.size());
    std::vector<bool> expected_intersection(scene.objects.size());
    std::vector<bool> expected_bit(scene.objects.size());
    for (std::size_t i = 0; i < scene.objects.size(); ++i) {
      flags[i] = i % 2 ? ccl::SD_OBJECT_INTERSECTS_VOLUME : 0;
      expected_intersection[i] = scene.objects[i]->intersects_volume;
      expected_bit[i] = bool(flags[i] & ccl::SD_OBJECT_INTERSECTS_VOLUME);
    }
    ccl::Progress progress;
    const auto pass = [&](bool valid) {
      bool has_volume = false;
      for (const auto *object : scene.objects)
        has_volume |= object->get_geometry()->has_volume;
      for (std::size_t i = 0; i < scene.objects.size(); ++i) {
        auto *object = scene.objects[i];
        if (valid) {
          expected_intersection[i] = false;
          for (const auto *other : scene.objects)
            if (other != object && other->get_geometry()->has_volume && object->bounds.intersects(other->bounds)) {
              expected_intersection[i] = expected_bit[i] = true;
              break;
            }
        } else if (has_volume)
          expected_bit[i] = true;
      }
      scene.object_manager->tag_update(&scene, ccl::ObjectManager::UPDATE_ALL);
      scene.object_manager->device_update_flags(device.get(), &scene.dscene, &scene, progress, valid);
      for (std::size_t i = 0; i < scene.objects.size(); ++i) {
        require(scene.objects[i]->intersects_volume == expected_intersection[i],
                "Volume host intersection state differs from legacy update passes");
        require(bool(flags[i] & ccl::SD_OBJECT_INTERSECTS_VOLUME) == expected_bit[i],
                "Packed volume intersection flag differs from legacy update passes");
      }
      ++flag_passes;
    };
    pass(false);
    pass(true);
    for (std::size_t i = 0; i < scene.objects.size(); ++i)
      scene.objects[i]->bounds = box(float(i) * 10, float(i) * 10 + 1);
    pass(true);
    pass(false);
    volume->has_volume = false;
    pass(false);
    pass(true);
    require(!device->have_error(), "Volume flag contract device error");
  }
  std::cout << "{\"result\":\"passed\",\"checks\":" << checks << ",\"queries\":" << queries
            << ",\"flag_passes\":" << flag_passes << "}\n";
}
} // namespace cycles_houdini::harness
