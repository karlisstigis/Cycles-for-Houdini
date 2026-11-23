// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
#include <algorithm>
#include <cmath>

namespace cycles_houdini {
inline double transform_determinant(const Matrix &m) {
  return double(m[0]) * (double(m[5]) * m[10] - double(m[6]) * m[9]) -
         double(m[1]) * (double(m[4]) * m[10] - double(m[6]) * m[8]) +
         double(m[2]) * (double(m[4]) * m[9] - double(m[5]) * m[8]);
}
inline bool finite_transform(const Matrix &m) {
  return std::all_of(m.begin(), m.end(), [](float v) { return std::isfinite(v); });
}
// A determinant measures volume, not scale-independent invertibility. An
// absolute epsilon incorrectly rejects ordinary tiny instances (s^3).
// Evaluate in double so finite float scales are not lost to float underflow.
inline bool collapsed_transform(const Matrix &m) { return finite_transform(m) && transform_determinant(m) == 0; }
inline bool invertible_transform(const Matrix &m) { return finite_transform(m) && transform_determinant(m) != 0; }
} // namespace cycles_houdini
