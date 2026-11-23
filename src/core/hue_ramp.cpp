// SPDX-License-Identifier: Apache-2.0
// HSV/HSL Color Ramp evaluation for the native Color Ramp node.
// Basis: written from the behaviour description docs/dev/provenance/hue-ramp.md
// (see its Provenance section for who wrote what and from which sources).
// - RGB to HSV: the hexcone model of A. R. Smith, "Color Gamut Transform
//   Pairs", Computer Graphics 12(3), SIGGRAPH 1978, with hue as a fraction of
//   a turn. The blue-sector offset is the one interoperability constant the
//   description pins (section 3.2, fact H1).
// - Between stops: componentwise linear interpolation, the hue along the arc
//   picked by the Hue Interpolation setting (arc table of section 4, facts
//   H2-H5).
// - RGB to HSL and HSV/HSL back to RGB: Cycles' Apache-2.0 util/color.h
//   (SPDX-FileCopyrightText: 2011-2022 Blender Foundation).
// boundaries() lists the stop positions only (the description allows this;
// nothing reads it at present). An earlier helper also added the hue-sector
// and lightness crossings inside each interval; it was dropped for speed
// (about 20 % of the table build with 32 stops, see the description's
// Performance paragraph).
#include "hue_ramp.h"
#include "owned_ramp.h"
#include "util/color.h"
#include <algorithm>
#include <bit>
#include <cmath>

