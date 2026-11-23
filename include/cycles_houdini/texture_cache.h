// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/texture.h"

namespace cycles_houdini {
struct OwnedSceneSnapshot;
struct TextureCachePayload;
// A plan owns its renderer policy. Callers can display paths but cannot forge
// native texture/color/alpha metadata or substitute an arbitrary output path.
struct TextureCachePlanItem {
  std::string source, destination;
  std::shared_ptr<const TextureCachePayload> payload;
};
std::vector<TextureCachePlanItem> texture_cache_plan(const OwnedSceneSnapshot &snapshot);
void generate_texture_cache(const TextureCachePlanItem &item, const TextureInputPolicy &policy);
} // namespace cycles_houdini
