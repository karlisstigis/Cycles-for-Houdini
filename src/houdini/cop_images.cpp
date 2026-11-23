// SPDX-License-Identifier: Apache-2.0
#include "cop_images.h"
#include <TIL/TIL_CopResolver.h>
#include <TIL/TIL_Raster.h>
#include <PXL/PXL_Common.h>
#include <UT/UT_Thread.h>
#include <OP/OP_Node.h>
#include <OP/OP_Network.h>
#include <OP/OP_Dependency.h>
#include <algorithm>
#include <bit>
#include <cmath>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <limits>
#include <map>
#include <functional>
#include <stdexcept>

namespace cycles_houdini {
namespace {
// COP internals can retain a name-only interest in the requesting ROP.
// A node-name interest has no parameter/data source; ch()/chs() references
// have a valid source reference even when Houdini labels their interest NAME.
// Keep those real parameter references on the normal conservative path.
// OP_DependencyList uses atEnd(), not a standard begin/end iteration contract.
bool name_only_reference(const OP_Node &from, const OP_Node &target) {
  bool found = false;
  for (auto it = target.getOpDependents().begin(); !it.atEnd(); ++it) {
    if (it->getRefOpId() != from.getUniqueId())
      continue;
    found = true;
    if (it->getSourceRefId().isValid() || (unsigned(it->getInterestType()) & ~unsigned(OP_INTEREST_NAME)))
      return false;
  }
  return found;
}
struct ReturnRaster {
  void operator()(const TIL_Raster *raster) const {
    if (raster)
      TIL_CopResolver::doneWithRaster(raster);
  }
};
std::size_t pixel_elements(int width, int height, int channels) {
  if (width <= 0 || height <= 0 || channels < 1 || channels > 4)
    throw CopImageSourceError("COP image has unsupported dimensions or component count");
  const auto count = std::uint64_t(width) * height * channels;
  if (count > std::uint64_t(2) * 1024 * 1024 * 1024 / sizeof(float))
    throw CopImageSourceError("COP image exceeds the 2 GiB owned pixel-buffer limit");
  return std::size_t(count);
}
} // namespace
NativeCopSource cop_image_source(const std::string &source) {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("COP image source inspection requires Houdini's host thread");
  if (!source.starts_with("op:") || source.find('\0') != std::string::npos)
    throw std::invalid_argument("COP image source inspection requires an op: path");
  NativeCopSource result;
  if (!TIL_CopResolver::getResolver())
    return result;
  result.node_id = TIL_CopResolver::getNodeId(source.c_str());
  int frame_node = -1;
  fpreal frame{};
  result.pinned_frame = TIL_CopResolver::getIdFrame(source.c_str(), frame_node, frame) != 0;
  auto *root = OP_Node::lookupNode(result.node_id);
  if (!root)
    return result;
  result.dependencies = {std::bit_cast<std::int64_t>(double(TIL_CopResolver::getTimeFromFrame(1))),
                         std::bit_cast<std::int64_t>(double(TIL_CopResolver::getTimeFromFrame(2)))};
  std::vector<OP_Node *> pending{root};
  std::map<int, OP_Node *> nodes, name_references;
  // COP project defaults affect pixels, whereas an enclosing Geometry OBJ's
  // transform does not. Explicit references to that OBJ are visited below.
  for (auto *parent = root->getParent(); parent; parent = parent->getParent())
    if (parent->getChildTypeID() == COP_OPTYPE_ID || parent->getChildTypeID() == COP2_OPTYPE_ID)
      nodes.emplace(parent->getUniqueId(), parent);
  while (!pending.empty()) {
    auto *node = pending.back();
    pending.pop_back();
    if (!node || !nodes.emplace(node->getUniqueId(), node).second)
      continue;
    for (unsigned n = 0; n < node->nInputs(); ++n)
      pending.push_back(node->getInput(n));
    OP_NodeList references;
    node->getFullOpReferences(references, false);
    for (auto *reference : references) {
      if (reference && reference->getOpTypeID() == ROP_OPTYPE_ID && name_only_reference(*node, *reference))
        name_references.emplace(reference->getUniqueId(), reference);
      else
        pending.push_back(reference);
    }
    if (auto *network = dynamic_cast<OP_Network *>(node)) {
      OP_NodeList children;
      network->getAllChildren(children);
      for (auto *child : children)
        pending.push_back(child);
    }
  }
  for (const auto &[id, node] : nodes) {
    result.node_ids.push_back(id);
    result.dependencies.insert(result.dependencies.end(), {id, std::int64_t(node->getVersionParms()), node->getBypass(),
                                                           node->getHardLock(), node->nInputs()});
    for (unsigned n = 0; n < node->nInputs(); ++n) {
      const auto *input = node->getInput(n);
      result.dependencies.push_back(input ? input->getUniqueId() : -1);
    }
  }
  for (const auto &[id, node] : name_references)
    if (!nodes.contains(id)) {
      // Keep identity/name invalidation and deletion/rename observation,
      // without turning a name lookup into a dependency on ROP parameters,
      // its camera, all geometry, and every light in the scene.
      result.node_ids.push_back(id);
      const std::uint64_t name_hash = std::hash<std::string>{}(node->getFullPath().toStdString());
      result.dependencies.insert(result.dependencies.end(), {id, -1, std::bit_cast<std::int64_t>(name_hash)});
    }
  return result;
}
std::shared_ptr<const NativeCopImage> freeze_cop_image(const std::string &source, double time) {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("COP image acquisition requires Houdini's host thread");
  if (!source.starts_with("op:") || source.find('\0') != std::string::npos || !std::isfinite(time))
    throw std::invalid_argument("COP image acquisition requires an op: source and finite time");
  if (!TIL_CopResolver::getResolver() || !TIL_CopResolver::canResolve(source.c_str()))
    throw CopImageSourceError("Houdini cannot resolve COP image: " + source);
  TIL_CopResolver::autoEvaluateTime evaluate(time);
  // A previous render's thread-local cache cannot establish the requested
  // frame. Restore the caller's cache preference on every exit path.
  TIL_CopResolver::autoEnableCache cache(false);
  int opid = -1;
  std::unique_ptr<const TIL_Raster, ReturnRaster> raster(TIL_CopResolver::getRaster(source.c_str(), opid));
  if (!raster || !raster->isValid() || raster->isIncomplete() != TIL_RASTER_COMPLETE)
    throw CopImageSourceError("COP image is missing, incomplete or failed to cook: " + source);
  if (raster->getXres() > std::numeric_limits<int>::max() || raster->getYres() > std::numeric_limits<int>::max())
    throw CopImageSourceError("COP image dimensions exceed supported indexing: " + source);
  auto result = std::make_shared<NativeCopImage>();
  result->source = source;
  result->width = int(raster->getXres());
  result->height = int(raster->getYres());
  result->channels = raster->getNumChannels();
  pixel_elements(result->width, result->height, result->channels);
  result->node_id = opid;
  result->source_version = TIL_CopResolver::getRasterVersion(opid);
  result->requested_time = time;
  result->source_time = TIL_CopResolver::getRasterTime(opid);
  result->rotation = raster->getRotation();
  result->colorspace = raster->getColorSpaceName().c_str();
  if (result->colorspace.empty())
    if (const auto *name = PXLgetColorSpaceName(raster->getColorSpace()))
      result->colorspace = name;
  result->storage = std::to_string(int(raster->getFormat()));
  // Force any lazy COP/GPU fetch while the resolver still owns its raster.
  if (!raster->getPixels())
    throw CopImageSourceError("COP image has no readable grid data: " + source);
  result->raster = std::make_shared<const PXL_Raster>(*raster);
  const auto signature = cop_image_source(source);
  result->dependencies = signature.dependencies;
  // Inspect the completed cook's flag directly. OP_Node::isTimeDependent()
  // may cook an out-of-date COP and must not be used during metadata capture.
  if (const auto *node = OP_Node::lookupNode(result->node_id))
    result->time_dependent = !signature.pinned_frame && node->dataMicroNodeConst().isTimeDependent();
  return result;
}
std::vector<float> cop_image_pixels(const NativeCopImage &image) {
  if (!image.raster)
    throw std::logic_error("Owned COP image has no retained raster");
  const auto count = pixel_elements(image.width, image.height, image.channels);
  std::vector<float> pixels(count);
  const auto &source = *image.raster;
  if (source.getFormat() == PXL_FLOAT32 && source.isInterleaved()) {
    const auto row = std::size_t(image.width) * image.channels * sizeof(float);
    for (int y = 0; y < image.height; ++y)
      std::memcpy(pixels.data() + std::size_t(y) * image.width * image.channels, source.getPixel(0, y), row);
  } else {
    for (int y = 0; y < image.height; ++y)
      for (int x = 0; x < image.width; ++x) {
        float value[4]{0, 0, 0, 1};
        source.getPixelValue(x, y, value);
        std::copy_n(value, image.channels, pixels.data() + (std::size_t(y) * image.width + x) * image.channels);
      }
  }
  return pixels;
}
std::shared_ptr<NativeCopBatch> NativeCopImageResolver::acquire(const std::vector<TextureImageKey> &requests) {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("COP resource acquisition requires Houdini's host thread");
  const auto started = std::chrono::steady_clock::now();
  auto batch = std::make_shared<NativeCopBatch>();
  for (const auto &key : requests) {
    if (!std::isfinite(key.evaluation_time))
      throw std::invalid_argument("COP resource request has invalid time");
    auto &entry = batch->entries[key];
    entry.signature = cop_image_source(key.source).dependencies;
    if (cache_) {
      const auto previous = cache_->entries.find(key);
      if (previous != cache_->entries.end() && previous->second.signature == entry.signature &&
          previous->second.image) {
        entry.image = previous->second.image;
        entry.time_dependent = previous->second.time_dependent;
        ++batch->cache_hits;
        continue;
      }
    }
    ++batch->acquisitions;
    try {
      entry.raster = freeze_cop_image(key.source, key.evaluation_time);
      entry.time_dependent = entry.raster->time_dependent;
      if (entry.raster->rotation != 0)
        throw CopImageSourceError("Rotated COP raster requires an orientation adapter: " + key.source);
      // Re-read after cooking so cook-generated versions do not force a
      // second acquisition during the first camera edit.
      entry.signature = cop_image_source(key.source).dependencies;
    } catch (const CopImageSourceError &error) {
      entry.raster.reset();
      entry.error = error.what();
    }
    batch->pending |= bool(entry.raster);
  }
  batch->acquisition_seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count();
  if (!batch->pending)
    convert(*batch);
  return batch;
}
void NativeCopImageResolver::convert(NativeCopBatch &batch) {
  const auto started = std::chrono::steady_clock::now();
  for (auto &[key, entry] : batch.entries) {
    if (!entry.image) {
      auto image = std::make_shared<TextureImageData>();
      image->source = key.source;
      image->evaluation_time = key.evaluation_time;
      if (entry.raster) {
        image->width = entry.raster->width;
        image->height = entry.raster->height;
        image->channels = entry.raster->channels;
        image->colorspace = entry.raster->colorspace;
        image->pixels = cop_image_pixels(*entry.raster);
        if (image->channels == 2 && image->colorspace == "unknown") {
          // Copernicus float2 is XY data, not grayscale + alpha.
          // Tagged two-channel color rasters keep the LA convention.
          std::vector<float> expanded(std::size_t(image->width) * image->height * 3);
          for (std::size_t n = 0; n < expanded.size() / 3; ++n) {
            expanded[3 * n] = image->pixels[2 * n];
            expanded[3 * n + 1] = image->pixels[2 * n + 1];
            expanded[3 * n + 2] = 0;
          }
          image->pixels = std::move(expanded);
          image->channels = 3;
        }
        if (!std::all_of(image->pixels.begin(), image->pixels.end(),
                         [](float value) { return std::isfinite(value); })) {
          image->pixels.clear();
          image->width = image->height = image->channels = 0;
          image->error = "COP image contains non-finite pixel values: " + key.source;
        }
      } else
        image->error = entry.error;
      image->revision = texture_image_revision(*image);
      validate_texture_image(*image);
      entry.image = std::move(image);
    }
    batch.images[key] = entry.image;
    entry.raster.reset();
  }
  batch.pending = false;
  batch.conversion_seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count();
}
void NativeCopImageResolver::commit(std::shared_ptr<const NativeCopBatch> batch) {
  if (!batch || batch->pending)
    throw std::logic_error("COP resources have not completed owned conversion");
  acquisitions += batch->acquisitions;
  cache_hits += batch->cache_hits;
  acquisition_seconds += batch->acquisition_seconds;
  conversion_seconds += batch->conversion_seconds;
  cache_ = std::move(batch);
}
} // namespace cycles_houdini
