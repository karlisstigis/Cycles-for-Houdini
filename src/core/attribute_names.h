// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <string>

namespace cycles_houdini {
// Keep user fields disjoint from their engine-generated tangent/sign suffixes.
// The length makes names such as "map.tangent" distinct from map's tangent.
inline std::string engine_attribute_name(const std::string &name) {
  return "houdini:" + std::to_string(name.size()) + ":" + name;
}
} // namespace cycles_houdini
