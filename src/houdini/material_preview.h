// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
#include <span>
#include <string_view>
class VOP_Node;
namespace cycles_houdini {
struct MaterialPreviewRequest {
  long long node{};
  std::uint64_t revision{};
  int size{};
};
struct MaterialPreviewStats {
  std::uint64_t queued{}, coalesced{}, evicted{}, published{}, stale{}, image_bytes{}, images{};
};
class MaterialThumbnailOwner;
using MaterialThumbnailLease = std::shared_ptr<MaterialThumbnailOwner>;
MaterialThumbnailLease install_material_thumbnail(VOP_Node &node);
void ensure_material_thumbnail(VOP_Node &node, const MaterialThumbnailLease &owner);
MaterialPreviewRequest request_material_thumbnail(long long node, int size);
std::vector<MaterialPreviewRequest> take_material_previews();
bool material_preview_current(const MaterialPreviewRequest &request);
bool publish_material_thumbnail(const MaterialPreviewRequest &request, std::span<const std::byte> rgba,
                                const std::string &error);
MaterialPreviewStats material_preview_stats();
std::vector<std::byte> material_thumbnail_pixels(long long node);
SceneUpdate material_preview_fixture(std::string_view domain = "surface", bool world_medium = false);
} // namespace cycles_houdini
