// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <array>
#include <span>
#include <stdexcept>
#include <vector>

namespace cycles_houdini {
inline constexpr int shader_curve_magic = 0x43435231;
enum class ShaderCurveKind { Float = 0, RGB = 1, Vector = 2 };
class ShaderCurveSourceError : public std::invalid_argument {
public:
  using std::invalid_argument::invalid_argument;
};
struct ShaderCurveTable {
  float minimum{}, maximum{1};
  bool extrapolate{true};
  std::array<std::array<float, 3>, 257> values;
};
// Owned numeric controls only. No OP nodes, host evaluation, Blender runtime,
// native pointers or callbacks enter the worker preparation path.
ShaderCurveTable prepare_shader_curve(std::span<const int> source, ShaderCurveKind kind);
} // namespace cycles_houdini
