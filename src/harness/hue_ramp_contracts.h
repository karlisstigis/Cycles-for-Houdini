// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "../core/owned_ramp.h"
#include <bit>
#include <cmath>
#include <iostream>
#include <limits>

namespace cycles_houdini::harness {
inline void hue_ramp_contracts() {
  int checks = 0;
  const auto bits = [](float value) { return std::bit_cast<int>(value); };
  const std::vector<int> good{ramp_source_magic, 2,       2, 1, 1, 0,       0, 0, 1,
                              bits(0),           bits(1), 0, 0, 1, bits(1), 0, 0, bits(1)};
  const auto reject = [&](const std::vector<int> &source) {
    ++checks;
    try {
      prepare_owned_ramp(source);
    } catch (const RampSourceError &) {
      throw std::runtime_error("Malformed internal hue ownership was treated as an artist source problem");
    } catch (const std::invalid_argument &) {
      return;
    }
    throw std::runtime_error("Malformed hue ownership was accepted");
  };
  for (const auto [field, value] :
       std::vector<std::pair<int, int>>{{0, 0},
                                        {1, 0},
                                        {1, 4},
                                        {2, 0},
                                        {2, 33},
                                        {3, 0},
                                        {3, 2},
                                        {3, 4},
                                        {4, 0},
                                        {4, 2},
                                        {5, -1},
                                        {5, 4},
                                        {6, 1},
                                        {7, 1},
                                        {8, 0},
                                        {8, 2},
                                        {9, bits(-.1f)},
                                        {9, bits(1.1f)},
                                        {9, bits(std::numeric_limits<float>::quiet_NaN())},
                                        {10, bits(std::numeric_limits<float>::infinity())}}) {
    auto source = good;
    source[field] = value;
    reject(source);
  }
  auto source = good;
  source.pop_back();
  reject(source);
  source = good;
  source.push_back(0);
  reject(source);
  source = good;
  source[9] = bits(.8f);
  source[14] = bits(.2f);
  reject(source);
  const auto recover = [&](std::vector<int> source) {
    ++checks;
    try {
      prepare_owned_ramp(source);
    } catch (const RampSourceError &) {
      return;
    }
    throw std::runtime_error("Unsafe authored hue colors did not produce local recovery");
  };
  source = good;
  source[10] = bits(-.1f);
  recover(source);
  source = good;
  source[3] = 3;
  source[10] = bits(1.5f);
  source[11] = bits(.5f);
  source[12] = bits(.7f);
  recover(source);
  for (int reason = 1; reason <= 7; ++reason)
    recover({ramp_source_magic, 3, 0, reason, 1, 0, 0, 0});
  for (const auto [field, value] :
       std::vector<std::pair<int, int>>{{2, 1}, {3, 0}, {3, 8}, {4, 2}, {5, 1}, {6, 1}, {7, 1}}) {
    source = {ramp_source_magic, 3, 0, 1, 1, 0, 0, 0};
    source[field] = value;
    reject(source);
  }
  for (int space : {1, 3})
    for (int direction = 0; direction < 4; ++direction) {
      source = good;
      source[3] = space;
      source[5] = direction;
      auto program = prepare_owned_ramp(source);
      ++checks;
      for (float factor : {0.f, .125f, .5f, .875f, 1.f})
        for (float value : evaluate_owned_ramp(program, factor))
          if (!std::isfinite(value))
            throw std::runtime_error("Valid hue program produced a non-finite value");
    }
  // Coincident stops are represented by the same uniform table as Blender.
  source = good;
  source[9] = source[14] = bits(.1f);
  auto program = prepare_owned_ramp(source);
  ++checks;
  const float at = .1f * 256;
  const int index = int(at);
  std::array<float, 3> expected;
  for (int c = 0; c < 3; ++c)
    expected[c] = (1 - (at - index)) * program.values[index][c] + (at - index) * program.values[index + 1][c];
  if (program.values.size() != 257 || evaluate_owned_ramp(program, .1f) != expected)
    throw std::runtime_error("Hue table lookup differs from Blender's uniform interpolation");
  // Scalar alpha is not converted through HSV/HSL. Its independent linear
  // band accepts coincident stops, keeps exact boundaries and signed values.
  source = {ramp_source_magic, 4,           3,           0, 0,         0,          0,          0,          1, bits(.1f),
            bits(-.25f),       bits(-.25f), bits(-.25f), 1, bits(.1f), bits(.75f), bits(.75f), bits(.75f), 1, bits(.9f),
            bits(.5f),         bits(.5f),   bits(.5f)};
  program = prepare_owned_ramp(source);
  ++checks;
  if (std::abs(evaluate_owned_ramp(program, .5f)[0] - .625f) > 2e-6f || program.values.size() != 257)
    throw std::runtime_error("Independent alpha ramp lost scalar table interpolation");
  const auto scalar = source;
  for (const auto [field, value] :
       std::vector<std::pair<int, int>>{{3, 1}, {4, 1}, {5, 1}, {8, 0}, {11, 0}, {9, bits(.2f)}}) {
    source = scalar;
    source[field] = value;
    reject(source);
  }
  // Global RGB owns its method, scalar/color output and linear stop encoding.
  source = {ramp_source_magic, 5, 3, 0,       1, 3, 0,      0, 1, 0, bits(1), 0, 0, 1, 0, 0,
            bits(1),           0, 1, bits(1), 0, 0, bits(1)};
  const auto rgb = source;
  program = prepare_owned_ramp(source);
  ++checks;
  if (evaluate_owned_ramp(program, -.1f) != evaluate_owned_ramp(program, 0.f))
    throw std::runtime_error("RGB spline did not clamp Factor to its zero boundary");
  for (const auto [field, value] : std::vector<std::pair<int, int>>{
           {3, 1}, {4, 2}, {5, -1}, {5, 5}, {8, 0}, {8, 2}, {9, bits(-.1f)}, {6, 1}, {7, 1}}) {
    source = rgb;
    source[field] = value;
    reject(source);
  }
  for (int method = 0; method < 5; ++method) {
    source = rgb;
    source[5] = method;
    program = prepare_owned_ramp(source);
    ++checks;
  }
  std::cout << "{\"result\":\"passed\",\"hue_ramp_checks\":" << checks << "}\n";
}
} // namespace cycles_houdini::harness
