// SPDX-License-Identifier: Apache-2.0
#include "python_bindings.h"
#include "python_status.h"

namespace py = pybind11;
namespace cycles_houdini {
void bind_python_status(py::module_ &module, py::dict (*read_status)(py::handle)) {
  // The host API has always reported native validation and lifecycle failures
  // as RuntimeError. Keep exceptions raised by Python input objects intact.
  py::register_local_exception_translator([](std::exception_ptr error) {
    if (!error)
      return;
    try {
      std::rethrow_exception(error);
    } catch (py::error_already_set &python_error) {
      python_error.restore();
    } catch (const py::builtin_exception &python_error) {
      python_error.set_error();
    } catch (const std::exception &native_error) {
      py::set_error(PyExc_RuntimeError, native_error.what());
    }
  });
  // Local translators run newest first. A rejected candidate carries a distinct
  // exception type so IPR can keep the previously accepted scene rendering.
  py::register_local_exception<SceneUpdateRejected>(module, "SceneUpdateRejected", PyExc_RuntimeError);
  module.def("capabilities", [] { return python_capabilities_value(capabilities()); });
  module.def("status", read_status);
}
} // namespace cycles_houdini
