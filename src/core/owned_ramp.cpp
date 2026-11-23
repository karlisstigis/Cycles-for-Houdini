// SPDX-License-Identifier: Apache-2.0
// Owned Color Ramp sources: native Houdini ramps (UT_Ramp), Blender-style HSV/HSL
// ramps (hue_ramp.cpp), linear scalar stops, imported Blender tables, and the
// 257-entry table lookup. No OP/HOM/UI access.
// The Blender-style RGB interpolation (rgb_band: Constant, Linear, Ease,
// B-Spline, Cardinal) was rewritten from the behaviour description
// docs/dev/provenance/color-ramp-rgb.md with textbook formulas (see its
// Provenance section); an earlier version that followed Blender's
// GPL-2.0-or-later source was removed.
#include "owned_ramp.h"
#include "hue_ramp.h"
#include <UT/UT_Ramp.h>
#include <algorithm>
#include <bit>
#include <cmath>
#include <optional>

namespace cycles_houdini {
namespace {
using Color = std::array<float, 3>;
float value(int bits) { return std::bit_cast<float>(bits); }
bool finite(const Color &c) {
  return std::all_of(c.begin(), c.end(), [](float f) { return std::isfinite(f); });
}
Color color_at(std::span<const int> data, std::size_t start) {
  return {value(data[start]), value(data[start + 1]), value(data[start + 2])};
}
// Blender-style RGB Color Ramp at x (already clamped to [0, 1]). keys: 1..32
// stops, positions non-decreasing; method 0 Constant, 1 Linear, 2 Ease,
// 3 B-Spline, 4 Cardinal. Written from docs/dev/provenance/color-ramp-rgb.md
// (facts R1-R7 are its interoperability rules; references at each formula).
Color rgb_band(std::span<const HueRampKey> keys, int method, float x) {
  if (method < 0 || method > 4)
    throw std::invalid_argument("Unsupported Color Ramp interpolation");
  const std::size_t count = keys.size();
  if (count == 1)
    return keys.front().color;
  // First stop strictly after x (R2): coincident stops jump at their position.
  const std::size_t after = std::size_t(
      std::upper_bound(keys.begin(), keys.end(), x, [](float at, const HueRampKey &key) { return at < key.position; }) -
      keys.begin());
  if (method <= 2) {
    // Held ends (R1).
    if (x <= keys.front().position)
      return keys.front().color;
    if (after == count)
      return keys.back().color;
    // Inside: keys[after - 1].position <= x < keys[after].position, so the
    // interval has positive length.
    const HueRampKey &low = keys[after - 1], &high = keys[after];
    if (method == 0)
      return low.color;
    const float u = std::clamp((x - low.position) / (high.position - low.position), 0.f, 1.f);
    // Ease: the Hermite smoothstep 3u^2 - 2u^3 (K. Perlin, "An image
    // synthesizer", SIGGRAPH 1985) applied to the linear parameter (R5).
    const float t = method == 2 ? u * u * (3.f - 2.f * u) : u;
    Color out;
    for (int c = 0; c < 3; ++c)
      out[c] = low.color[c] + t * (high.color[c] - low.color[c]);
    return out;
  }
  // The interval [left, right] and the outer neighbours used by the cubic
  // forms. Outside the stops the splines use a virtual stop at 0 or 1 that
  // repeats the end colour (R3); a missing or virtual neighbour is replaced by
  // the interval end itself (R4).
  // Pointers to the four control colours (outer left, left, right, outer
  // right) and the interval's end positions.
  const Color *outer_left, *left, *right, *outer_right;
  float left_position, right_position;
  bool right_is_virtual_end = false;
  if (after == 0) {
    left_position = 0.f;
    right_position = keys.front().position;
    outer_left = left = right = &keys.front().color;
    outer_right = &keys[1].color;
  } else if (after == count) {
    left_position = keys.back().position;
    right_position = 1.f;
    right_is_virtual_end = true;
    outer_left = &keys[count - 2].color;
    left = right = outer_right = &keys.back().color;
  } else {
    left_position = keys[after - 1].position;
    right_position = keys[after].position;
    left = &keys[after - 1].color;
    right = &keys[after].color;
    outer_left = after >= 2 ? &keys[after - 2].color : left;
    outer_right = after + 1 < count ? &keys[after + 1].color : right;
  }
  float u;
  if (left_position == right_position)
    u = right_is_virtual_end ? 0.f : 1.f; // R3: zero-length virtual interval
  else
    u = std::clamp((x - left_position) / (right_position - left_position), 0.f, 1.f);
  Color out;
  const float u2 = u * u, u3 = u2 * u;
  float w[4]; // weights of outer_left, left, right, outer_right
  if (method == 3) {
    // Uniform cubic B-spline basis (Bartels, Beatty and Barsky 1987; Foley et
    // al., Computer Graphics: Principles and Practice, 2nd ed., section 11.2,
    // parametric cubic curves).
    const float v = 1.f - u;
    w[0] = v * v * v / 6.f;
    w[1] = (3.f * u3 - 6.f * u2 + 4.f) / 6.f;
    w[2] = (-3.f * u3 + 3.f * u2 + 3.f * u + 1.f) / 6.f;
    w[3] = u3 / 6.f;
  } else {
    // Cardinal spline as a cubic Hermite segment (Foley et al. 11.2) with
    // tangents k (right - outer_left) and k (outer_right - left), k = 0.71
    // (R6), rewritten as weights of the four control colours.
    constexpr float k = .71f;
    const float h_left = 2.f * u3 - 3.f * u2 + 1.f, h_right = -2.f * u3 + 3.f * u2;
    const float h_tan_left = u3 - 2.f * u2 + u, h_tan_right = u3 - u2;
    w[0] = -k * h_tan_left;
    w[1] = h_left - k * h_tan_right;
    w[2] = h_right + k * h_tan_left;
    w[3] = k * h_tan_right;
  }
  for (int c = 0; c < 3; ++c) {
    const float value = w[0] * (*outer_left)[c] + w[1] * (*left)[c] + w[2] * (*right)[c] + w[3] * (*outer_right)[c];
    if (!std::isfinite(value))
      throw RampSourceError("RGB spline interpolation exceeded finite precision");
    out[c] = std::clamp(value, 0.f, 1.f); // R7: only the two spline modes clip
  }
  return out;
}
} // namespace

std::vector<Color> sample_owned_ramp(std::span<const int> source, std::span<const float> factors, bool *interpolate) {
  if (source.size() >= 2 && source[0] == ramp_source_magic && source[1] == 8) {
    if (source.size() != 8 + 257 * 3 || source[2] != 257 || source[3] || source[4] < 0 || source[4] > 1 ||
        source[5] < 0 || source[5] > 1 || source[6] || source[7])
      throw std::invalid_argument("Malformed imported Blender ramp table");
    OwnedRampTable table;
    table.interpolate = source[5] != 0;
    for (std::size_t i = 0; i < table.values.size(); ++i) {
      table.values[i] = color_at(source, 8 + 3 * i);
      if (!finite(table.values[i]))
        throw RampSourceError("Imported Blender ramp table requires finite values");
      if (!source[4] && (table.values[i][0] != table.values[i][1] || table.values[i][0] != table.values[i][2]))
        throw std::invalid_argument("Imported scalar ramp table requires matching channels");
    }
    if (interpolate)
      *interpolate = table.interpolate;
    std::vector<Color> samples;
    samples.reserve(factors.size());
    for (float factor : factors) {
      if (!std::isfinite(factor))
        throw RampSourceError("Ramp sample factor must be finite");
      samples.push_back(evaluate_owned_ramp(table, factor));
    }
    return samples;
  }
  if (source.size() >= 2 && source[0] == ramp_source_magic && source[1] == 3) {
    // Explicit host source failures carry a bounded reason code, rather
    // than unsafe numbers or a whole-material replacement. Their private
    // ownership is still validated strictly before applying local recovery.
    if (source.size() != 8 || source[2] != 0 || source[3] < 1 || source[3] > 7 || (source[4] != 0 && source[4] != 1) ||
        source[5] || source[6] || source[7])
      throw std::invalid_argument("Malformed owned ramp source fault");
    constexpr const char *reasons[] = {"",
                                       "Ramp requires 1..32 matching keys, values and bases",
                                       "Ramp keys, values and Factor require finite float32 components",
                                       "Ramp keys are outside their supported order or domain",
                                       "Unsupported Color Ramp interpolation",
                                       "Unsupported Color Ramp Color Mode",
                                       "Unsupported Color Ramp Hue Interpolation",
                                       "Color Ramp channels are missing or incompatible"};
    throw RampSourceError(reasons[source[3]]);
  }
  if (source.size() < 8 || source[0] != ramp_source_magic ||
      (source[1] != 1 && source[1] != 2 && source[1] != 4 && source[1] != 5) || source[2] < 1 || source[2] > 32 ||
      source.size() != std::size_t(8 + 5 * source[2]) || source[3] < 0 || source[3] > (source[1] == 2 ? 3 : 2) ||
      (source[4] != 0 && source[4] != 1) || (!source[4] && source[3]) ||
      (source[1] == 2   ? (source[4] != 1 || (source[3] != 1 && source[3] != 3) || source[5] < 0 || source[5] > 3)
       : source[1] == 5 ? (source[3] != 0 || source[5] < 0 || source[5] > 4)
                        : source[5] != 0) ||
      (source[1] == 4 && (source[3] != 0 || source[4] != 0)) || source[6] || source[7])
    throw std::invalid_argument("Malformed owned ramp control header");
  const bool blender_hue = source[1] == 2;
  const bool linear_stops = source[1] == 4;
  const bool blender_rgb = source[1] == 5;
  constexpr UT_SPLINE_BASIS bases[] = {UT_SPLINE_CONSTANT,      UT_SPLINE_LINEAR, UT_SPLINE_CATMULL_ROM,
                                       UT_SPLINE_MONOTONECUBIC, UT_SPLINE_BEZIER, UT_SPLINE_BSPLINE,
                                       UT_SPLINE_HERMITE};
  UT_Ramp ramp;
  ramp.clearAndDestroy();
  constexpr UT_ColorType spaces[] = {UT_RGB, UT_HSV, UT_LAB};
  if (!blender_hue)
    ramp.setColorType(spaces[source[3]]);
  std::vector<HueRampKey> hue_keys;
  float previous = -1.f;
  for (int i = 0; i < source[2]; ++i) {
    const auto at = std::size_t(8 + 5 * i);
    const auto basis = source[at];
    const float key = value(source[at + 1]);
    const auto color = color_at(source, at + 2);
    if ((!blender_hue && !linear_stops && !blender_rgb && (basis < 0 || basis > 6)) ||
        ((blender_hue || linear_stops || blender_rgb) && basis != 1) || !std::isfinite(key) || key < 0 || key > 1 ||
        (blender_hue || linear_stops || blender_rgb ? key < previous : key <= previous) || !finite(color) ||
        (!source[4] && (color[0] != color[1] || color[0] != color[2])))
      throw std::invalid_argument("Malformed owned ramp controls");
    previous = key;
    if (blender_hue || linear_stops || blender_rgb)
      hue_keys.push_back({key, color});
    else {
      float c[4] = {color[0], color[1], color[2], source[4] ? 1.f : color[0]};
      ramp.addNode(key, c, bases[basis], false);
    }
  }
  std::optional<HueRamp> hue;
  if (blender_hue) {
    hue.emplace(hue_keys, source[3] == 3, source[5]);
  } else if (!linear_stops && !blender_rgb)
    ramp.ensureRampIsBuilt();
  const auto lookup = [&](double x) {
    Color out;
    if (blender_rgb)
      out = rgb_band(hue_keys, source[5], float(x));
    else if (hue)
      out = hue->evaluate(float(x));
    else if (linear_stops) {
      const float at = float(x);
      const auto right = std::upper_bound(hue_keys.begin(), hue_keys.end(), at,
                                          [](float p, const HueRampKey &key) { return p < key.position; });
      if (at <= hue_keys.front().position)
        out = hue_keys.front().color;
      else if (right == hue_keys.end())
        out = hue_keys.back().color;
      else {
        const auto &left = *(right - 1);
        const float f = (at - right->position) / (left.position - right->position);
        for (int n = 0; n < 3; ++n)
          out[n] = (1 - f) * right->color[n] + f * left.color[n];
      }
    } else {
      float c[4];
      ramp.getColor(x, c);
      out = {c[0], c[1], c[2]};
    }
    if (!finite(out))
      throw RampSourceError("Native ramp interpolation produced a non-finite value");
    return out;
  };
  // Direct authored-control samples are shared by standalone ramps and
  // composed RGB curves, so Combined does not introduce a second lookup bake.
  if (interpolate) {
    bool constant = !blender_hue && !linear_stops && !blender_rgb;
    for (int i = 0; constant && i < source[2]; ++i)
      constant = source[8 + 5 * i] == 0;
    *interpolate = !(blender_rgb && source[5] == 0) && !constant;
  }
  std::vector<Color> samples;
  samples.reserve(factors.size());
  for (float factor : factors) {
    if (!std::isfinite(factor))
      throw RampSourceError("Ramp sample factor must be finite");
    samples.push_back(lookup(std::clamp(factor, 0.f, 1.f)));
  }
  return samples;
}

OwnedRampTable prepare_owned_ramp(std::span<const int> source) {
  OwnedRampTable table;
  std::array<float, 257> factors;
  for (std::size_t i = 0; i < factors.size(); ++i)
    factors[i] = float(i) / 256.f;
  const auto samples = sample_owned_ramp(source, factors, &table.interpolate);
  std::copy(samples.begin(), samples.end(), table.values.begin());
  return table;
}

std::array<float, 3> evaluate_owned_ramp(const OwnedRampTable &table, float factor) {
  if (!std::isfinite(factor) || factor <= 0)
    return table.values.front();
  if (factor >= 1)
    return table.values.back();
  const float position = factor * 256.f;
  const auto index = std::size_t(position);
  if (!table.interpolate)
    return table.values[index];
  const float f = position - float(index);
  Color result;
  for (int c = 0; c < 3; ++c)
    result[c] = (1 - f) * table.values[index][c] + f * table.values[index + 1][c];
  return result;
}
} // namespace cycles_houdini
