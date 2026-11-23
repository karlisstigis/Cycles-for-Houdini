// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/texture.h"
#include <string>

namespace cycles_houdini {
// Owned header observations. No borrowed IMG/PXL strings survive a host reload.
struct NativeTextureColorInfo {
  std::string filename_space, plane_space, native_space, guessed_space, heuristic_space;
  std::string storage, color_model, plane_name, texture_space;
  int planes{};
};
NativeTextureColorInfo native_texture_color_info(const std::string &filename, bool read_as_is = true,
                                                 bool observe_texture_cache = false);
TextureInputPolicy native_texture_input_policy();
} // namespace cycles_houdini
