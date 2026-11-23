// SPDX-License-Identifier: Apache-2.0
// Shader-curve tables for the Float, RGB and Vector Curves nodes.
// - Version 1 sources (authored Bezier points): the per-channel curve (handles,
//   sampled segments, 257-entry table, extension) is written from the behaviour
//   description docs/dev/provenance/curve-mapping.md (see its Provenance
//   section for who wrote it from what). Its rules marked [interop] there
//   exist only to match Blender's CurveMapping results; the mathematics is the
//   textbook cubic Bezier curve (G. Farin, Curves and Surfaces for CAGD, 5th
//   ed., 2002, ch. 4-5), sampled by Horner's rule on its power form (see
//   sample_cubic). The composition of the channels
//   into one table (combined-first RGB, 257 samples over the authored X range)
//   follows Blender's Apache-2.0 Cycles exporter, intern/cycles/blender/util.h
//   (curvemapping_minmax, curvemapping_color_to_array; SPDX-FileCopyrightText:
//   2011-2022 Blender Foundation).
// - Version 2 (native Houdini ramps) and version 3 (tables baked inside Blender
//   through its public Python API) are this project's own code.
#include "shader_curves.h"
#include "owned_ramp.h"
#include <algorithm>
#include <bit>
#include <cmath>
#include <limits>

