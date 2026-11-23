// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <array>
#include <functional>
#include <optional>
#include <memory>
#include <map>
#include <compare>
#include <cstdint>
#include <string>
#include <vector>

namespace cycles_houdini {
struct TextureHeader {
  std::string filename, file_colorspace, color_interop_id, format;
  std::optional<std::array<float, 8>> chromaticities;
  bool aces_image_container{};
  bool operator==(const TextureHeader &) const = default;
};
struct TextureInputSelection {
  std::string colorspace, reason, label;
  bool operator==(const TextureInputSelection &) const = default;
};
struct TextureImageData {
  std::string source, revision, colorspace, error;
  int width{}, height{}, channels{};
  double evaluation_time{};
  bool unassociated_alpha{};
  std::vector<float> pixels; // Bottom scanline first, native scene/data values.
};
struct TextureImageKey {
  std::string source;
  double evaluation_time{};
  auto operator<=>(const TextureImageKey &) const = default;
};
using TextureImageTable = std::map<TextureImageKey, std::shared_ptr<const TextureImageData>>;
void validate_texture_image(const TextureImageData &image);
std::string texture_image_revision(const TextureImageData &image);
struct TextureInputPolicy {
  // Called only during changed-graph preparation on the applying host thread.
  // The engine owns image pixels; host policy consumes owned header metadata.
  std::function<TextureInputSelection(const TextureHeader &)> resolve_auto;
  std::function<std::string()> identity;
  // This callback returns owned immutable data, never borrowed host pixels.
  // Worker providers must only consult their captured resource ledger.
  std::function<std::shared_ptr<const TextureImageData>(const std::string &, double)> resolve_image;
};
struct TextureInputInfo {
  std::string material_key, filename, colorspace, selection, policy_id, revision;
  std::vector<int> tiles;
  // Nominal full-resolution float4 decode cost, summed across portable tiles.
  // This excludes scratch/mips and is not a live allocation measurement.
  std::uint64_t portable_decode_bytes{};
  std::string portable_cache_format;
};
} // namespace cycles_houdini
