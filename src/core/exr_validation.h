// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <OpenImageIO/imageio.h>
#include <functional>
#include <string>
#include <vector>

namespace cycles_houdini {
// The header callback matches the plan and decides whether to validate pixels.
// Both checks use the same open file.
void inspect_existing_exr_file(const std::string &filename, int expected_parts,
                               const std::function<bool(const std::vector<OIIO::ImageSpec> &)> &validate,
                               const std::function<void()> &checkpoint);
} // namespace cycles_houdini
