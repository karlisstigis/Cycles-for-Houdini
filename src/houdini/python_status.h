// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <pybind11/pybind11.h>
#include "cycles_houdini/session.h"
#include <map>
#include <string>

namespace cycles_houdini {
// Only adapter metadata exposed by status(). No HDK or live adapter ownership.
struct PythonStatusExtraction {
  double cooking_seconds{}, conversion_seconds{};
  std::uint64_t extractions{}, cache_hits{}, packed_instances{}, index_instance_ids{};
  std::uint64_t material_motion_cooks{}, material_motion_samples{}, material_motion_cache_hits{};
  std::vector<std::string> motion_warnings;
  // Diagnostic resource -> "<object path>: " for notes that name their object; the
  // DSO resolves the nodes before the call.
  std::map<std::string, std::string> resource_objects;
};
// Called with the GIL held. Each call returns independently owned Python values.
pybind11::dict python_status_value(const SessionStatus &, const PythonStatusExtraction & = {});
pybind11::dict python_capabilities_value(const Capabilities &);
} // namespace cycles_houdini
