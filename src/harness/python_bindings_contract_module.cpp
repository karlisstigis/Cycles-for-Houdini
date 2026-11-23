// SPDX-License-Identifier: Apache-2.0
// Standalone Python 3.13 probe: use real binding implementation without HDK or
// engine initialization. It deliberately owns only fixture model values.
#include "../houdini/python_bindings.h"
#include "../houdini/python_status.h"
#include "cycles_houdini/shader.h"
#include <stdexcept>
namespace cycles_houdini {
const char *state_name(SessionState) { return "idle"; }
Capabilities capabilities() {
  Capabilities result;
  result.image_iris = true;
  return result;
}
std::vector<std::string> surface_graph_definitions() { return {"surface"}; }
std::vector<std::string> volume_graph_definitions() { return {"volume"}; }
std::vector<std::string> world_graph_definitions() { return {"world"}; }
std::vector<std::string> light_graph_definitions() { return {"light"}; }
} // namespace cycles_houdini
namespace {
pybind11::dict status(pybind11::handle value) {
  const auto number = pybind11::module_::import("operator").attr("index")(value).cast<long>();
  if (number == -1)
    throw std::invalid_argument("native argument");
  if (number == -2)
    throw cycles_houdini::SceneUpdateRejected("candidate rejected");
  cycles_houdini::SessionStatus status;
  status.generation = (1ull << 63) + 17;
  status.progress = 0.375;
  status.message = "Unicode: \xe2\x98\x83";
  status.render_device_ids = {"CPU", "OPTIX"};
  status.source_diagnostics.push_back({});
  cycles_houdini::PythonStatusExtraction stats;
  stats.motion_warnings = {"warning"};
  return cycles_houdini::python_status_value(status, stats);
}
} // namespace
PYBIND11_MODULE(_cycles_houdini, module) {
  cycles_houdini::bind_python_status(module, status);
  module.def("legacy_failure", [] { throw std::invalid_argument("legacy argument"); });
}
