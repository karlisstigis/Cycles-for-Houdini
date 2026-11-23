// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
#include "device/device.h"

namespace cycles_houdini {
std::string physical_device_id(const ccl::DeviceInfo &device);
ccl::DeviceInfo resolve_render_devices(const SessionConfig &config, std::vector<std::string> &ids);
} // namespace cycles_houdini