namespace cycles_houdini {
namespace {
struct XY {
  float x{}, y{};
};
[[maybe_unused]] XY operator+(XY a, XY b) { return {a.x + b.x, a.y + b.y}; }
[[maybe_unused]] XY operator-(XY a, XY b) { return {a.x - b.x, a.y - b.y}; }
[[maybe_unused]] XY operator*(XY a, float b) { return {a.x * b, a.y * b}; }
[[maybe_unused]] XY operator/(XY a, float b) { return {a.x / b, a.y / b}; }
[[maybe_unused]] float length(XY a) { return std::sqrt(a.x * a.x + a.y * a.y); }
bool finite(XY a) { return std::isfinite(a.x) && std::isfinite(a.y); }
float value(int v) { return std::bit_cast<float>(v); }
// One authored control point. handle: 0 Auto, 1 Auto Clamped, 2 Vector.
// left/right are free for the channel builder's use.
struct Point {
  XY p, left, right;
  int handle{};
};
constexpr int table_intervals = 256;          // [interop] 256 intervals, 257 entries
constexpr int samples_per_segment = 32;       // [interop] polyline samples per Bezier segment
constexpr float auto_handle_factor = 2.5614f; // [interop] Auto handle length divisor
constexpr float end_sample_tolerance = 1e-6f; // [interop] lookup snaps to an end sample
constexpr float vertical_extension = 10000.f; // [interop] value step of a vertical end handle

bool finite(float v) { return std::isfinite(v); }

// One prepared channel (one CurveMap): a 257-entry table of the sampled
// polyline over [start, start + 256 * spacing], and the two end directions
// that continue it outside (spec sections 4 and 6).
struct Channel {
  std::array<float, table_intervals + 1> table{};
  float start{}, spacing{1};
  // Unit vectors along the end handles, pointing away from the curve
  // (spec section 4): outward[before] is e_in, outward[after] is -e_out.
  std::array<XY, 2> outward;
  bool extrapolate{};
  float evaluate(float x) const;
  float extension(int side, XY end, float x) const;
};
constexpr int before = 0, after = 1; // the two ends of a channel

// Spec 6.4: the continuation of the curve beyond its end point `end` on the
// given side. The caller decides the side. Extrapolated: the straight line
// through the end point along the end handle; Horizontal: the end value.
float Channel::extension(int side, XY end, float x) const {
  if (!extrapolate)
    return end.y;
  const XY direction = outward[side];
  // A vertical end handle has no slope: the curve steps by a fixed amount
  // along the handle instead.
  const float rise = direction.x == 0.f ? direction.y * vertical_extension : direction.y * (x - end.x) / direction.x;
  return end.y + rise;
}

// Precondition: x is not NaN (prepare_shader_curve checks the one input
// that could be: the Combined curve's value). Infinite x extends as usual.
float Channel::evaluate(float x) const {
  const float position = (x - start) / spacing;
  if (position < 0.f) // exactly when x < start
    return extension(before, {start, table.front()}, x);
  // Dividing (not multiplying by the reciprocal) makes this imply x >= the
  // last entry's X, which matters for the fixed step of a vertical end handle.
  if (position > float(table_intervals))
    return extension(after, {start + spacing * float(table_intervals), table.back()}, x);
  const int index = int(position);
  if (index >= table_intervals)
    return table.back();
  const float blend = position - float(index);
  return table[index] + (table[index + 1] - table[index]) * blend;
}

// Spec 3.1: tangent handles of one point from its two neighbours (a missing
// end neighbour is the mirror image of the other one through the point).
// Returns false, leaving the handles unset, when a chord is too short to have
// a direction.
bool place_handles(Point &point, XY previous, XY next) {
  const XY incoming = point.p - previous, outgoing = next - point.p;
  if (point.handle == 2) {
    // Control points equally spaced on the chord give a straight segment.
    point.left = point.p - incoming / 3.f;
    point.right = point.p + outgoing / 3.f;
    return true;
  }
  const float incoming_length = length(incoming), outgoing_length = length(outgoing);
  // Distinct X makes both chords non-zero; in float32 a chord shorter than
  // about 1e-22 still squares to zero, and its direction is then unknown.
  if (incoming_length == 0.f || outgoing_length == 0.f)
    return false;
  const XY bisector = incoming / incoming_length + outgoing / outgoing_length;
  const float scale = auto_handle_factor * length(bisector);
  if (scale == 0.f) {
    point.left = point.right = point.p;
    return true;
  }
  point.left = point.p - bisector * (incoming_length / scale);
  point.right = point.p + bisector * (outgoing_length / scale);
  return true;
}

// Spec 3.2: Auto Clamped interior points never overshoot their neighbours.
void clamp_interior(Point &point, float previous_y, float next_y) {
  const float below = previous_y - point.p.y, above = next_y - point.p.y;
  if ((below <= 0.f && above <= 0.f) || (below >= 0.f && above >= 0.f)) {
    point.left.y = point.right.y = point.p.y;
  } else if (below <= 0.f) {
    point.left.y = std::max(previous_y, point.left.y);
    point.right.y = std::min(next_y, point.right.y);
  } else {
    point.left.y = std::min(previous_y, point.left.y);
    point.right.y = std::max(next_y, point.right.y);
  }
}

// Spec 3.3: an Auto end point aims at its neighbour's nearer handle, keeping
// the length of its own handle. `own` is the handle on the curve's side.
void aim_end(Point &end, XY target, XY &own, XY &opposite) {
  const XY offset = target - end.p;
  const float distance = length(offset);
  if (!(distance > std::numeric_limits<float>::epsilon()))
    return;
  const XY aimed = offset * (length(own - end.p) / distance);
  own = end.p + aimed;
  opposite = end.p - aimed;
}

// The sample parameters s = k / 31 of spec 5.2, computed once.
constexpr std::array<float, samples_per_segment> sample_parameters = [] {
  std::array<float, samples_per_segment> s{};
  for (int k = 0; k < samples_per_segment; ++k)
    s[k] = float(k) / float(samples_per_segment - 1);
  return s;
}();

// One coordinate of a cubic Bezier segment with control values p0..p3,
// sampled at the 32 parameters: the Bernstein form rewritten once per segment
// in the power basis, p0 + s (c1 + s (c2 + s c3)), and evaluated by Horner's
// rule (Bernstein form: Farin, 5th ed., ch. 5; Horner's rule: D. E. Knuth,
// TAOCP vol. 2, section 4.6.4). The end samples are the end points
// themselves. Returns false if a sample is not finite.
bool sample_cubic(float p0, float p1, float p2, float p3, float *out) {
  const float c1 = 3.f * (p1 - p0), c2 = 3.f * (p0 - 2.f * p1 + p2), c3 = p3 - p0 + 3.f * (p1 - p2);
  bool finite_samples = true;
  for (int k = 0; k < samples_per_segment; ++k) {
    const float s = sample_parameters[k];
    out[k] = p0 + s * (c1 + s * (c2 + s * c3));
    finite_samples &= std::abs(out[k]) <= std::numeric_limits<float>::max(); // false for NaN
  }
  out[0] = p0;
  out[samples_per_segment - 1] = p3;
  return finite_samples;
}

Channel build_channel(std::vector<Point> points, float domain_min, float domain_max, bool extend) {
  std::stable_sort(points.begin(), points.end(), [](const Point &a, const Point &b) { return a.p.x < b.p.x; });
  for (std::size_t i = 1; i < points.size(); ++i)
    if (!(points[i].p.x > points[i - 1].p.x))
      throw ShaderCurveSourceError("Curve points require finite, distinct X positions");
  const std::size_t n = points.size();
  for (std::size_t i = 0; i < n; ++i) {
    const XY previous = i > 0 ? points[i - 1].p : points[0].p * 2.f - points[1].p;
    const XY next = i + 1 < n ? points[i + 1].p : points[n - 1].p * 2.f - points[n - 2].p;
    if (!place_handles(points[i], previous, next))
      throw ShaderCurveSourceError("Curve points are too close together to place their handles");
    if (points[i].handle == 1 && i > 0 && i + 1 < n)
      clamp_interior(points[i], points[i - 1].p.y, points[i + 1].p.y);
  }
  if (n > 2) {
    Point &first = points.front(), &last = points.back();
    if (first.handle == 0) {
      XY target = points[1].left;
      target.x = std::max(target.x, first.p.x);
      aim_end(first, target, first.right, first.left);
    }
    if (last.handle == 0) {
      XY target = points[n - 2].right;
      target.x = std::min(target.x, last.p.x);
      aim_end(last, target, last.left, last.right);
    }
  }

  Channel channel;
  channel.extrapolate = extend;
  auto unit = [](XY v) {
    const float size = length(v);
    if (!std::isfinite(size) || size == 0.f)
      throw ShaderCurveSourceError("Curve end handles require a finite, non-zero length");
    return v / size;
  };
  channel.outward = {unit(points.front().left - points.front().p), unit(points.back().right - points.back().p)};

  // Spec 5: each segment becomes 32 polyline samples at equal parameter steps,
  // stored as separate X and Y arrays.
  const std::size_t m = (n - 1) * samples_per_segment;
  std::vector<float> sample_x(m), sample_y(m);
  for (std::size_t j = 1; j < n; ++j) {
    const XY a = points[j - 1].p, b = points[j].p;
    XY out_offset = a - points[j - 1].right, in_offset = b - points[j].left;
    const float reach = std::abs(out_offset.x) + std::abs(in_offset.x), width = b.x - a.x;
    if (reach > width) { // Spec 5.1: keep x(s) monotone.
      const float shrink = width / reach;
      out_offset = out_offset * shrink;
      in_offset = in_offset * shrink;
    }
    const XY h1 = a - out_offset, h2 = b - in_offset;
    const std::size_t first = (j - 1) * samples_per_segment;
    if (!sample_cubic(a.x, h1.x, h2.x, b.x, &sample_x[first]) || !sample_cubic(a.y, h1.y, h2.y, b.y, &sample_y[first]))
      throw ShaderCurveSourceError("Curve segment produced non-finite samples");
  }

  // Spec 6.1/6.2: 257 entries over the clipping range widened to the points.
  channel.start = std::min(domain_min, points.front().p.x);
  const float end = std::max(domain_max, points.back().p.x);
  channel.spacing = (end - channel.start) / float(table_intervals);
  if (!finite(channel.spacing) || !(channel.spacing > 0.f))
    throw ShaderCurveSourceError("Curve table step must be finite and positive");
  std::size_t cursor = 0; // first sample with X greater than the entry's X
  for (int i = 0; i <= table_intervals; ++i) {
    const float x = channel.start + channel.spacing * float(i);
    while (cursor < m && !(sample_x[cursor] > x))
      ++cursor;
    float result;
    if (cursor == 0 || cursor == m) {
      const std::size_t e = cursor == 0 ? 0 : m - 1;
      const XY end_sample{sample_x[e], sample_y[e]};
      result = std::abs(x - end_sample.x) <= end_sample_tolerance
                   ? end_sample.y
                   : channel.extension(cursor == 0 ? before : after, end_sample, x);
    } else {
      // sample_x[cursor - 1] <= x < sample_x[cursor], so the gap is positive.
      const std::size_t u = cursor, l = cursor - 1;
      result = sample_y[l] + (sample_y[u] - sample_y[l]) * ((x - sample_x[l]) / (sample_x[u] - sample_x[l]));
    }
    if (!finite(result))
      throw ShaderCurveSourceError("Curve table produced non-finite values");
    channel.table[i] = result;
  }
  return channel;
}
} // namespace

ShaderCurveTable prepare_shader_curve(std::span<const int> source, ShaderCurveKind kind) {
  const int channels = kind == ShaderCurveKind::Float ? 1 : kind == ShaderCurveKind::RGB ? 4 : 3;
  if (source.empty()) {
    ShaderCurveTable identity;
    identity.minimum = kind == ShaderCurveKind::Vector ? -1.f : 0.f;
    identity.maximum = 1;
    for (int i = 0; i <= 256; ++i)
      identity.values[i].fill(identity.minimum + (identity.maximum - identity.minimum) * (i / 256.f));
    return identity;
  }
  if (source.size() >= 8 && source[0] == shader_curve_magic && source[1] == 3) {
    if (source.size() != 8 + 257 * 3 || source[2] != int(kind) || source[3] < 0 || source[3] > 1 || source[6] ||
        source[7] != 257)
      throw std::invalid_argument("Malformed Blender shader-curve table");
    ShaderCurveTable result;
    result.minimum = value(source[4]);
    result.maximum = value(source[5]);
    result.extrapolate = source[3] != 0;
    if (!std::isfinite(result.minimum) || !std::isfinite(result.maximum) || result.minimum >= result.maximum ||
        !std::isfinite(result.maximum - result.minimum))
      throw ShaderCurveSourceError("Blender curve range requires finite increasing bounds");
    for (std::size_t i = 0; i < result.values.size(); ++i)
      for (int c = 0; c < 3; ++c) {
        result.values[i][c] = value(source[8 + 3 * i + c]);
        if (!std::isfinite(result.values[i][c]))
          throw ShaderCurveSourceError("Blender curve table requires finite values");
      }
    return result;
  }
  if (source.size() >= 8 && source[0] == shader_curve_magic && source[1] == 2) {
    if (source[2] != int(kind) || source[3] < 0 || source[3] > 1 || source[6] || source[7] != channels)
      throw std::invalid_argument("Malformed native shader-curve header");
    ShaderCurveTable result;
    result.minimum = value(source[4]);
    result.maximum = value(source[5]);
    result.extrapolate = source[3] != 0;
    const float range = result.maximum - result.minimum;
    if (!std::isfinite(result.minimum) || !std::isfinite(result.maximum) || !std::isfinite(range) || range <= 0)
      throw ShaderCurveSourceError("Curve domain requires finite increasing bounds");
    struct NativeChannel {
      std::vector<int> source;
      float before, after, first, last, first_value, last_value;
      std::vector<float> evaluate(std::span<const float> factors, bool extend) const {
        std::vector<std::array<float, 3>> samples;
        try {
          samples = sample_owned_ramp(source, factors);
        } catch (const RampSourceError &e) {
          throw ShaderCurveSourceError(e.what());
        }
        std::vector<float> values;
        values.reserve(factors.size());
        for (std::size_t i = 0; i < factors.size(); ++i) {
          const float x = factors[i];
          values.push_back(extend && x < first  ? first_value + before * (x - first)
                           : extend && x > last ? last_value + after * (x - last)
                                                : samples[i][0]);
        }
        return values;
      }
    };
    std::vector<NativeChannel> maps;
    std::size_t offset = 8;
    for (int c = 0; c < channels; ++c) {
      if (offset + 7 > source.size() || source[offset] < 8 || source[offset] > 168 ||
          std::size_t(source[offset]) > source.size() - offset - 7)
        throw std::invalid_argument("Malformed native shader-curve channel");
      const auto size = std::size_t(source[offset]);
      NativeChannel channel;
      float *fields[] = {&channel.before, &channel.after,       &channel.first,
                         &channel.last,   &channel.first_value, &channel.last_value};
      for (int i = 0; i < 6; ++i) {
        *fields[i] = value(source[offset + i + 1]);
        if (!std::isfinite(*fields[i]))
          throw ShaderCurveSourceError("Curve endpoint controls require finite values");
      }
      if (channel.first < 0 || channel.last > 1 || channel.first > channel.last)
        throw std::invalid_argument("Malformed native curve endpoint range");
      try {
        channel.source.assign(source.begin() + offset + 7, source.begin() + offset + 7 + size);
        prepare_owned_ramp(channel.source); // Validate controls even if this channel is dormant.
      } catch (const RampSourceError &e) {
        throw ShaderCurveSourceError(e.what());
      }
      maps.push_back(std::move(channel));
      offset += size + 7;
    }
    if (offset != source.size())
      throw std::invalid_argument("Trailing native shader-curve controls");
    std::array<float, 257> factors;
    for (std::size_t i = 0; i < factors.size(); ++i)
      factors[i] = float(i) / 256.f;
    if (kind == ShaderCurveKind::RGB) {
      const auto common = maps[3].evaluate(factors, result.extrapolate);
      for (std::size_t i = 0; i < factors.size(); ++i)
        factors[i] = (common[i] - result.minimum) / range;
    }
    for (int c = 0; c < 3; ++c) {
      const auto values = maps[kind == ShaderCurveKind::Float ? 0 : c].evaluate(factors, result.extrapolate);
      for (std::size_t i = 0; i < factors.size(); ++i) {
        result.values[i][c] = values[i];
        if (!std::isfinite(result.values[i][c]))
          throw ShaderCurveSourceError("Native curve table produced non-finite values");
      }
    }
    return result;
  }
  if (source.size() < 8 || source[0] != shader_curve_magic || source[1] != 1 || source[2] != int(kind) ||
      source[3] < 0 || source[3] > 1 || source[6] || source[7] != channels)
    throw std::invalid_argument("Malformed owned shader-curve header");
  const float domain_min = value(source[4]), domain_max = value(source[5]);
  if (!std::isfinite(domain_min) || !std::isfinite(domain_max) || domain_min >= domain_max)
    throw ShaderCurveSourceError("Curve domain requires finite increasing bounds");
  std::vector<Channel> maps;
  std::size_t offset = 8;
  ShaderCurveTable result;
  result.minimum = std::numeric_limits<float>::max();
  result.maximum = -result.minimum;
  result.extrapolate = source[3] != 0;
  for (int j = 0; j < channels; ++j) {
    if (offset >= source.size())
      throw std::invalid_argument("Truncated owned shader-curve controls");
    const int count = source[offset++];
    if (count < 2 || count > 64)
      throw ShaderCurveSourceError("Each curve supports 2..64 authored points");
    if (source.size() - offset < std::size_t(3 * count))
      throw std::invalid_argument("Truncated owned shader-curve points");
    std::vector<Point> points;
    points.reserve(count);
    for (int i = 0; i < count; ++i) {
      const XY position{value(source[offset]), value(source[offset + 1])};
      const int handle = source[offset + 2];
      // Validate before ordering: NaN X values violate sort's comparator
      // contract and must be a local source problem instead.
      if (!finite(position) || handle < 0 || handle > 2)
        throw ShaderCurveSourceError("Curve points require finite positions and supported handles");
      points.push_back({position, {}, {}, handle});
      offset += 3;
    }
    auto [lo, hi] =
        std::minmax_element(points.begin(), points.end(), [](const auto &a, const auto &b) { return a.p.x < b.p.x; });
    result.minimum = std::min(result.minimum, lo->p.x);
    result.maximum = std::max(result.maximum, hi->p.x);
    maps.push_back(build_channel(std::move(points), domain_min, domain_max, result.extrapolate));
  }
  if (offset != source.size())
    throw std::invalid_argument("Trailing owned shader-curve controls");
  if (!std::isfinite(result.maximum - result.minimum) || result.minimum >= result.maximum)
    throw ShaderCurveSourceError("Combined curve range cannot be represented safely");
  for (int i = 0; i <= 256; ++i) {
    const float x = result.minimum + (i / 256.f) * (result.maximum - result.minimum);
    const float input = kind == ShaderCurveKind::RGB ? maps[3].evaluate(x) : x;
    if (std::isnan(input))
      throw ShaderCurveSourceError("Combined curve produced non-finite values");
    for (int c = 0; c < 3; ++c) {
      result.values[i][c] = maps[kind == ShaderCurveKind::Float ? 0 : c].evaluate(input);
      if (!std::isfinite(result.values[i][c]))
        throw ShaderCurveSourceError("Combined curve produced non-finite values");
    }
  }
  return result;
}
} // namespace cycles_houdini