namespace cycles_houdini {
namespace {
// Hue sector offsets as fractions of a turn. The blue offset is float32
// 1 - float32(1/3), one unit in the last place below the float32 nearest 2/3;
// with that value a blue and a yellow stop are not exactly half a turn apart,
// which decides the Far arc in the same way as Blender (description 3.2, H1).
constexpr float green_offset = std::bit_cast<float>(0x3EAAAAABu);
constexpr float blue_offset = std::bit_cast<float>(0x3F2AAAAAu);
static_assert(green_offset == 1.f / 3.f && blue_offset == 1.f - green_offset);

// Hue in [0, 1) by wrapping a value that is at most one turn too large.
float wrap_once(float hue) { return hue >= 1.f ? hue - 1.f : hue; }

// Smith's hexcone: value = largest channel, chroma = largest - smallest,
// saturation = chroma / value, hue from the sector of the largest channel
// (red is preferred on ties, then green).
std::array<float, 3> hexcone_hsv(const std::array<float, 3> &rgb) {
  const float r = rgb[0], g = rgb[1], b = rgb[2];
  const float largest = std::max({r, g, b});
  const float chroma = largest - std::min({r, g, b});
  const float saturation = largest > 0.f ? chroma / largest : 0.f;
  float hue = 0.f;
  if (chroma > 0.f) {
    const float sixfold = 6.f * chroma;
    if (largest == r) {
      hue = (g - b) / sixfold;
      if (hue < 0.f)
        hue += 1.f;
    } else if (largest == g)
      hue = green_offset + (b - r) / sixfold;
    else
      hue = blue_offset + (r - g) / sixfold;
  }
  return {wrap_once(hue), saturation, largest};
}

std::array<float, 3> to_components(const std::array<float, 3> &rgb, bool hsl) {
  if (!hsl)
    return hexcone_hsv(rgb);
  const ccl::float3 c = ccl::rgb_to_hsl(ccl::make_float3(rgb[0], rgb[1], rgb[2]));
  return {wrap_once(c.x), c.y, c.z};
}

std::array<float, 3> to_rgb(float hue, float saturation, float v, bool hsl) {
  const ccl::float3 c = ccl::make_float3(hue, saturation, v);
  const ccl::float3 rgb = hsl ? ccl::hsl_to_rgb(c) : ccl::hsv_to_rgb(c);
  return {rgb.x, rgb.y, rgb.z};
}

// The arc between a left hue a and a right hue b (both in [0, 1)) is chosen by
// raising one of them by a whole turn. direction: 0 Near, 1 Far, 2 Clockwise,
// 3 Counter-Clockwise. The comparisons, their strictness and which side is
// raised are the interoperability table of description section 4 (H3): an
// exact half turn raises nothing for Near or Far, and Far turns once around
// for equal hues.
void choose_arc(float &a, float &b, int direction) {
  const float gap = b - a;
  bool raise_left = false, raise_right = false;
  switch (direction) {
  case 0: // shortest way round
    raise_left = gap > .5f;
    raise_right = gap < -.5f;
    break;
  case 1: // longest way round
    raise_right = gap == 0.f || (gap > -.5f && gap < 0.f);
    raise_left = gap > 0.f && gap < .5f;
    break;
  case 2: // hue never decreases from left to right
    raise_right = gap < 0.f;
    break;
  default: // hue never increases from left to right
    raise_left = gap > 0.f;
    break;
  }
  if (raise_left)
    a += 1.f;
  if (raise_right)
    b += 1.f;
}

} // namespace

HueRamp::HueRamp(std::span<const HueRampKey> keys, bool hsl, int direction) : hsl_(hsl) {
  if (keys.empty() || keys.size() > 32 || direction < 0 || direction > 3)
    throw std::invalid_argument("Malformed hue ramp ownership");
  for (const auto &key : keys)
    if (std::any_of(key.color.begin(), key.color.end(), [](float v) { return v < 0; }))
      throw RampSourceError(
          "HSV/HSL Color Ramp requires non-negative RGB stops, as in Blender; RGB and LAB retain signed values");
  stops_.reserve(keys.size());
  for (const auto &key : keys) {
    const auto components = to_components(key.color, hsl);
    if (!std::all_of(components.begin(), components.end(), [](float v) { return std::isfinite(v); }))
      throw RampSourceError("HSV/HSL Color Ramp conversion is non-finite for these stops");
    stops_.push_back({key.position, key.color, components});
  }
  // spans_[i] belongs to the interval from stop i to stop i + 1.
  spans_.reserve(stops_.size() - 1);
  for (std::size_t i = 0; i < stops_.size(); ++i) {
    boundaries_.push_back(stops_[i].position);
    if (i + 1 == stops_.size())
      break;
    const Stop &left = stops_[i], &right = stops_[i + 1];
    Span span{left.components[0], right.components[0]};
    choose_arc(span.left_hue, span.right_hue, direction);
    spans_.push_back(span);
  }
  std::sort(boundaries_.begin(), boundaries_.end());
  boundaries_.erase(std::unique(boundaries_.begin(), boundaries_.end()), boundaries_.end());
}

std::array<float, 3> HueRamp::evaluate(float position) const {
  // Held ends (H4): before the first stop, or with a single stop, the first
  // stop's own RGB; past the last stop, the last stop's own RGB.
  if (stops_.size() == 1 || position <= stops_.front().position)
    return stops_.front().color;
  // The right stop is the first one strictly after position (H5), so a group
  // of coincident stops jumps exactly at its position.
  const auto right = std::upper_bound(stops_.begin(), stops_.end(), position,
                                      [](float x, const Stop &stop) { return x < stop.position; });
  if (right == stops_.end())
    return stops_.back().color;
  const std::size_t interval = std::size_t(right - stops_.begin()) - 1;
  const Stop &left = stops_[interval];
  const Span &span = spans_[interval];
  // Convex weights of the two stops (linear interpolation).
  const float to_right = (position - left.position) / (right->position - left.position);
  const float to_left = 1.f - to_right;
  const auto blend = [&](float a, float b) { return to_left * a + to_right * b; };
  const float hue = wrap_once(blend(span.left_hue, span.right_hue));
  return to_rgb(hue, blend(left.components[1], right->components[1]), blend(left.components[2], right->components[2]),
                hsl_);
}
} // namespace cycles_houdini
