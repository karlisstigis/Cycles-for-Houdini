// SPDX-License-Identifier: Apache-2.0
#include "texture_assets.h"
#include "color_internal.h"
#include <OpenImageIO/imageio.h>
#include <OpenColorIO/OpenColorIO.h>
#include "util/md5.h"
#include <algorithm>
#include <filesystem>
#include <cstring>
#include <regex>
#include <set>
#include <stdexcept>

namespace OCIO = OCIO_NAMESPACE;
namespace cycles_houdini {
namespace {
namespace fs = std::filesystem;
std::string utf8(const fs::path &path) {
  const auto value = path.generic_u8string();
  return {value.begin(), value.end()};
}
std::int64_t modified(const fs::path &path) {
  return static_cast<std::int64_t>(fs::last_write_time(path).time_since_epoch().count());
}
TextureFileStamp stamp(const std::string &path) {
  const auto native = fs::u8path(path);
  if (!fs::is_regular_file(native))
    throw TextureSourceError("Missing texture file: " + path);
  return {path, fs::file_size(native), modified(native)};
}
std::string configuration() { return render_color_identity(); }
std::string replace(std::string text, const std::string &token, const std::string &value) {
  const auto index = text.find(token);
  if (index != std::string::npos)
    text.replace(index, token.size(), value);
  return text;
}
std::string tile_filename(const std::string &pattern, int tile) {
  auto text = replace(pattern, "<UDIM>", std::to_string(tile));
  return replace(text, "<UVTILE>",
                 "u" + std::to_string((tile - 1001) % 10 + 1) + "_v" + std::to_string((tile - 1001) / 10 + 1));
}
std::string escaped(const std::string &text) {
  std::string result;
  for (char ch : text) {
    if (std::string(".^$|()[]{}*+?\\").find(ch) != std::string::npos)
      result += '\\';
    result += ch;
  }
  return result;
}
std::string validate_colorspace(const std::string &value) {
  // These engine tokens have special handling independently of OCIO names.
  if (value.empty() || value == "data" || value == "scene_linear")
    return value;
  if (value == "scene_linear_srgb" || value == "__builtin_srgb")
    throw TextureSourceError("Texture colorspace is reserved for internal engine storage: " + value);
  const auto config = render_color_configuration();
  const auto space = config->getColorSpace(value.c_str());
  if (!space)
    throw TextureSourceError("Unknown texture colorspace in the active OCIO configuration: " + value);
  try {
    config->getProcessor(value.c_str(), OCIO::ROLE_SCENE_LINEAR);
  } catch (const OCIO::Exception &error) {
    throw TextureSourceError("Texture colorspace cannot convert to scene_linear: " + value + ": " + error.what());
  }
  return space->getName();
}
void prepare_input_conversion(TextureAsset &asset) {
  if (!asset.colorspace.starts_with("__cycles_houdini_portable_") ||
      render_authoring_color_configuration()->getColorSpace(asset.colorspace.c_str()))
    return;
  const auto config = render_color_configuration();
  asset.input_processor =
      config->getProcessor(asset.colorspace.c_str(), OCIO::ROLE_SCENE_LINEAR)->getDefaultCPUProcessor();
  // Keep the portable source in the revision and diagnostic label. Only the
  // shader's storage interpretation becomes working-linear.
  asset.colorspace = "scene_linear";
}
} // namespace
TextureAsset prepare_texture_asset(const std::string &filename, const std::string &colorspace,
                                   const std::vector<int> &requested_tiles, const TextureInputPolicy &policy,
                                   double image_time) {
  if (filename.empty() || filename.find('\0') != std::string::npos)
    throw TextureSourceError("Image Texture requires a filename");
  if (filename.starts_with("op:")) {
    if (!policy.resolve_image)
      throw TextureSourceError("Houdini op: texture sources require an in-memory image adapter");
    if (!requested_tiles.empty())
      throw TextureSourceError("COP image tiles require a tiled memory-image adapter: " + filename);
    auto image = policy.resolve_image(filename, image_time);
    if (!image)
      throw TextureSourceError("No captured COP image is available: " + filename);
    validate_texture_image(*image);
    if (image->source != filename || image->evaluation_time != image_time)
      throw std::invalid_argument("Owned image source/time does not match its shader request");
    if (!image->error.empty())
      throw TextureSourceError(image->error);
    TextureAsset result;
    result.memory_image = std::move(image);
    result.decoded_pixels = std::uint64_t(result.memory_image->width) * result.memory_image->height;
    result.filename = filename;
    result.image_time = image_time;
    result.auto_input = colorspace.empty() || colorspace == "auto";
    const auto input = result.auto_input ? result.memory_image->colorspace : colorspace;
    if (input.empty() || input == "unknown" || input == "auto" ||
        (result.auto_input && (input == "linear" || input == "Linear")))
      throw TextureSourceError("COP image has no qualified input colorspace; select Data or an explicit input space: " +
                               filename);
    result.colorspace = result.display_colorspace = validate_colorspace(input);
    result.selection = result.auto_input ? "captured COP metadata" : "explicit";
    result.color_configuration = configuration();
    if (result.auto_input && policy.identity)
      result.input_policy = policy.identity();
    ccl::MD5Hash hash;
    hash.append(result.memory_image->revision);
    hash.append(result.color_configuration);
    hash.append(result.colorspace);
    hash.append(result.input_policy);
    result.revision = hash.get_hex();
    prepare_input_conversion(result);
    return result;
  }
  const bool auto_input = colorspace.empty() || colorspace == "auto";
  TextureAsset result;
  result.filename = utf8(fs::absolute(fs::u8path(filename)).lexically_normal());
  result.auto_input = auto_input;
  result.colorspace = auto_input ? "" : validate_colorspace(colorspace);
  result.display_colorspace = result.colorspace;
  result.selection = auto_input ? "engine_auto" : "explicit";
  if (auto_input && policy.identity)
    result.input_policy = policy.identity();
  result.color_configuration = configuration();
  const auto udim = result.filename.find("<UDIM>");
  const auto uvtile = result.filename.find("<UVTILE>");
  const bool tiled = udim != std::string::npos || uvtile != std::string::npos;
  if (udim != std::string::npos && uvtile != std::string::npos)
    throw TextureSourceError("A texture path cannot contain both UDIM and UVTILE tokens");
  if (!tiled && !requested_tiles.empty())
    throw TextureSourceError("Texture tile IDs require <UDIM> or <UVTILE> in the filename");
  std::set<int> tiles;
  for (int tile : requested_tiles) {
    if (tile < 1001 || tile > 9999 || !tiles.insert(tile).second)
      throw TextureSourceError("Texture tile IDs must be unique integers in 1001..9999");
  }
  if (tiled && tiles.empty()) {
    const auto pattern_path = fs::u8path(result.filename);
    const auto basename = utf8(pattern_path.filename());
    const auto token = udim != std::string::npos ? std::string("<UDIM>") : std::string("<UVTILE>");
    const auto at = basename.find(token);
    if (at == std::string::npos || basename.find(token, at + token.size()) != std::string::npos)
      throw TextureSourceError("A tiled texture requires exactly one token in its basename");
    result.discovery_directory = utf8(pattern_path.parent_path());
    result.directory_modified = modified(pattern_path.parent_path());
    const std::regex matcher("^" + escaped(basename.substr(0, at)) +
                             (token == "<UDIM>" ? "([0-9]{4})" : "u([0-9]+)_v([0-9]+)") +
                             escaped(basename.substr(at + token.size())) + "$");
    for (const auto &entry : fs::directory_iterator(pattern_path.parent_path()))
      if (entry.is_regular_file()) {
        std::smatch match;
        const auto name = utf8(entry.path().filename());
        if (!std::regex_match(name, match, matcher))
          continue;
        try {
          int tile = 0;
          if (token == "<UDIM>")
            tile = std::stoi(match[1]);
          else {
            const auto u = std::stoll(match[1]), v = std::stoll(match[2]);
            if (u < 1 || u > 10 || v < 1 || v > 900)
              continue;
            tile = int(1001 + (v - 1) * 10 + u - 1);
          }
          if (tile >= 1001 && tile <= 9999 && tile_filename(result.filename, tile) == utf8(entry.path()))
            tiles.insert(tile);
        } catch (const std::exception &) {
          continue;
        }
      }
    if (tiles.empty())
      throw TextureSourceError("No texture tiles match: " + result.filename);
  }
  result.tiles.assign(tiles.begin(), tiles.end());
  const auto load = [&](const std::string &path) {
    const auto before = stamp(path);
    auto input = OIIO::ImageInput::open(path);
    if (!input)
      throw TextureSourceError("Cannot read texture header: " + path + ": " + OIIO::geterror());
    const auto &spec = input->spec();
    if (spec.width <= 0 || spec.height <= 0 || spec.depth != 1 || spec.nchannels <= 0)
      throw TextureSourceError("Texture has invalid dimensions or channels: " + path);
    result.decoded_pixels += std::uint64_t(spec.width) * spec.height;
    if (auto_input && policy.resolve_auto) {
      TextureHeader header;
      header.filename = path;
      header.file_colorspace = spec.get_string_attribute("oiio:ColorSpace");
      header.format = input->format_name();
      header.color_interop_id = spec.get_string_attribute("colorInteropID");
      if (const auto *value = spec.find_attribute("colorInteropID"); value && value->type() != OIIO::TypeDesc::STRING)
        throw TextureSourceError("Texture colorInteropID must be a string: " + path);
      if (const auto *value = spec.find_attribute("acesImageContainer")) {
        if ((value->type() != OIIO::TypeDesc::INT && value->type() != OIIO::TypeDesc::UINT) ||
            spec.get_int_attribute("acesImageContainer", 0) != 1)
          throw TextureSourceError("Texture acesImageContainer must be the integer flag 1: " + path);
        header.aces_image_container = true;
      }
      if (const auto *value = spec.find_attribute("chromaticities")) {
        if (value->type() != OIIO::TypeDesc("float[8]") || value->nvalues() != 1)
          throw TextureSourceError("Texture chromaticities have unsupported metadata storage: " + path);
        std::array<float, 8> primaries;
        std::memcpy(primaries.data(), value->data(), sizeof(primaries));
        header.chromaticities = primaries;
      }
      const auto selected = policy.resolve_auto(header);
      if (selected.colorspace.empty())
        throw TextureSourceError("Native Auto could not determine a texture input colorspace: " + path);
      const auto resolved = validate_colorspace(selected.colorspace);
      if (!result.files.empty() && result.colorspace != resolved)
        throw TextureSourceError("Texture tiles resolve to different input colorspaces: " + result.files.front().path +
                                 " (" + result.colorspace + ") and " + path + " (" + resolved +
                                 "). Choose an explicit Image Texture input space or use separate texture nodes.");
      result.colorspace = resolved;
      if (result.files.empty())
        result.display_colorspace = selected.label.empty() ? resolved : selected.label;
      if (result.files.empty())
        result.selection = selected.reason;
      else if (result.selection != selected.reason)
        result.selection = "mixed sources (uniform colorspace)";
    }
    input->close();
    if (stamp(path) != before)
      throw TextureSourceError("Texture changed while reading its header: " + path);
    result.files.push_back(before);
  };
  if (tiled)
    for (int tile : result.tiles)
      load(tile_filename(result.filename, tile));
  else
    load(result.filename);
  ccl::MD5Hash hash;
  hash.append(result.color_configuration);
  hash.append(result.colorspace);
  hash.append(result.input_policy);
  for (const auto &file : result.files) {
    hash.append(file.path);
    hash.append(std::to_string(file.size));
    hash.append(std::to_string(file.modified));
  }
  result.revision = hash.get_hex();
  result.cache_namespace = "cycles_houdini/" + result.revision;
  prepare_input_conversion(result);
  return result;
}
bool texture_asset_changed(const TextureAsset &asset, const TextureInputPolicy &policy) {
  try {
    if (asset.filename.starts_with("op:") && policy.resolve_image) {
      const auto current = policy.resolve_image(asset.filename, asset.image_time);
      if (current && (current->source != asset.filename || current->evaluation_time != asset.image_time))
        return true;
      if ((current ? current->revision : std::string{}) !=
          (asset.memory_image ? asset.memory_image->revision : std::string{}))
        return true;
    }
    if (!asset.failure_revision.empty())
      return asset.failure_revision !=
             texture_watch_revision({asset.filename}, policy.identity ? policy.identity() : std::string{});
    if (configuration() != asset.color_configuration)
      return true;
    if (asset.auto_input && policy.identity && policy.identity() != asset.input_policy)
      return true;
    if (asset.memory_image) {
      if (!policy.resolve_image)
        return true;
      const auto current = policy.resolve_image(asset.filename, asset.image_time);
      return !current || current->revision != asset.memory_image->revision;
    }
    for (const auto &file : asset.files)
      if (stamp(file.path) != file)
        return true;
    if (!asset.discovery_directory.empty() &&
        modified(fs::u8path(asset.discovery_directory)) != asset.directory_modified)
      return true;
    return false;
  } catch (const std::exception &) {
    return true;
  } // Preparation supplies the actionable failure.
}
std::string texture_watch_path(const std::string &filename) {
  if (filename.empty() || filename.starts_with("op:") || filename.find('\0') != std::string::npos)
    return filename;
  return utf8(fs::absolute(fs::u8path(filename)).lexically_normal());
}
std::string texture_watch_revision(const std::vector<std::string> &filenames, const std::string &input_policy) {
  ccl::MD5Hash hash;
  hash.append(configuration());
  hash.append(input_policy);
  for (const auto &filename : filenames) {
    hash.append(filename);
    if (filename.empty() || filename.starts_with("op:"))
      continue;
    try {
      const auto path = fs::u8path(filename);
      const auto basename = utf8(path.filename());
      auto at = basename.find("<UDIM>");
      std::string token = "<UDIM>";
      if (at == std::string::npos) {
        at = basename.find("<UVTILE>");
        token = "<UVTILE>";
      }
      std::vector<std::string> files;
      if (at == std::string::npos)
        files.push_back(filename);
      else if (fs::is_directory(path.parent_path())) {
        const std::regex matcher("^" + escaped(basename.substr(0, at)) + ".*" +
                                 escaped(basename.substr(at + token.size())) + "$");
        for (const auto &entry : fs::directory_iterator(path.parent_path()))
          if (entry.is_regular_file() && std::regex_match(utf8(entry.path().filename()), matcher))
            files.push_back(utf8(entry.path()));
      }
      std::sort(files.begin(), files.end());
      for (const auto &file : files) {
        hash.append(file);
        try {
          const auto value = stamp(file);
          hash.append(std::to_string(value.size));
          hash.append(std::to_string(value.modified));
        } catch (const std::exception &) {
          hash.append("missing");
        }
      }
    } catch (const std::exception &) {
      hash.append("unavailable");
    }
  }
  return hash.get_hex();
}
} // namespace cycles_houdini
