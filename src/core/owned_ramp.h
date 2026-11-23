// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <array>
#include <span>
#include <stdexcept>
#include <vector>
namespace cycles_houdini {
// Owned numeric controls are baked on the host. No private kernel program.
inline constexpr int ramp_source_magic = 0x43525331;
class RampSourceError : public std::invalid_argument {
public:
  using std::invalid_argument::invalid_argument;
};
struct OwnedRampTable {
  std::array<std::array<float, 3>, 257> values;
  bool interpolate{true};
};
std::vector<std::array<float, 3>> sample_owned_ramp(std::span<const int> source, std::span<const float> factors,
                                                    bool *interpolate = nullptr);
OwnedRampTable prepare_owned_ramp(std::span<const int> source);
std::array<float, 3> evaluate_owned_ramp(const OwnedRampTable &table, float factor);
} // namespace cycles_houdini
