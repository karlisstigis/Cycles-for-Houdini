// SPDX-License-Identifier: Apache-2.0
#include "cycles_houdini/memory.h"
#include "../platform/platform.h"
namespace cycles_houdini {
std::string process_memory_json() { return platform::process_memory_json(); }
} // namespace cycles_houdini
