// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <cstdint>
#include <memory>
#include <string>
#include <vector>
#include <map>
#include <stdexcept>
#include "cycles_houdini/texture.h"

class PXL_Raster;
namespace cycles_houdini {
struct NativeCopSource {
  int node_id{-1};
  bool pinned_frame{};
  std::vector<std::int64_t> dependencies;
  std::vector<int> node_ids;
};
class CopImageSourceError : public std::runtime_error {
public:
  using std::runtime_error::runtime_error;
};
// Metadata only: includes uncooked wired inputs and project/subnet settings.
NativeCopSource cop_image_source(const std::string &source);
// Fully owned raster, acquired in Houdini's supported host context. Pixel
// conversion reads this private copy and must not resolve or cook an OP.
struct NativeCopImage {
  std::string source, colorspace, storage;
  int node_id{-1}, width{}, height{}, channels{}, rotation{};
  bool time_dependent{};
  std::size_t source_version{};
  std::vector<std::int64_t> dependencies;
  double requested_time{}, source_time{};
  std::shared_ptr<const PXL_Raster> raster;
};
std::shared_ptr<const NativeCopImage> freeze_cop_image(const std::string &source, double time);
std::vector<float> cop_image_pixels(const NativeCopImage &image);
struct NativeCopBatch {
  struct Entry {
    std::vector<std::int64_t> signature;
    std::shared_ptr<const NativeCopImage> raster;
    std::shared_ptr<const TextureImageData> image;
    std::string error;
    bool time_dependent{};
  };
  std::map<TextureImageKey, Entry> entries;
  TextureImageTable images;
  std::uint64_t acquisitions{}, cache_hits{};
  double acquisition_seconds{}, conversion_seconds{};
  bool pending{};
};
class NativeCopImageResolver {
public:
  std::shared_ptr<NativeCopBatch> acquire(const std::vector<TextureImageKey> &requests);
  static void convert(NativeCopBatch &batch); // Private owned rasters only; safe off host thread.
  void commit(std::shared_ptr<const NativeCopBatch> batch);
  void clear() { cache_.reset(); }
  std::shared_ptr<const NativeCopBatch> latest() const { return cache_; }
  std::uint64_t acquisitions{}, cache_hits{};
  double acquisition_seconds{}, conversion_seconds{};

private:
  std::shared_ptr<const NativeCopBatch> cache_;
};
} // namespace cycles_houdini
