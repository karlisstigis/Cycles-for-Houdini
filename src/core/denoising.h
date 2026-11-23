// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
#include "device/denoise.h"
#include "device/device.h"
#include "util/array.h"
#include "util/transform.h"

namespace cycles_houdini {
// Cycles advances previous[0] after a viewport evaluation. Keep that accepted
// history while replacing the current sample at the serialized scene boundary.
inline ccl::array<ccl::Transform> denoising_motion_history(const ccl::array<ccl::Transform> &previous,
                                                           const ccl::Transform &current) {
  ccl::array<ccl::Transform> result;
  result.resize(2);
  result[0] = previous.empty() ? current : previous[0];
  result[1] = current;
  return result;
}
struct DenoisePolicy {
  ccl::DenoiseParams parameters;
  std::string backend{"none"}, device_id, warning;
};
std::vector<std::string> available_denoisers(const ccl::DeviceInfo &device);
DenoisePolicy resolve_denoising(const DenoiseSettings &settings, const ccl::DeviceInfo &device);
} // namespace cycles_houdini
