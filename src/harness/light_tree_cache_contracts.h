// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "scene/light_tree_cache.h"
#include "scene/light_tree_reuse.h"
#include "scene/light_tree_emission.h"
#include "scene/light_tree_build_profile.h"
#include "scene/light_tree.h"
#include <cmath>
#include <limits>
#include <algorithm>
#include <array>
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <vector>

namespace cycles_houdini::harness {
inline void light_tree_cache_contracts() {
  int checks = 0, blocks = 0;
  const auto require = [&](bool value, const char *message) {
    ++checks;
    if (!value)
      throw std::runtime_error(message);
  };
  const auto rejects = [&](auto action) {
    bool rejected = false;
    try {
      action();
    } catch (const std::runtime_error &) {
      rejected = true;
    }
    require(rejected, "Malformed owned cache block was accepted");
  };
  {
    const auto same = [](const ccl::LightTreeMeasure &a, const ccl::LightTreeMeasure &b) {
      const std::array<float, 12> x{{a.bbox.min.x, a.bbox.min.y, a.bbox.min.z, a.bbox.max.x, a.bbox.max.y, a.bbox.max.z,
                                     a.bcone.axis.x, a.bcone.axis.y, a.bcone.axis.z, a.bcone.theta_o, a.bcone.theta_e,
                                     a.energy}},
          y{{b.bbox.min.x, b.bbox.min.y, b.bbox.min.z, b.bbox.max.x, b.bbox.max.y, b.bbox.max.z, b.bcone.axis.x,
             b.bcone.axis.y, b.bcone.axis.z, b.bcone.theta_o, b.bcone.theta_e, b.energy}};
      return std::memcmp(x.data(), y.data(), sizeof(x)) == 0;
    };
    std::size_t duplicates = 0;
    for (unsigned mask = 0; mask < 4096; ++mask) {
      std::array<ccl::LightTreeBucket, 12> buckets;
      for (int index = 0; index < 12; ++index)
        if (mask & (1u << index)) {
          auto &b = buckets[index];
          b.count = index % 4 + 1;
          b.measure.bbox.grow(ccl::make_float3(float(index), -.1f * index, -.25f));
          b.measure.bbox.grow(ccl::make_float3(float(index) + .01f, .3f, .2f));
          b.measure.bcone = ccl::OrientationBounds(index % 2 ? ccl::make_float3(0, 1, 0) : ccl::make_float3(0, 0, 1),
                                                   .05f * (index % 3), index % 2 ? M_PI_F : M_PI_2_F);
          b.measure.energy = index % 4 == 0 ? 0.f : index % 3 == 0 ? 1e6f : float(index) * .13f;
          b.light_link = ccl::LightTreeLightLink(uint64_t(1) << (index % 6));
        }
      std::array<ccl::LightTreeBucket, 11> left, right;
      left.front() = buckets.front();
      for (int i = 1; i < 11; ++i)
        left[i] = left[i - 1] + buckets[i];
      right.back() = buckets.back();
      for (int i = 9; i >= 0; --i)
        right[i] = right[i + 1] + buckets[i + 1];
      std::array<ccl::LightTreeMeasure, 11> measures;
      ccl::light_tree_right_measures(buckets, measures);
      bool equal = true, duplicate_equal = true;
      const float total = (left.back().measure + buckets.back().measure).calculate();
      float old_min = FLT_MAX, new_min = FLT_MAX;
      int old_split = -1, new_split = -1;
      for (int i = 0; i < 11; ++i) {
        equal &= same(measures[i], right[i].measure);
        const bool skip = ccl::light_tree_duplicate_split(buckets, i);
        duplicates += skip;
        if (skip)
          duplicate_equal &= same(left[i].measure, left[i - 1].measure) &&
                             same(right[i].measure, right[i - 1].measure) && left[i].count == left[i - 1].count;
        volatile const float original = 1.37f * (left[i].measure.calculate() + right[i].measure.calculate());
        volatile const float optimized = 1.37f * (left[i].measure.calculate() + measures[i].calculate());
        equal &= original == optimized;
        if (original < total && original < old_min) {
          old_min = original;
          old_split = i;
        }
        if (!skip && optimized < total && optimized < new_min) {
          new_min = optimized;
          new_split = i;
        }
      }
      require(equal, "Measure-only prefixes changed original float fields or costs");
      require(duplicate_equal, "An empty split crossed different measures or left counts");
      require(old_split == new_split && old_min == new_min,
              "Duplicate skipping changed the chosen split or first-tie policy");
    }
    require(duplicates > 10000, "Empty-bucket occupancy coverage is incomplete");
  }
  {
    using Profile = ccl::LightTreeBuildProfile;
    using Stage = ccl::LightTreeBuildStage;
    require(Profile::weight(2, 2) == 0 && Profile::weight(2, 1) == 0, "Empty profiling ranges were sampled");
    require(Profile::weight(0, 1024) == 1 && Profile::weight(7, 1031) == 1,
            "Large profiling ranges lost full coverage");
    int sampled = 0;
    bool bounded = true;
    for (int start = 0; start < 65536; ++start) {
      const auto weight = Profile::weight(start, start + 1);
      bounded &= weight == 0 || weight == Profile::small_weight;
      sampled += weight != 0;
    }
    require(bounded && sampled > 150 && sampled < 370, "Small-node profiling is not a bounded deterministic sample");
    Profile profile;
    profile.record(Stage::Decision, 1, 1024, 123);
    profile.record(Stage::Decision, 256, 1, 5);
    profile.record(Stage::Decision, 0, 100, 99);
    const auto &stage = profile.stages[int(Stage::Decision)];
    require(stage.samples.load() == 2 && stage.represented_calls.load() == 257, "Profiling sample weights were lost");
    require(stage.nanoseconds.load() == 128 && stage.estimated_nanoseconds.load() == 1403,
            "Profiling time estimates lost integer weights");
    require(stage.represented_emitters.load() == 1280, "Profiling emitter visits lost range weights");
    const ccl::LightTreeBuildTimer disabled(nullptr, Stage::Decision, 0, 1024);
  }
  {
    const auto original = ccl::light_tree_emission_key(3, ccl::make_float3(.125f, .25f, .5f));
    const std::array<std::array<float, 3>, 6> permutations{{{.125f, .25f, .5f},
                                                            {.125f, .5f, .25f},
                                                            {.25f, .125f, .5f},
                                                            {.25f, .5f, .125f},
                                                            {.5f, .125f, .25f},
                                                            {.5f, .25f, .125f}}};
    for (const auto &rgb : permutations)
      for (int signs = 0; signs < 8; ++signs) {
        const auto value =
            ccl::make_float3(signs & 1 ? -rgb[0] : rgb[0], signs & 2 ? -rgb[1] : rgb[1], signs & 4 ? -rgb[2] : rgb[2]);
        require(ccl::light_tree_emission_key(3, value) == original,
                "Equivalent exact emission energy lost cache equality");
        require(ccl::light_tree_emission_energy(value) == ccl::average(ccl::fabs(value)),
                "Key energy differs from triangle consumer");
      }
    for (int sampling : {0, 1, 2, 4})
      require(!(original == ccl::light_tree_emission_key(sampling, ccl::make_float3(.125f, .25f, .5f))),
              "Changed emission orientation reused a mesh hierarchy");
    require(!(original == ccl::light_tree_emission_key(3, ccl::make_float3(.125f, .25f, .5001f))),
            "Changed consumed energy reused a mesh hierarchy");
    const auto energy = ccl::light_tree_emission_energy(ccl::make_float3(.125f, .25f, .5f));
    const auto next = std::nextafter(energy, std::numeric_limits<float>::infinity());
    require(!(original == ccl::LightTreeEmissionKey{3, next}), "One-ULP energy difference was treated approximately");
    require(!(original == ccl::LightTreeEmissionKey{3, std::numeric_limits<float>::quiet_NaN()}),
            "Invalid energy acquired cache equality");
    const std::vector<ccl::LightTreeEmissionKey> slots{original, {3, energy * 2}};
    require(slots != std::vector<ccl::LightTreeEmissionKey>{slots[1], slots[0]},
            "Different per-face palette energies lost their ordering");
  }
  {
    ccl::LightTreeFlattenRange original{7, 11, 4097, 19, 3, 65536, 17, 2048};
    require(original == original, "Exact retained context did not match");
    for (int field = 0; field < 8; ++field) {
      auto changed = original;
      if (field == 0)
        ++changed.identity;
      if (field == 1)
        ++changed.first_node;
      if (field == 2)
        ++changed.num_nodes;
      if (field == 3)
        ++changed.first_emitter;
      if (field == 4)
        ++changed.object_id;
      if (field == 5)
        ++changed.primitive_offset;
      if (field == 6)
        ++changed.visibility;
      if (field == 7)
        ++changed.triangle_offset;
      require(!(original == changed), "A changed live flatten context reused stale rows");
    }
  }
  for (int words : {4, 16})
    for (int count : {1, 3, 31, 255, 1023, 1024, 1025, 2048, 3073, 8193})
      for (int kind : {0, 1, 2}) {
        std::vector<uint32_t> source(size_t(words) * count);
        uint32_t state = 0x916f37a5;
        const std::array<uint32_t, 8> special = {0,           0x80000000u, 0x7f800000u, 0xff800000u,
                                                 0x7fc01234u, 0x7fffffffu, 0xffffffffu, 1};
        for (int record = 0; record < count; ++record)
          for (int word = 0; word < words; ++word) {
            state ^= state << 13;
            state ^= state >> 17;
            state ^= state << 5;
            source[size_t(record) * words + word] = kind == 0   ? special[word % 8]
                                                    : kind == 1 ? (special[word % 8] ^ uint32_t(record / 8))
                                                                : state;
          }
        ccl::LightTreeCacheBlocks packed;
        packed.reset(count, words * 4);
        for (int first = 0; first < count; first += ccl::light_tree_cache_block_size) {
          const int number = std::min(ccl::light_tree_cache_block_size, count - first);
          packed.append(first / ccl::light_tree_cache_block_size, source.data() + size_t(first) * words, number);
          ++blocks;
        }
        std::vector<uint32_t> result(size_t(words) * count + 8, 0x37be1409);
        for (int first = 0; first < count; first += ccl::light_tree_cache_block_size)
          packed.read(first / ccl::light_tree_cache_block_size, result.data() + 4 + size_t(first) * words);
        require(std::memcmp(source.data(), result.data() + 4, source.size() * 4) == 0,
                "Cache changed floating-point/integer bits");
        require(result[0] == 0x37be1409 && result[3] == 0x37be1409 && result[result.size() - 1] == 0x37be1409 &&
                    result[result.size() - 4] == 0x37be1409,
                "Cache decode exceeded the owned output range");
        if (kind == 0 && count > 3)
          require(packed.bytes() < source.size() * 4 / 2, "Repeated records were not compacted");
        if (kind == 2)
          require((packed.blocks[0][0] & 1) == 0, "Incompressible block did not retain exact raw storage");
        rejects([&] { packed.read(-1, result.data()); });
        rejects([&] { packed.read(int(packed.blocks.size()), result.data()); });
        auto corrupt = packed;
        corrupt.blocks[0].clear();
        rejects([&] { corrupt.read(0, result.data()); });
        corrupt = packed;
        corrupt.blocks[0][0] = 9;
        rejects([&] { corrupt.read(0, result.data()); });
        corrupt = packed;
        corrupt.blocks[0].pop_back();
        rejects([&] { corrupt.read(0, result.data()); });
        corrupt = packed;
        corrupt.blocks[0].push_back(0);
        rejects([&] { corrupt.read(0, result.data()); });
        corrupt = packed;
        corrupt.count = 0;
        rejects([&] { corrupt.read(0, result.data()); });
        corrupt = packed;
        corrupt.count += 1;
        rejects([&] { corrupt.read(int(packed.blocks.size()) - 1, result.data()); });
        corrupt = packed;
        corrupt.stride = words == 4 ? 20 : 60;
        rejects([&] { corrupt.read(0, result.data()); });
      }
  ccl::LightTreeCacheBlocks packed;
  rejects([&] { packed.reset(1, 0); });
  rejects([&] { packed.reset(1, 65); });
  rejects([&] { packed.reset(-1, 4); });
  packed.reset(1, 16);
  packed.blocks[0] = {7, 1, 0, 1, 0, 0xff, 0xff, 0xff, 0xff, 0x10};
  std::array<uint32_t, 16> result{};
  rejects([&] { packed.read(0, result.data()); });
  packed.blocks[0] = {7, 1, 0, 0, 0x80};
  rejects([&] { packed.read(0, result.data()); });
  packed.blocks[0] = {7, 1, 0, 1, 0, 0x80, 0x80, 0x80, 0x80, 0x80, 0};
  rejects([&] { packed.read(0, result.data()); });
  rejects([&] { packed.read(0, nullptr); });
  rejects([&] { packed.append(0, nullptr, 1); });
  std::cout << "{\"result\":\"passed\",\"checks\":" << checks << ",\"blocks\":" << blocks << "}" << std::endl;
}
} // namespace cycles_houdini::harness
