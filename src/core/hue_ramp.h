// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <array>
#include <span>
#include <vector>
namespace cycles_houdini {
struct HueRampKey {
  float position;
  std::array<float, 3> color;
};
// Only ordinary owned arrays cross the native spline/Cycles SIMD boundary.
class HueRamp {
public:
  HueRamp(std::span<const HueRampKey> keys, bool hsl, int direction);
  std::array<float, 3> evaluate(float position) const;
  std::span<const float> boundaries() const { return boundaries_; }

private:
  struct Stop {
    float position;
    std::array<float, 3> color, components;
  };
  struct Span {
    float left_hue, right_hue;
  };
  bool hsl_;
  std::vector<Stop> stops_;
  std::vector<Span> spans_;
  std::vector<float> boundaries_;
};
} // namespace cycles_houdini
