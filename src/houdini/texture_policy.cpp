// SPDX-License-Identifier: Apache-2.0
#include "texture_policy.h"
#include "../core/color_internal.h"
#include <IMG/IMG_File.h>
#include <IMG/IMG_FileParms.h>
#include <IMG/IMG_Plane.h>
#include <IMG/IMG_Stat.h>
#include <PXL/PXL_Common.h>
#include <PXL/PXL_OCIO.h>
#include <TIL/TIL_TextureMap.h>
#include <UT/UT_EnvControl.h>
#include <algorithm>
#include <cmath>
#include <cctype>
#include <filesystem>
#include <stdexcept>

namespace cycles_houdini {
namespace {
std::string owned(const char *value) { return value ? value : ""; }
namespace OCIO = OCIO_NAMESPACE;
std::string lower_ascii(std::string value) {
  for (auto &character : value)
    if (static_cast<unsigned char>(character) < 128)
      character = char(std::tolower(static_cast<unsigned char>(character)));
  return value;
}
std::string strict_filename_space(const OCIO::ConstConfigRcPtr &config, const std::string &path) {
  // Apply the documented . _ - space delimiters to the filename. The installed
  // PXL strict evaluator has additional undocumented exclusions (e.g. aliases
  // and some delimiters), and retains its mode until a host OCIO reload.
  // Resolve from the same immutable definitions used by the engine instead.
  const auto utf8 = std::filesystem::path(std::u8string(path.begin(), path.end())).filename().u8string();
  const auto filename = lower_ascii(std::string(utf8.begin(), utf8.end()));
  std::size_t best_end{}, best_length{};
  std::string result;
  const auto delimiter = [](char value) { return value == '.' || value == '_' || value == '-' || value == ' '; };
  const auto match = [&](const char *input, const char *canonical) {
    const auto token = lower_ascii(owned(input));
    if (token.empty())
      return;
    for (std::size_t at = filename.find(token); at != std::string::npos; at = filename.find(token, at + 1)) {
      const auto end = at + token.size();
      if ((at == 0 || delimiter(filename[at - 1])) && (end == filename.size() || delimiter(filename[end])) &&
          (end > best_end || (end == best_end && token.size() > best_length))) {
        best_end = end;
        best_length = token.size();
        result = canonical;
      }
    }
  };
  for (int index = 0; index < config->getNumColorSpaces(OCIO::SEARCH_REFERENCE_SPACE_ALL, OCIO::COLORSPACE_ALL);
       ++index) {
    const auto space = config->getColorSpace(
        config->getColorSpaceNameByIndex(OCIO::SEARCH_REFERENCE_SPACE_ALL, OCIO::COLORSPACE_ALL, index));
    match(space->getName(), space->getName());
    for (std::size_t alias = 0; alias < space->getNumAliases(); ++alias)
      match(space->getAlias(alias), space->getName());
  }
  return result;
}
} // namespace
NativeTextureColorInfo native_texture_color_info(const std::string &filename, bool read_as_is,
                                                 bool observe_texture_cache) {
  if (filename.empty() || filename.find('\0') != std::string::npos)
    throw std::invalid_argument("Texture header inspection requires a filename");
  IMG_FileParms options;
  options.readAsIs();
  auto image = IMG_File::open(filename.c_str(), read_as_is ? &options : nullptr);
  if (!image)
    throw std::invalid_argument("Houdini cannot open texture header: " + filename);
  const auto &stat = image->getStat();
  const auto *plane = stat.getPlane();
  if (!plane)
    throw std::invalid_argument("Texture header has no image planes: " + filename);
  NativeTextureColorInfo result;
  result.filename_space = owned(PXL_OCIO::parseColorSpaceFromString(filename.c_str()));
  result.plane_space = plane->getOCIOColorSpace().c_str();
  result.native_space = owned(PXLgetColorSpaceName(plane->getColorSpace()));
  result.guessed_space = plane->guessOCIOColorSpace(UT_StringHolder(result.filename_space)).c_str();
  result.heuristic_space = plane->guessOCIOColorSpace(UT_StringHolder()).c_str();
  result.storage = owned(IMGgetNameFromDataType(plane->getDataType()));
  result.color_model = owned(IMGgetNameFromColorModel(plane->getColorModel()));
  result.plane_name = plane->getName().c_str();
  result.planes = stat.getNumPlanes();
  if (observe_texture_cache) {
    TIL_TextureMap map{UT_StringHolder(filename)};
    result.texture_space = map.ocioColorSpace().c_str();
  }
  return result;
}
TextureInputPolicy native_texture_input_policy() {
  TextureInputPolicy policy;
  policy.identity = []() {
    return "houdini-auto/3:mode=" + std::to_string(UT_EnvControl::getInt(ENV_HOUDINI_OCIO_FILENAME_COLORSPACE)) +
           ":ocio=" + (PXL_OCIO::configFilePath().isstring() ? "1" : "0") +
           ":srgb=" + owned(UT_EnvControl::getString(ENV_HOUDINI_OCIO_SRGB_FILE_COLORSPACE));
  };
  policy.resolve_auto = [](const TextureHeader &header) -> TextureInputSelection {
    if (!PXL_OCIO::configFilePath().isstring())
      throw std::invalid_argument("Native Auto requires a valid active Houdini OCIO configuration: " + header.filename);
    const auto mode = UT_EnvControl::getInt(ENV_HOUDINI_OCIO_FILENAME_COLORSPACE);
    if (mode < 0 || mode > 5)
      throw std::invalid_argument("HOUDINI_OCIO_FILENAME_COLORSPACE must be in 0..5 for native Auto");
    // Use the engine's frozen OCIO configuration. The public PXL parser
    // caches its mode until reload, so delegating every edit to it can mix
    // a new mode with an older evaluator. Exact file rules are only evaluated
    // on changed graphs; warm camera edits never enter this resolver.
    if (mode != 0) {
      const auto config = render_authoring_color_configuration();
      std::string detected;
      if (mode == 3)
        detected = strict_filename_space(config, header.filename);
      else if (mode < 4 || !config->filepathOnlyMatchesDefaultRule(header.filename.c_str()))
        detected = owned(config->getColorSpaceFromFilepath(header.filename.c_str()));
      if (!detected.empty())
        return {detected, "filename"};
    }
    // A recognized filename is the host/pipeline override. Within the header,
    // an ACES container declaration takes priority, then a usable portable
    // identifier, then named metadata and legacy format interpretation.
    const auto id = header.aces_image_container ? std::string("lin_ap0_scene") : header.color_interop_id;
    if (!id.empty() && id != "unknown") {
      const auto input = render_portable_input_space(id);
      if (!input.error.empty())
        throw std::invalid_argument("Native Auto cannot convert texture colorInteropID '" + id + "': " + input.error +
                                    ": " + header.filename);
      if (!input.colorspace.empty())
        return {input.colorspace, header.aces_image_container ? "acesImageContainer" : "colorInteropID", input.label};
    }
    const auto reason = [&](const std::string &source) {
      return id.empty() ? source : source + " (unresolved colorInteropID '" + id + "')";
    };
    // Legacy OIIO names are mapped by
    // public Houdini image semantics; named studio spaces remain explicit
    // and are validated against the frozen render configuration by the core.
    const auto &declared = header.file_colorspace;
    if (declared == "scene_linear" || declared == "scene_linear_srgb" || declared == "__builtin_srgb" ||
        declared.starts_with("__cycles_houdini_portable_"))
      throw std::invalid_argument(
          "Native Auto cannot identify a source encoding from an unqualified engine storage marker: " + declared +
          ": " + header.filename + ". Choose an explicit input colorspace.");
    const bool legacy_linear = declared == "Linear" || declared == "linear" || declared == "lin_srgb";
    const bool legacy_srgb = declared == "sRGB" || declared == "srgb";
    if (!declared.empty() && !legacy_linear && !legacy_srgb)
      return {declared, reason("metadata")};
    if (legacy_srgb && header.format == "openexr") {
      // An explicitly encoded legacy EXR is not made linear by its
      // primaries or floating-point storage. Non-EXR format defaults
      // continue through the host's sRGB-file override policy below.
      const auto input = render_portable_input_space("srgb_rec709_scene");
      if (!input.error.empty() || input.colorspace.empty())
        throw std::invalid_argument("Native Auto cannot convert legacy EXR sRGB metadata: " + input.error + ": " +
                                    header.filename);
      return {input.colorspace, reason("metadata"), input.label};
    }
    if (header.chromaticities && header.format == "openexr") {
      // Legacy EXRs can establish the six common linear encodings through
      // their actual primaries. A usable interop ID above is authoritative.
      // Coordinates are from the CIF Texture Assets recommendation.
      const std::pair<const char *, std::array<float, 8>> standards[]{
          {"lin_rec709_scene", {.64f, .33f, .30f, .60f, .15f, .06f, .3127f, .3290f}},
          {"lin_ap1_scene", {.713f, .293f, .165f, .830f, .128f, .044f, .32168f, .33767f}},
          {"lin_ap0_scene", {.7347f, .2653f, 0, 1, .0001f, -.0770f, .32168f, .33767f}},
          {"lin_rec2020_scene", {.708f, .292f, .170f, .797f, .131f, .046f, .3127f, .3290f}},
          {"lin_p3d65_scene", {.680f, .320f, .265f, .690f, .150f, .060f, .3127f, .3290f}},
          {"lin_adobergb_scene", {.640f, .330f, .210f, .710f, .150f, .060f, .3127f, .3290f}}};
      for (const auto &[standard, primaries] : standards) {
        bool matches = true;
        for (std::size_t i = 0; i < primaries.size(); ++i)
          if (!std::isfinite((*header.chromaticities)[i]) ||
              std::abs((*header.chromaticities)[i] - primaries[i]) > 1e-5f) {
            matches = false;
            break;
          }
        if (matches) {
          const auto input = render_portable_input_space(standard);
          if (!input.error.empty() || input.colorspace.empty())
            throw std::invalid_argument("Native Auto cannot convert texture chromaticities: " + input.error + ": " +
                                        header.filename);
          return {input.colorspace, reason("chromaticities"), input.label};
        }
      }
      throw std::invalid_argument(
          "Native Auto requires an explicit input colorspace for unsupported texture chromaticities: " +
          header.filename);
    }
    // Primaries alone do not establish a transfer function in PNG/TIFF.
    // Preserve the existing host format interpretation for Rec.709 primaries;
    // a nonstandard declaration still needs an explicit input encoding.
    if (header.chromaticities && header.format != "openexr") {
      constexpr std::array<float, 8> rec709{.64f, .33f, .30f, .60f, .15f, .06f, .3127f, .3290f};
      for (std::size_t i = 0; i < rec709.size(); ++i)
        if (!std::isfinite((*header.chromaticities)[i]) || std::abs((*header.chromaticities)[i] - rec709[i]) > 1e-5f)
          throw std::invalid_argument(
              "Native Auto requires an explicit input encoding for non-EXR texture chromaticities: " + header.filename);
    }
    if (!id.empty() && id != "unknown")
      throw std::invalid_argument("Native Auto cannot resolve portable texture colorInteropID '" + id +
                                  "': " + header.filename + ". Choose an explicit input colorspace.");
    const auto info = native_texture_color_info(header.filename);
    if (info.heuristic_space.empty())
      throw std::invalid_argument("Houdini could not determine the texture input colorspace: " + header.filename);
    return {info.heuristic_space, reason(declared.empty() ? "format" : "metadata")};
  };
  return policy;
}
} // namespace cycles_houdini
