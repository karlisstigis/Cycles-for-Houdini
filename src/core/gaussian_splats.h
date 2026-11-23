// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
#include <algorithm>
#include <cmath>
#include <numbers>
#include <stdexcept>

namespace cycles_houdini {
using SplatQuaternion = std::array<float, 4>; // xyzw
inline SplatQuaternion normalized_splat_quaternion(SplatQuaternion q) {
  double n = 0;
  for (float v : q) {
    if (!std::isfinite(v)) {
      throw std::invalid_argument("Nonfinite Gaussian quaternion");
    }
    n += double(v) * v;
  }
  if (n < 1e-30) {
    throw std::invalid_argument("Gaussian quaternion has zero length");
  }
  const auto scale = 1 / std::sqrt(n);
  for (auto &v : q) {
    v = float(v * scale);
  }
  return q;
}
inline SplatQuaternion splat_quaternion_product(const SplatQuaternion &a, const SplatQuaternion &b) {
  return {a[3] * b[0] + a[0] * b[3] + a[1] * b[2] - a[2] * b[1], a[3] * b[1] - a[0] * b[2] + a[1] * b[3] + a[2] * b[0],
          a[3] * b[2] + a[0] * b[1] - a[1] * b[0] + a[2] * b[3], a[3] * b[3] - a[0] * b[0] - a[1] * b[1] - a[2] * b[2]};
}
inline SplatQuaternion splat_quaternion_inverse(SplatQuaternion q) {
  q[0] = -q[0];
  q[1] = -q[1];
  q[2] = -q[2];
  return q;
}
inline std::array<double, 3> splat_rotate(const SplatQuaternion &q, std::array<double, 3> v) {
  const std::array<double, 3> t = {2 * (q[1] * v[2] - q[2] * v[1]), 2 * (q[2] * v[0] - q[0] * v[2]),
                                   2 * (q[0] * v[1] - q[1] * v[0])};
  return {v[0] + q[3] * t[0] + q[1] * t[2] - q[2] * t[1], v[1] + q[3] * t[1] + q[2] * t[0] - q[0] * t[2],
          v[2] + q[3] * t[2] + q[0] * t[1] - q[1] * t[0]};
}
// Real SH, in the exact coefficient order and signs used by the upstream
// Gaussian renderer. Keep unquantized values until the engine upload.
inline std::array<double, 15> splat_sh_basis(const std::array<double, 3> &v) {
  const auto x = v[0], y = v[1], z = v[2];
  return {-.488602512 * y,
          .488602512 * z,
          -.488602512 * x,
          1.092548431 * x * y,
          -1.092548431 * y * z,
          .315391565 * (3 * z * z - 1),
          -1.092548431 * x * z,
          .546274215 * (x * x - y * y),
          -.590043589 * y * (3 * x * x - y * y),
          2.890611442 * y * x * z,
          -.457045799 * y * (5 * z * z - 1),
          .373176332 * z * (5 * z * z - 3),
          -.457045799 * x * (5 * z * z - 1),
          1.445305721 * z * (x * x - y * y),
          -.5900435899 * x * (x * x - 3 * y * y)};
}
// Bounded per-capture cache: fixed quadrature is constructed once, and a single
// last-used rotation matrix is retained. A common Transform SOP on millions of
// points builds the projection once, then performs only band-local multiplies.
class SplatHarmonicRotation {
  struct Quadrature {
    std::array<double, 3> direction;
    std::array<double, 15> basis;
    double weight;
  };
  static const std::array<Quadrature, 32> &quadrature() {
    static const auto values = []() {
      constexpr double z[] = {-.8611363115940526, -.3399810435848563, .3399810435848563, .8611363115940526};
      constexpr double w[] = {.3478548451374538, .6521451548625461, .6521451548625461, .3478548451374538};
      std::array<Quadrature, 32> result;
      for (int i = 0; i < 4; ++i) {
        for (int j = 0; j < 8; ++j) {
          const auto angle = 2 * std::numbers::pi * j / 8, r = std::sqrt(1 - z[i] * z[i]);
          const std::array<double, 3> d = {r * std::cos(angle), r * std::sin(angle), z[i]};
          result[i * 8 + j] = {d, splat_sh_basis(d), w[i] * 2 * std::numbers::pi / 8};
        }
      }
      return result;
    }();
    return values;
  }
  std::optional<SplatQuaternion> rotation_;
  std::array<std::array<double, 15>, 15> matrix_{};
  std::uint64_t builds_{};

public:
  std::uint64_t matrix_builds() const { return builds_; }
  std::array<Float3, 15> operator()(const std::array<Float3, 15> &source, SplatQuaternion rotation) {
    if (std::abs(rotation[0]) + std::abs(rotation[1]) + std::abs(rotation[2]) < 1e-7) {
      return source;
    }
    if (rotation[3] < 0) {
      for (auto &v : rotation) {
        v = -v;
      }
    }
    bool reuse = rotation_.has_value();
    // Equivalent delta quaternions differ slightly after orient/restorient
    // cancellation. Bound reuse error to far below upstream SH quantization.
    if (reuse) {
      for (int i = 0; i < 4; ++i) {
        reuse &= std::abs(rotation[i] - (*rotation_)[i]) < 1e-6;
      }
    }
    if (!reuse) {
      rotation_ = rotation;
      matrix_ = {};
      ++builds_;
      const auto inverse = splat_quaternion_inverse(rotation);
      // Products of degree <=3 SH integrate exactly with this rule.
      // Each band is rotated independently, preserving DC and band energy.
      for (const auto &sample : quadrature()) {
        const auto basis = splat_sh_basis(splat_rotate(inverse, sample.direction));
        for (int band = 1; band <= 3; ++band) {
          const int begin = band * band - 1, end = (band + 1) * (band + 1) - 1;
          for (int to = begin; to < end; ++to) {
            for (int from = begin; from < end; ++from) {
              matrix_[to][from] += sample.weight * sample.basis[to] * basis[from];
            }
          }
        }
      }
    }
    std::array<Float3, 15> result;
    for (int band = 1; band <= 3; ++band) {
      const int begin = band * band - 1, end = (band + 1) * (band + 1) - 1;
      for (int to = begin; to < end; ++to) {
        double x = 0, y = 0, z = 0;
        for (int from = begin; from < end; ++from) {
          const auto factor = matrix_[to][from];
          x += factor * source[from].x;
          y += factor * source[from].y;
          z += factor * source[from].z;
        }
        result[to] = {float(x), float(y), float(z)};
      }
    }
    return result;
  }
};
inline std::array<Float3, 15> rotate_splat_harmonics(const std::array<Float3, 15> &source,
                                                     const SplatQuaternion &rotation) {
  return SplatHarmonicRotation{}(source, rotation);
}
} // namespace cycles_houdini
