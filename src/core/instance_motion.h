// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
#include <algorithm>
#include <numeric>

namespace cycles_houdini {
inline std::optional<std::size_t> instance_motion_grid(std::size_t a, std::size_t b) {
  if (a < 3)
    return b < 3 ? 1 : b;
  if (b < 3)
    return a;
  const auto intervals = (a - 1) / std::gcd(a - 1, b - 1) * (b - 1);
  return intervals <= 128 ? std::optional(intervals + 1) : std::nullopt;
}
// Integer lattice positions preserve every old breakpoint and its float bits.
// New in-between positions interpolate components without changing constants.
inline float instance_motion_lerp(float a, float b, std::size_t part, std::size_t whole) {
  if (!part || float_bits_equal(a, b))
    return a;
  if (part == whole)
    return b;
  const float f = float(part) / float(whole);
  return (1 - f) * a + f * b;
}
inline Float2 instance_motion_lerp(Float2 a, Float2 b, std::size_t part, std::size_t whole) {
  return {instance_motion_lerp(a.x, b.x, part, whole), instance_motion_lerp(a.y, b.y, part, whole)};
}
inline Float3 instance_motion_lerp(Float3 a, Float3 b, std::size_t part, std::size_t whole) {
  return {instance_motion_lerp(a.x, b.x, part, whole), instance_motion_lerp(a.y, b.y, part, whole),
          instance_motion_lerp(a.z, b.z, part, whole)};
}
template <class T>
SharedArray<std::vector<T>> instance_resample_stream(const SharedArray<T> &centre,
                                                     const SharedArray<std::vector<T>> &samples, std::size_t steps) {
  SharedArray<std::vector<T>> result;
  const auto source_steps = samples.empty() ? 1 : samples.size() + 1;
  const auto at = [&](std::size_t step) -> const std::vector<T> & {
    const auto middle = (source_steps - 1) / 2;
    if (step == middle)
      return centre;
    return samples[step < middle ? step : step - 1];
  };
  for (std::size_t step = 0; step < steps; ++step) {
    if (step == (steps - 1) / 2)
      continue;
    const auto numerator = step * (source_steps - 1), denominator = steps - 1;
    const auto first = numerator / denominator, remainder = numerator % denominator;
    if (!remainder) {
      result.push_back(at(first));
      continue;
    }
    const auto &a = at(first);
    const auto &b = at(first + 1);
    std::vector<T> values;
    values.reserve(centre.size());
    for (std::size_t i = 0; i < centre.size(); ++i)
      values.push_back(instance_motion_lerp(a[i], b[i], remainder, denominator));
    result.push_back(std::move(values));
  }
  return result;
}
inline MeshData instance_resample_mesh(const MeshData &source, std::size_t steps) {
  auto result = source;
  result.nominal_pose.reset();
  result.motion.positions = instance_resample_stream(source.positions, source.motion.positions, steps);
  // Displacement can introduce motion positions on an otherwise static mesh.
  // Motion triangles still need the authored normal at every shutter step;
  // omitting that stream makes their displacement basis use generated normals.
  if (!source.corner_normals.empty())
    result.motion_corner_normals = instance_resample_stream(source.corner_normals, source.motion_corner_normals, steps);
  if (!source.motion_corner_uv.empty())
    result.motion_corner_uv = instance_resample_stream(source.corner_uv, source.motion_corner_uv, steps);
  if (!source.motion_corner_colors.empty())
    result.motion_corner_colors = instance_resample_stream(source.corner_colors, source.motion_corner_colors, steps);
  for (std::size_t i = 0; i < source.attributes.size(); ++i)
    if (!source.attributes[i].motion_values.empty())
      result.attributes[i].motion_values =
          instance_resample_stream(source.attributes[i].values, source.attributes[i].motion_values, steps);
  return result;
}
} // namespace cycles_houdini
