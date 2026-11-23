// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
#include <GEO/GEO_Curve.h>
#include <GU/GU_Detail.h>
#include <cmath>
#include <limits>
#include <unordered_map>

namespace cycles_houdini {
struct NativeCurveSample {
  std::vector<GA_Offset> vertices;
  std::vector<float> weights;
  std::vector<double> geometry_weights; // Rational position/control-velocity weights.
  Float3 position;
};
struct NativeCurveSampling {
  // One owned sampled path per valid native primitive. Invalid basis data
  // is diagnosed before calling the host evaluator.
  std::vector<std::vector<NativeCurveSample>> paths;
  std::string error;
};
inline NativeCurveSampling sample_native_curve(const GU_Detail &source, const GEO_Curve &curve, int divisions) {
  NativeCurveSampling result;
  const auto fail = [&](const std::string &reason) {
    result.paths.clear();
    result.error = reason;
    return result;
  };
  const auto *basis = curve.getBasis();
  if (!basis || curve.getVertexCount() < 2 || curve.getVertexCount() > std::numeric_limits<int>::max() ||
      basis->getLength() < 2 || basis->getOrder() < 2 || basis->getOrder() > GA_MAXORDER)
    return fail("Invalid curve basis/control-point count");
  const auto &knots = basis->getKnotVector();
  for (int n = 0; n < basis->getLength(); ++n)
    if (!std::isfinite(knots(n)) || (n && knots(n) < knots(n - 1)))
      return fail("Curve knots must be finite and nondecreasing");
  if (curve.isRational())
    for (exint n = 0; n < curve.getVertexCount(); ++n)
      if (!std::isfinite(curve.getWeight(unsigned(n))))
        return fail("Curve has a non-finite rational weight");
  if (!basis || !basis->checkValid(int(curve.getVertexCount()), curve.isClosed()))
    return fail("Invalid curve basis/control-point count");
  GA_KnotVector breaks;
  const auto count = basis->getBreakpoints(breaks, 0);
  if (count < 2 || divisions < 1 || divisions > 256)
    return fail("Curve has no usable knot spans or sampling resolution");
  constexpr std::size_t key_limit = 1048576;
  if (std::size_t(count - 1) * std::size_t(divisions) + std::size_t(count) > key_limit)
    return fail("Curve sampling exceeds 1048576 keys; reduce Segments per Span or simplify this curve");
  for (int n = 0; n < count; ++n)
    if (!std::isfinite(breaks(n)) || (n && breaks(n) <= breaks(n - 1)))
      return fail("Curve knot spans must be finite and increasing");
  UT_Array<GA_Offset> vertices;
  UT_Array<float> weights;
  const bool rational = curve.isRational();
  std::unordered_map<GA_Offset, float> control_weights;
  if (rational)
    for (exint n = 0; n < curve.getVertexCount(); ++n)
      control_weights.emplace(curve.getVertexOffset(n), curve.getWeight(unsigned(n)));
  result.paths.emplace_back();
  for (int span = 0; span + 1 < count; ++span) {
    const float begin = curve.realToUnitDomain(float(breaks(span))),
                end = curve.realToUnitDomain(float(breaks(span + 1)));
    if (!std::isfinite(begin) || !std::isfinite(end) || begin >= end)
      return fail("Curve knot span is not representable in the renderer parameter range");
    auto &path = result.paths.back();
    for (int step = path.empty() ? 0 : 1; step <= divisions; ++step) {
      const float parameter = step == divisions ? end : float(double(begin) + (double(end) - begin) * step / divisions);
      vertices.clear();
      weights.clear();
      curve.computeInteriorPointWeights(vertices, weights, parameter, 0, 0);
      if (vertices.isEmpty() || vertices.size() != weights.size())
        return fail("Curve evaluation returned no usable interpolation weights");
      NativeCurveSample sample;
      double sum = 0, position_sum = 0, x = 0, y = 0, z = 0;
      for (exint i = 0; i < weights.size(); ++i) {
        const auto weight = weights(i);
        if (!std::isfinite(weight))
          return fail("Curve interpolation contains non-finite weights");
        if (weight == 0)
          continue;
        const auto point = source.vertexPoint(vertices(i));
        const auto index = source.pointIndex(point);
        if (index < 0 || index >= source.getNumPoints())
          return fail("Curve interpolation references an unavailable control point");
        const auto p = source.getPos3(point);
        sum += weight;
        const double position_weight = double(weight) * (rational ? control_weights.at(vertices(i)) : 1);
        position_sum += position_weight;
        x += position_weight * p.x();
        y += position_weight * p.y();
        z += position_weight * p.z();
        sample.vertices.push_back(vertices(i));
        sample.weights.push_back(weight);
        if (rational)
          sample.geometry_weights.push_back(position_weight);
      }
      if (!std::isfinite(position_sum) || position_sum == 0)
        return fail("Curve evaluation has singular rational weights");
      sample.position = {float(x / position_sum), float(y / position_sum), float(z / position_sum)};
      for (auto &weight : sample.geometry_weights)
        weight /= position_sum;
      if (std::abs(sum - 1) > 1e-3 || !std::isfinite(sample.position.x) || !std::isfinite(sample.position.y) ||
          !std::isfinite(sample.position.z))
        return fail("Curve evaluation produced unusable positions or rational weights");
      path.push_back(std::move(sample));
    }
  }
  return result;
}
} // namespace cycles_houdini
