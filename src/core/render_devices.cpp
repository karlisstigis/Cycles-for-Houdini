// SPDX-License-Identifier: Apache-2.0
#include "render_devices.h"
#include <algorithm>
#include <set>
#include <stdexcept>

namespace cycles_houdini {
std::string physical_device_id(const ccl::DeviceInfo &device) {
  if (device.type == ccl::DEVICE_CUDA || device.type == ccl::DEVICE_OPTIX) {
    // The pinned CUDA enumerator appends domain:bus:device. OptiX uses the
    // same ID with an _OptiX suffix. Use the PCI identity, not the label or
    // an enumeration index, to retain choices when changing backends.
    auto id = device.id;
    if (device.type == ccl::DEVICE_OPTIX && id.ends_with("_OptiX"))
      id.resize(id.size() - 6);
    const auto separator = id.rfind('_');
    if (separator != std::string::npos && id.substr(separator + 1).find(':') != std::string::npos)
      return "NVIDIA:" + id.substr(separator + 1);
    return id;
  }
  return device.id;
}

ccl::DeviceInfo resolve_render_devices(const SessionConfig &config, std::vector<std::string> &ids) {
  if (!config.device_id.empty() && !config.device_ids.empty())
    throw std::invalid_argument("Specify device_id or device_ids, not both");
  ids = config.device_ids;
  if (ids.empty() && !config.device_id.empty())
    ids.push_back(config.device_id);
  if (ids.empty())
    throw std::invalid_argument("Select at least one render device");
  std::set<std::string> unique, physical;
  ccl::vector<ccl::DeviceInfo> selected;
  const auto available = ccl::Device::available_devices();
  for (const auto &id : ids) {
    if (id.empty() || !unique.insert(id).second)
      throw std::invalid_argument("Invalid or duplicate render device: " + id);
    const auto found = std::find_if(available.begin(), available.end(), [&](const auto &d) { return d.id == id; });
    if (found == available.end())
      throw std::invalid_argument("Requested device is unavailable: " + id);
    if (!physical.insert(physical_device_id(*found)).second)
      throw std::invalid_argument("The same physical GPU cannot render through two backends at once: " + id);
    if (!selected.empty() && selected.front().type != found->type)
      throw std::invalid_argument("Selected render devices must use one backend; CPU/GPU mixing is not supported");
    selected.push_back(*found);
  }
  // Cycles' native MultiDevice shares one scene/session and schedules work
  // across the selected GPUs. It can silently omit CPUs in interactive mixed
  // mode; rejecting mixed selections above prevents that implicit change.
  return ccl::Device::get_multi_device(selected, config.threads, !config.interactive);
}
} // namespace cycles_houdini
