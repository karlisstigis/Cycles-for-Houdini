// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/texture.h"
#include <OpenColorIO/OpenColorIO.h>
#include <cstdint>
#include <string>
#include <vector>
#include <stdexcept>
namespace cycles_houdini {
class TextureSourceError : public std::invalid_argument {
public:
  using std::invalid_argument::invalid_argument;
};
struct TextureFileStamp {
  std::string path;
  std::uintmax_t size{};
  std::int64_t modified{};
  bool operator==(const TextureFileStamp &) const = default;
};
struct TextureAsset {
  std::shared_ptr<const TextureImageData> memory_image;
  // Portable encodings missing from the host config are converted by the
  // glue loader. The engine receives working-linear pixels, never an overlay.
  OCIO_NAMESPACE::ConstCPUProcessorRcPtr input_processor;
  std::string shader_node;
  double image_time{};
  std::string failure_revision;
  std::string filename, revision, cache_namespace, color_configuration;
  std::string colorspace, display_colorspace, selection, input_policy;
  bool auto_input{};
  std::uint64_t decoded_pixels{};
  std::vector<int> tiles;
  std::vector<TextureFileStamp> files;
  std::string discovery_directory;
  std::int64_t directory_modified{};
};
// File metadata is inspected on warm updates; image headers are opened only
// when preparing a changed graph. Cycles retains pixel/cache ownership.
TextureAsset prepare_texture_asset(const std::string &filename, const std::string &colorspace,
                                   const std::vector<int> &requested_tiles, const TextureInputPolicy &policy,
                                   double image_time = 0.);
bool texture_asset_changed(const TextureAsset &asset, const TextureInputPolicy &policy);
std::string texture_watch_path(const std::string &filename);
std::string texture_watch_revision(const std::vector<std::string> &filenames, const std::string &input_policy = {});
} // namespace cycles_houdini
