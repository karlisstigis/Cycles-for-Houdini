// SPDX-License-Identifier: Apache-2.0
// Standalone: cl /std:c++20 /EHsc /I include tests/gaussian_splats_math_test.cpp
#include "../src/core/gaussian_splats.h"
#include <iostream>
#include <limits>
using namespace cycles_houdini;
namespace {
void require(bool value, const char *reason) {
  if (!value)
    throw std::runtime_error(reason);
}
double evaluate(const std::array<Float3, 15> &coefficients, std::array<double, 3> direction, int channel) {
  const auto basis = splat_sh_basis(direction);
  double result = 0;
  for (int i = 0; i < 15; ++i) {
    const auto v = coefficients[i];
    result += basis[i] * (channel == 0 ? v.x : channel == 1 ? v.y : v.z);
  }
  return result;
}
} // namespace
int main() {
  require(normalized_splat_quaternion({0, 0, 0, 2}) == SplatQuaternion{0, 0, 0, 1}, "Quaternion normalization");
  for (const auto q : {SplatQuaternion{0, 0, 0, 0}, SplatQuaternion{std::numeric_limits<float>::infinity(), 0, 0, 1}}) {
    bool rejected = false;
    try {
      normalized_splat_quaternion(q);
    } catch (const std::invalid_argument &) {
      rejected = true;
    }
    require(rejected, "Invalid quaternion accepted");
  }
  const auto turn = normalized_splat_quaternion({0, 0, 1, 1});
  const auto turned = splat_rotate(turn, {1, 0, 0});
  require(std::abs(turned[0]) < 1e-6 && std::abs(turned[1] - 1) < 1e-6, "xyzw rotation convention");
  std::array<Float3, 15> values;
  for (int i = 0; i < 15; ++i)
    values[i] = {float(std::sin(i * .7) * .2), float(std::cos(i * .4) * .3), float(std::sin(i * .3) * .15)};
  require(rotate_splat_harmonics(values, {0, 0, 0, 1}) == values, "Identity changed coefficients");
  double error = 0;
  for (const auto q : {turn, normalized_splat_quaternion({.2f, -.7f, .5f, .3f})}) {
    const auto rotated = rotate_splat_harmonics(values, q);
    const auto recovered = rotate_splat_harmonics(rotated, splat_quaternion_inverse(q));
    for (int i = 0; i < 15; ++i) {
      require(std::abs(recovered[i].x - values[i].x) < 1e-6, "SH inverse rotation");
    }
    for (int i = 0; i < 47; ++i) {
      const double z = -1 + 2 * (i + .5) / 47, angle = i * 2.399963229728653;
      const std::array<double, 3> d = {std::sqrt(1 - z * z) * std::cos(angle), std::sqrt(1 - z * z) * std::sin(angle),
                                       z};
      for (int c = 0; c < 3; ++c)
        error = std::max(error, std::abs(evaluate(rotated, d, c) -
                                         evaluate(values, splat_rotate(splat_quaternion_inverse(q), d), c)));
    }
  }
  require(error < 1e-6, "Rotated SH did not preserve view-dependent radiance");
  // Rest and current equal must cancel, even when neither is identity.
  const auto rest = normalized_splat_quaternion({.4f, .2f, -.1f, .7f});
  const auto delta = normalized_splat_quaternion(splat_quaternion_product(rest, splat_quaternion_inverse(rest)));
  require(rotate_splat_harmonics(values, delta) == values, "restorient cancellation");
  SplatHarmonicRotation cache;
  cache(values, {0, 0, 0, 1});
  require(cache.matrix_builds() == 0, "Identity rotation performed projection work");
  for (int i = 0; i < 10000; ++i)
    cache(values, turn);
  require(cache.matrix_builds() == 1, "Repeated Transform SOP rotation rebuilt its SH matrix per point");
  std::cout << "{\"result\":\"passed\",\"directions\":94,\"bands\":3,\"maximum_radiance_error\":" << error << "}\n";
}
