// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <pybind11/pybind11.h>

namespace cycles_houdini {
void bind_python_status(pybind11::module_ &module, pybind11::dict (*read_status)(pybind11::handle));
} // namespace cycles_houdini
