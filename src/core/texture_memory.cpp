// SPDX-License-Identifier: Apache-2.0
#include "texture_memory.h"
#include "scene/image.h"
#include "scene/image_loader.h"
#include "scene/image_oiio.h"
#include "util/image_metadata.h"
#include "staged_output.h"
#include "color_internal.h"
#include "cycles_houdini/texture_cache.h"
#include "cycles_houdini/snapshot.h"
#include <OpenImageIO/imagebuf.h>
#include <OpenImageIO/imagebufalgo.h>
#include "scene/scene.h"
#include "scene/shader_nodes.h"
#include "util/md5.h"
#include "util/image_maketx.h"
#include <algorithm>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <regex>
#include <stdexcept>
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <Windows.h>
#endif

namespace cycles_houdini {
struct TextureCachePayload {
  TextureAsset asset;
  std::string source, destination;
  int alpha_type{};
};
void validate_texture_image(const TextureImageData &image) {
  for (const auto *text : {&image.source, &image.colorspace, &image.error})
    if (text->size() > 65536 || text->find('\0') != std::string::npos)
      throw std::invalid_argument("Owned image text metadata is invalid");
  if (!image.source.starts_with("op:") || image.source.find('\0') != std::string::npos ||
      !std::isfinite(image.evaluation_time))
    throw std::invalid_argument("Owned image has an invalid source or evaluation time");
  if (image.revision.size() != 32 || !std::all_of(image.revision.begin(), image.revision.end(), [](char c) {
        return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
      }))
    throw std::invalid_argument("Owned image has an invalid revision");
  if (!image.error.empty()) {
    if (image.width || image.height || image.channels || !image.pixels.empty())
      throw std::invalid_argument("Failed image carries an unexpected pixel payload");
    return;
  }
  if (image.width < 1 || image.height < 1 || image.channels < 1 || image.channels > 4)
    throw std::invalid_argument("Owned image dimensions/channels are invalid");
  const auto count = std::uint64_t(image.width) * image.height * image.channels;
  if (count > std::uint64_t(2) * 1024 * 1024 * 1024 / sizeof(float) || count != image.pixels.size())
    throw std::invalid_argument("Owned image pixel layout exceeds its allocation contract");
  if (!std::all_of(image.pixels.begin(), image.pixels.end(), [](float value) { return std::isfinite(value); }))
    throw std::invalid_argument("Owned image contains non-finite pixels");
}
std::string texture_image_revision(const TextureImageData &image) {
  ccl::MD5Hash hash;
  const auto text = [&](const std::string &value) {
    const std::uint64_t size = value.size();
    hash.append(reinterpret_cast<const std::uint8_t *>(&size), sizeof(size));
    for (std::size_t offset = 0; offset < value.size();) {
      const auto chunk = std::min<std::size_t>(value.size() - offset, 64 * 1024 * 1024);
      hash.append(reinterpret_cast<const std::uint8_t *>(value.data() + offset), int(chunk));
      offset += chunk;
    }
  };
  text(image.source);
  text(image.colorspace);
  text(image.error);
  for (const auto value : {image.width, image.height, image.channels})
    hash.append(reinterpret_cast<const std::uint8_t *>(&value), sizeof(value));
  // Time identifies a capture request, not different pixel contents. Static
  // images acquired at multiple shutter times can share one engine image.
  hash.append(image.unassociated_alpha ? "straight" : "associated");
  const auto *bytes = reinterpret_cast<const std::uint8_t *>(image.pixels.data());
  for (std::size_t offset = 0, size = image.pixels.size() * sizeof(float); offset < size;) {
    const auto chunk = std::min<std::size_t>(size - offset, 64 * 1024 * 1024);
    hash.append(bytes + offset, int(chunk));
    offset += chunk;
  }
  return hash.get_hex();
}
namespace {
std::string path_text(const std::filesystem::path &path) {
  const auto text = path.generic_u8string();
  return {text.begin(), text.end()};
}
bool safe_cache_path(const std::string &source, const std::string &destination) {
  namespace fs = std::filesystem;
  if (source.empty() || destination.empty())
    return false;
  const auto input = fs::absolute(fs::u8path(source)).lexically_normal();
  const auto output = fs::absolute(fs::u8path(destination)).lexically_normal();
  if (output == input || output.parent_path().parent_path() != input.parent_path() / "cycles_houdini" ||
      !std::regex_match(path_text(output.parent_path().filename()), std::regex("[0-9a-fA-F]{32}")))
    return false;
  // Reject links in any existing ancestor. Python also rejects Windows reparse
  // points before explicit actions; automatic rendering retains source fallback.
  for (auto path = output; !path.empty();) {
    std::error_code error;
    if (fs::is_symlink(fs::symlink_status(path, error)))
      return false;
#ifdef _WIN32
    const auto attributes = GetFileAttributesW(path.c_str());
    if (attributes != INVALID_FILE_ATTRIBUTES && (attributes & FILE_ATTRIBUTE_REPARSE_POINT))
      return false;
#endif
    const auto parent = path.parent_path();
    if (parent == path)
      break;
    path = parent;
  }
  return true;
}
void record_generated_cache(const std::string &source, const std::string &destination) {
  namespace fs = std::filesystem;
  if (!safe_cache_path(source, destination))
    return;
  try {
    const auto path = fs::u8path(destination);
    if (!fs::is_regular_file(path) || fs::hard_link_count(path) != 1)
      return;
    ccl::MD5Hash source_hash, contents;
    source_hash.append(source);
    std::ifstream input(path, std::ios::binary);
    std::array<char, 64 * 1024> buffer;
    while (input) {
      input.read(buffer.data(), buffer.size());
      contents.append(reinterpret_cast<const std::uint8_t *>(buffer.data()), int(input.gcount()));
    }
    if (!input.eof())
      return;
    StagedOutput staged(destination + ".ch-cache");
    std::ofstream marker(fs::u8path(staged.temporary_filename()), std::ios::binary);
    marker << "cycles_houdini.generated_texture/1\n"
           << source_hash.get_hex() << '\n'
           << contents.get_hex() << '\n'
           << fs::file_size(path) << '\n';
    marker.close();
    if (marker)
      staged.commit();
  } catch (const std::exception &) {
    // Optional bookkeeping failure must never break an otherwise valid render.
  }
}
class CacheImageLoader final : public ccl::OIIOImageLoader {
public:
  explicit CacheImageLoader(const std::string &path, int tile = 0) : ccl::OIIOImageLoader(path), tile_(tile) {}
  int get_tile_number() const override { return tile_; }
  bool load_metadata(ccl::ImageMetaData &metadata, const ccl::ImageLoaderParams &params,
                     ccl::Progress &progress) override {
    std::string destination;
    ccl::ImageMetaData probe;
    bool existed = true;
    if (params.use_texture_cache && params.auto_texture_cache) {
      ccl::resolve_tx(original_filepath_, params.texture_cache_path, params.colorspace, params.alpha_type,
                      ccl::IMAGE_FORMAT_PLAIN, destination, probe, false);
      existed = destination.empty() || std::filesystem::exists(std::filesystem::u8path(destination));
      if (!destination.empty() && !safe_cache_path(original_filepath_, destination)) {
        // Source .tx and tiled EXR/TIFF may already be directly usable.
        if (destination != original_filepath_) {
          auto fallback = params;
          fallback.use_texture_cache = false;
          return ccl::OIIOImageLoader::load_metadata(metadata, fallback, progress);
        }
      }
    }
    const bool loaded = ccl::OIIOImageLoader::load_metadata(metadata, params, progress);
    if (loaded && !existed && metadata.is_tx_file && get_filepath() == destination)
      record_generated_cache(original_filepath_, destination);
    return loaded;
  }

private:
  int tile_{};
};
class OwnedImageLoader final : public ccl::ImageLoader {
public:
  explicit OwnedImageLoader(std::shared_ptr<const TextureImageData> image,
                            OCIO_NAMESPACE::ConstCPUProcessorRcPtr processor = {})
      : image_(std::move(image)), processor_(std::move(processor)) {}
  bool load_metadata(ccl::ImageMetaData &metadata, const ccl::ImageLoaderParams &, ccl::Progress &) override {
    metadata.width = image_->width;
    metadata.height = image_->height;
    metadata.channels = image_->channels;
    metadata.type = image_->channels == 1 && !processor_ ? ccl::IMAGE_DATA_TYPE_FLOAT : ccl::IMAGE_DATA_TYPE_FLOAT4;
    metadata.is_compressible_as_srgb = false;
    metadata.is_unassociated_alpha = image_->unassociated_alpha;
    return true;
  }
  bool load_pixels(const ccl::ImageMetaData &metadata, void *target) override {
    if (metadata.width != image_->width || metadata.height != image_->height || metadata.channels != image_->channels)
      throw std::logic_error("Owned image metadata changed during engine loading");
    std::memcpy(target, image_->pixels.data(), image_->pixels.size() * sizeof(float));
    metadata.conform_pixels(target);
    convert(metadata, target, processor_);
    return true;
  }
  ccl::string name() const override { return image_->source; }
  bool equals(const ccl::ImageLoader &other) const override {
    const auto *owned = dynamic_cast<const OwnedImageLoader *>(&other);
    return owned && image_->source == owned->image_->source && image_->revision == owned->image_->revision &&
           processor_ == owned->processor_;
  }

  static void convert(const ccl::ImageMetaData &metadata, void *target,
                      const OCIO_NAMESPACE::ConstCPUProcessorRcPtr &processor) {
    if (!processor)
      return;
    auto *pixels = static_cast<float *>(target);
    const bool rgba = metadata.is_rgba();
    const bool associated = rgba && !metadata.ignore_alpha && !metadata.is_channel_packed;
    // RGB-only blocks let OCIO vectorize without ever transforming coverage.
    // Bounded scratch also avoids another full decoded image allocation.
    constexpr std::int64_t block_size = 4096;
    std::array<float, block_size * 3> rgb;
    const auto count = metadata.width * metadata.height;
    for (std::int64_t offset = 0; offset < count; offset += block_size) {
      const auto block = std::min(block_size, count - offset);
      for (std::int64_t i = 0; i < block; ++i) {
        const auto *pixel = pixels + (offset + i) * (rgba ? 4 : 1);
        // Match stock conversion: zero/negative coverage may carry additive RGB.
        const float alpha = associated && pixel[3] > 0.f ? pixel[3] : 1.f;
        for (int c = 0; c < 3; ++c)
          rgb[i * 3 + c] = pixel[rgba ? c : 0] / alpha;
      }
      OCIO_NAMESPACE::PackedImageDesc image(rgb.data(), long(block), 1, 3);
      processor->apply(image);
      for (std::int64_t i = 0; i < block; ++i) {
        auto *pixel = pixels + (offset + i) * (rgba ? 4 : 1);
        const float alpha = associated && pixel[3] > 0.f ? pixel[3] : 1.f;
        for (int c = 0; c < (rgba ? 3 : 1); ++c)
          pixel[c] = std::isfinite(rgb[i * 3 + c]) ? rgb[i * 3 + c] * alpha : 0.f;
      }
    }
  }

private:
  std::shared_ptr<const TextureImageData> image_;
  OCIO_NAMESPACE::ConstCPUProcessorRcPtr processor_;
};
// Lazy float loading keeps file ownership in Cycles while making portable
// source spaces a host-side conversion. Ordinary named spaces use the stock
// tiled/cache loader. These exceptional inputs never alter the OCIO config.
class PortableImageLoader final : public ccl::OIIOImageLoader {
public:
  PortableImageLoader(std::string path, std::string revision, int tile,
                      OCIO_NAMESPACE::ConstCPUProcessorRcPtr processor)
      : ccl::OIIOImageLoader(path), path_(std::move(path)), revision_(std::move(revision)), tile_(tile),
        processor_(std::move(processor)), output_space_(color_policy().working_space),
        output_id_(color_policy().output_interop_id) {}
  std::string cache_path(const ccl::ImageLoaderParams &params) const {
    auto cache = std::filesystem::u8path(params.texture_cache_path);
    if (cache.empty())
      cache = std::filesystem::path("cycles_houdini") / revision_;
    if (cache.is_relative())
      cache = std::filesystem::u8path(path_).parent_path() / cache;
    ccl::MD5Hash hash;
    hash.append("portable-input/1");
    hash.append(path_);
    hash.append(revision_);
    hash.append(processor_->getCacheID());
    hash.append(std::to_string(int(params.alpha_type)));
    return path_text(cache / std::filesystem::u8path(path_text(std::filesystem::u8path(path_).filename()) +
                                                     ".portable-" + hash.get_hex() + ".tx"));
  }
  bool load_metadata(ccl::ImageMetaData &metadata, const ccl::ImageLoaderParams &params, ccl::Progress &) override {
    if (params.use_texture_cache) {
      // Only consult our conversion namespace. A stock derivative or source
      // .tx with the same basename has not necessarily had this transform.
      texture_cache_filepath_ = cache_path(params);
      if (auto input = OIIO::ImageInput::open(texture_cache_filepath_)) {
        const auto &spec = input->spec();
        const bool valid = spec.get_string_attribute("cycles_houdini:input_revision") == revision_ &&
                           spec.get_string_attribute("cycles_houdini:input_processor") == processor_->getCacheID() &&
                           spec.get_int_attribute("cycles_houdini:input_alpha", -1) == int(params.alpha_type);
        input->close();
        if (valid && metadata.oiio_load_metadata(texture_cache_filepath_) && metadata.is_tx_file)
          return true;
      }
      if (params.auto_texture_cache && safe_cache_path(path_, texture_cache_filepath_)) {
        const bool existed = std::filesystem::exists(std::filesystem::u8path(texture_cache_filepath_));
        auto source = metadata;
        if (!source.oiio_load_metadata(path_)) {
          ++params.load_failure_num;
          return false;
        }
        prepare_metadata(source);
        source.finalize(params.alpha_type);
        std::vector<float> pixels(std::size_t(source.width) * source.height * 4);
        if (source.oiio_load_pixels(path_, pixels.data(), false)) {
          source.conform_pixels(pixels.data());
          OwnedImageLoader::convert(source, pixels.data(), processor_);
          OIIO::ImageSpec spec(int(source.width), int(source.height), 4, OIIO::TypeDesc::FLOAT);
          spec.attribute("oiio:ColorSpace", output_space_);
          spec.attribute("colorInteropID", output_id_);
          spec.attribute("oiio:UnassociatedAlpha", 0);
          spec.attribute("cycles_houdini:input_revision", revision_);
          spec.attribute("cycles_houdini:input_processor", processor_->getCacheID());
          spec.attribute("cycles_houdini:input_alpha", int(params.alpha_type));
          OIIO::ImageBuf buffer(spec, pixels.data());
          OIIO::ImageSpec options;
          options.set_format(OIIO::TypeDesc::FLOAT);
          options.tile_width = options.tile_height = 64;
          options.tile_depth = 1;
          options.attribute("maketx:fileformatname", "openexr");
          options.attribute("maketx:verbose", 0);
          options.attribute("maketx:filtername", "box");
          try {
            std::filesystem::create_directories(std::filesystem::u8path(texture_cache_filepath_).parent_path());
            StagedOutput staged(texture_cache_filepath_);
            if (OIIO::ImageBufAlgo::make_texture(OIIO::ImageBufAlgo::MakeTxTexture, buffer, staged.temporary_filename(),
                                                 options) &&
                staged.commit() && metadata.oiio_load_metadata(texture_cache_filepath_)) {
              if (!existed)
                record_generated_cache(path_, texture_cache_filepath_);
              return true;
            }
          } catch (const std::runtime_error &) {
            // Cache I/O is optional, including mkdir/reservation/publication
            // failures on read-only asset folders. Source conversion below
            // remains authoritative and never depends on cache writability.
          }
        }
        ++params.tx_failure_num;
      }
    }
    texture_cache_filepath_.clear();
    if (!metadata.oiio_load_metadata(path_)) {
      ++params.load_failure_num;
      return false;
    }
    prepare_metadata(metadata);
    return true;
  }
  void prepare_metadata(ccl::ImageMetaData &metadata) const {
    // A grayscale source can become chromatic after a primaries conversion.
    metadata.type = ccl::IMAGE_DATA_TYPE_FLOAT4;
    metadata.colorspace = ccl::u_colorspace_scene_linear;
    metadata.is_compressible_as_srgb = false;
    metadata.has_tiles_and_mipmaps = false;
    metadata.tile_size = 0;
    metadata.is_tx_file = false;
    metadata.tile_need_conform = true;
    metadata.average_color = ccl::zero_float4();
  }
  bool load_pixels(const ccl::ImageMetaData &metadata, void *target) override {
    auto input = metadata;
    if (!texture_cache_filepath_.empty())
      input.is_unassociated_alpha = false; // Derived pixels were associated before encoding.
    if (!ccl::OIIOImageLoader::load_pixels(input, target))
      return false;
    if (texture_cache_filepath_.empty())
      OwnedImageLoader::convert(metadata, target, processor_);
    return true;
  }
  bool load_pixels_tile(const ccl::ImageMetaData &metadata, int miplevel, std::int64_t x, std::int64_t y,
                        std::int64_t width, std::int64_t height, std::int64_t x_stride, std::int64_t y_stride,
                        std::int64_t padding, ccl::ExtensionType extension, std::uint8_t *pixels) override {
    auto input = metadata;
    input.is_unassociated_alpha = false;
    return ccl::OIIOImageLoader::load_pixels_tile(input, miplevel, x, y, width, height, x_stride, y_stride, padding,
                                                  extension, pixels);
  }
  ccl::string name() const override { return path_; }
  int get_tile_number() const override { return tile_; }
  bool equals(const ccl::ImageLoader &other) const override {
    const auto *value = dynamic_cast<const PortableImageLoader *>(&other);
    return value && path_ == value->path_ && revision_ == value->revision_ && tile_ == value->tile_ &&
           std::string(processor_->getCacheID()) == value->processor_->getCacheID();
  }

private:
  std::string path_, revision_;
  int tile_{};
  OCIO_NAMESPACE::ConstCPUProcessorRcPtr processor_;
  std::string output_space_, output_id_;
};
} // namespace
void convert_texture_pixels(const ccl::ImageMetaData &metadata, void *pixels,
                            const OCIO_NAMESPACE::ConstCPUProcessorRcPtr &processor) {
  OwnedImageLoader::convert(metadata, pixels, processor);
}
std::vector<TextureCachePayload> texture_cache_items(const ShaderGraphData &data, const TextureInputPolicy &policy,
                                                     ShaderContext context, const std::vector<AovDefinition> &aovs) {
  ShaderGraphRequirements requirements;
  const auto graph = prepare_shader_graph(data, requirements, policy, aovs, context);
  std::vector<TextureCachePayload> result;
  for (const auto &asset : requirements.textures) {
    if (asset.memory_image)
      continue;
    if (!asset.failure_revision.empty()) {
      result.push_back({asset, asset.filename, "", 0});
      continue;
    }
    ccl::ImageAlphaType alpha = ccl::IMAGE_ALPHA_AUTO;
    for (auto *node : graph->nodes)
      if (node->name.string() == asset.shader_node) {
        if (const auto *image = dynamic_cast<ccl::ImageTextureNode *>(node))
          alpha = image->image_params().alpha_type;
        else if (const auto *environment = dynamic_cast<ccl::EnvironmentTextureNode *>(node))
          alpha = environment->image_params().alpha_type;
      }
    std::atomic<int> load_failures{}, tx_failures{};
    ccl::ImageLoaderParams params{
        true, false, asset.cache_namespace, true, ccl::ustring(asset.colorspace), alpha, load_failures, tx_failures};
    for (const auto &file : asset.files) {
      std::string destination;
      if (asset.input_processor) {
        PortableImageLoader loader(file.path, asset.revision, 0, asset.input_processor);
        destination = loader.cache_path(params);
      } else {
        ccl::ImageMetaData metadata;
        ccl::resolve_tx(file.path, asset.cache_namespace, params.colorspace, alpha, ccl::IMAGE_FORMAT_PLAIN,
                        destination, metadata, false);
      }
      result.push_back({asset, file.path, safe_cache_path(file.path, destination) ? destination : "", int(alpha)});
    }
  }
  return result;
}
void generate_texture_cache_item(const TextureCachePayload &item) {
  if (!safe_cache_path(item.source, item.destination))
    throw std::invalid_argument("Unsafe generated texture cache destination");
  std::atomic<int> load_failures{}, tx_failures{};
  ccl::ImageLoaderParams params{true,
                                true,
                                item.asset.cache_namespace,
                                true,
                                ccl::ustring(item.asset.colorspace),
                                ccl::ImageAlphaType(item.alpha_type),
                                load_failures,
                                tx_failures};
  ccl::ImageMetaData metadata;
  ccl::Progress progress;
  bool loaded;
  if (item.asset.input_processor) {
    PortableImageLoader loader(item.source, item.asset.revision, 0, item.asset.input_processor);
    loaded = loader.load_metadata(metadata, params, progress);
  } else {
    CacheImageLoader loader(item.source);
    loaded = loader.load_metadata(metadata, params, progress);
  }
  if (!loaded || load_failures || tx_failures || !metadata.is_tx_file ||
      !std::filesystem::is_regular_file(std::filesystem::u8path(item.destination)))
    throw std::runtime_error("Could not generate texture cache: " + item.source);
}
std::vector<TextureCachePlanItem> texture_cache_plan(const OwnedSceneSnapshot &snapshot) {
  if (render_color_identity() != snapshot.color->policy().render_policy_id)
    throw std::invalid_argument("Color configuration changed; recapture textures");
  const auto policy = snapshot_texture_policy(snapshot.textures);
  const auto &state = *snapshot.state;
  const auto aovs = state.aovs.value_or(std::vector<AovDefinition>{});
  std::vector<TextureCachePlanItem> result;
  std::set<std::pair<std::string, std::string>> seen;
  const auto append = [&](const ShaderGraphData &graph, ShaderContext context) {
    for (auto &item : texture_cache_items(graph, policy, context, aovs)) {
      if (!seen.emplace(item.source, item.destination).second)
        continue;
      auto payload = std::make_shared<TextureCachePayload>(std::move(item));
      result.push_back({payload->source, payload->destination, std::move(payload)});
    }
  };
  for (const auto &[key, material] : state.materials) {
    if (material.graph)
      append(*material.graph, ShaderContext::Surface);
    for (const auto &sample : material.displacement_samples)
      append(sample, ShaderContext::Surface);
  }
  if (state.world && state.world->graph)
    append(*state.world->graph, ShaderContext::World);
  for (const auto &[key, light] : state.lights)
    if (light.graph)
      append(*light.graph, ShaderContext::Light);
  return result;
}
void generate_texture_cache(const TextureCachePlanItem &plan, const TextureInputPolicy &policy) {
  if (!plan.payload || plan.source != plan.payload->source || plan.destination != plan.payload->destination)
    throw std::invalid_argument("Invalid owned texture cache plan");
  const auto &item = *plan.payload;
  if (texture_asset_changed(item.asset, policy)) {
    // Creating cycles_houdini changes the discovery directory mtime. Rebuild
    // the source ledger to distinguish our own output from changed/new UDIMs.
    const auto fresh =
        prepare_texture_asset(item.asset.filename, item.asset.auto_input ? "auto" : item.asset.display_colorspace,
                              item.asset.discovery_directory.empty() ? item.asset.tiles : std::vector<int>{}, policy);
    if (fresh.revision != item.asset.revision)
      throw std::invalid_argument("Texture changed; generate a fresh cache plan");
  }
  generate_texture_cache_item(item);
}
void install_memory_images(ccl::Scene &scene, ccl::ShaderGraph &graph, const ShaderGraphRequirements &requirements) {
  for (const auto &texture : requirements.textures) {
    if (!texture.failure_revision.empty())
      continue;
    if (!texture.memory_image && !texture.input_processor &&
        (!scene.image_manager->get_use_texture_cache() || !scene.image_manager->get_auto_texture_cache()))
      continue; // Preserve stock lazy loading/tile culling when not generating.
    ccl::ShaderNode *target = nullptr;
    for (auto *node : graph.nodes)
      if (node->name.string() == texture.shader_node) {
        target = node;
        break;
      }
    if (!target)
      throw std::logic_error("Owned image has no corresponding prepared shader node");
    ccl::ImageSlotTextureNode *slot = nullptr;
    ccl::ImageParams parameters;
    if (auto *node = dynamic_cast<ccl::ImageTextureNode *>(target)) {
      slot = node;
      parameters = node->image_params();
    } else if (auto *node = dynamic_cast<ccl::EnvironmentTextureNode *>(target)) {
      slot = node;
      parameters = node->image_params();
    } else
      throw std::logic_error("Owned image points to an incompatible prepared shader node");
    if (texture.memory_image)
      slot->handle = scene.image_manager->add_image(
          ccl::make_unique<OwnedImageLoader>(texture.memory_image, texture.input_processor), parameters);
    else if (!texture.input_processor && texture.tiles.empty())
      slot->handle =
          scene.image_manager->add_image(ccl::make_unique<CacheImageLoader>(texture.filename), parameters, false);
    else if (!texture.input_processor) {
      ccl::vector<ccl::unique_ptr<ccl::ImageLoader>> loaders;
      for (std::size_t index = 0; index < texture.files.size(); ++index)
        loaders.push_back(ccl::make_unique<CacheImageLoader>(texture.files[index].path,
                                                             texture.tiles.empty() ? 0 : texture.tiles.at(index)));
      slot->handle = scene.image_manager->add_image(std::move(loaders), parameters);
    } else if (texture.tiles.empty())
      slot->handle = scene.image_manager->add_image(
          ccl::make_unique<PortableImageLoader>(texture.filename, texture.revision, 0, texture.input_processor),
          parameters);
    else {
      ccl::vector<ccl::unique_ptr<ccl::ImageLoader>> loaders;
      for (std::size_t i = 0; i < texture.tiles.size(); ++i)
        loaders.push_back(ccl::make_unique<PortableImageLoader>(texture.files.at(i).path, texture.revision,
                                                                texture.tiles[i], texture.input_processor));
      slot->handle = scene.image_manager->add_image(std::move(loaders), parameters);
    }
  }
}
} // namespace cycles_houdini
