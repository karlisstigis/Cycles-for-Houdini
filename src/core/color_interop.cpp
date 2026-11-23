// SPDX-License-Identifier: Apache-2.0
#include "color_interop.h"
#include <algorithm>
#include <array>
#include <cmath>
#include <set>
#include <string_view>
#include <vector>

namespace OCIO = OCIO_NAMESPACE;
namespace cycles_houdini {
namespace {
constexpr std::string_view allowed = ".-_~/*#%^+()[]|";
bool allowed_character(unsigned char ch) {
  return (ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9') || allowed.find(char(ch)) != std::string_view::npos;
}
OCIO::ConstConfigRcPtr builtin() {
  // This is the configuration shipped with the certified SDK, not a download
  // or a mutable global. Its version is retained in the color diagnostics.
  static const OCIO::ConstConfigRcPtr value = []() {
    auto config = OCIO::Config::CreateFromBuiltinConfig("studio-config-latest")->createEditableCopy();
    // The certified 2.5 catalogue predates the July 2026 CIF recommendation.
    // Its scene-referred gamma-2.4 implementation already has the required
    // math; add the published bare ID while preserving the earlier alias.
    if (!config->getColorSpace("g24_rec709_scene")) {
      auto gamma = config->getColorSpace("ocio:g24_rec709_scene")->createEditableCopy();
      gamma->setInteropID("g24_rec709_scene");
      gamma->addAlias("g24_rec709_scene");
      config->addColorSpace(gamma);
    }
    if (!config->getColorSpace("lin_ciexyzd65_scene")) {
      // Use the SDK's analytic Bradford utility, not a display transform.
      // Compose explicitly into this configuration's scene reference.
      auto reference = OCIO::ColorSpace::Create(OCIO::REFERENCE_SPACE_SCENE);
      reference->setName("__cycles_houdini_builtin_reference");
      config->addColorSpace(reference);
      auto utility = OCIO::BuiltinTransform::Create();
      utility->setStyle("UTILITY - ACES-AP0_to_CIE-XYZ-D65_BFD");
      utility->setDirection(OCIO::TRANSFORM_DIR_INVERSE);
      auto transform = OCIO::GroupTransform::Create();
      transform->appendTransform(utility);
      transform->appendTransform(
          config->getProcessor("aces_interchange", reference->getName())->createGroupTransform());
      auto xyz = OCIO::ColorSpace::Create(OCIO::REFERENCE_SPACE_SCENE);
      xyz->setName("CIE XYZ-D65 - Scene-referred");
      xyz->setInteropID("lin_ciexyzd65_scene");
      xyz->addAlias("lin_ciexyzd65_scene");
      xyz->setTransform(transform, OCIO::COLORSPACE_DIR_TO_REFERENCE);
      config->addColorSpace(xyz);
      std::string inactive = config->getInactiveColorSpaces();
      if (!inactive.empty())
        inactive += ',';
      inactive += reference->getName();
      config->setInactiveColorSpaces(inactive.c_str());
    }
    const std::string name = std::string(config->getName()) + "+cycles-houdini-cif-2026-07-v1";
    config->setName(name.c_str());
    return config;
  }();
  return value;
}
bool identity_processor(const OCIO::ConstProcessorRcPtr &processor) {
  // Test compiled analytic operations, not sampled basis vectors or names.
  // Lossless optimization cancels inverse matrices for the stock six linear
  // spaces. A remaining LUT, exponent, offset or nonidentity matrix is not
  // sufficient evidence of a standard encoding.
  const auto group = processor->getOptimizedProcessor(OCIO::OPTIMIZATION_LOSSLESS)->createGroupTransform();
  for (int i = 0; i < group->getNumTransforms(); ++i) {
    const auto matrix = std::dynamic_pointer_cast<const OCIO::MatrixTransform>(group->getTransform(i));
    if (!matrix)
      return false;
    double values[16], offset[4];
    matrix->getMatrix(values);
    matrix->getOffset(offset);
    for (int c = 0; c < 16; ++c)
      if (!std::isfinite(values[c]) || std::abs(values[c] - (c % 5 == 0 ? 1. : 0.)) > 1e-12)
        return false;
    for (double v : offset)
      if (!std::isfinite(v) || std::abs(v) > 1e-12)
        return false;
  }
  return true;
}
std::string local_space_name(const OCIO::ConstConfigRcPtr &config, const std::string &base) {
  std::string match;
  for (int i = 0; i < config->getNumColorSpaces(OCIO::SEARCH_REFERENCE_SPACE_ALL, OCIO::COLORSPACE_ALL); ++i) {
    const auto name = config->getColorSpaceNameByIndex(OCIO::SEARCH_REFERENCE_SPACE_ALL, OCIO::COLORSPACE_ALL, i);
    if (sanitize_interop_component(name) == base) {
      if (!match.empty() && match != name)
        return {}; // Sanitization can collide.
      match = name;
    }
  }
  return match;
}
} // namespace
std::string sanitize_interop_component(const std::string &input) {
  std::string result;
  for (std::size_t i = 0; i < input.size(); ++i) {
    const auto value = static_cast<unsigned char>(input[i]);
    if (value >= 128) {
      // Replace a non-ASCII Unicode code point once, not once per UTF-8 byte.
      std::size_t length = value >= 0xc2 && value <= 0xdf   ? 2
                           : value >= 0xe0 && value <= 0xef ? 3
                           : value >= 0xf0 && value <= 0xf4 ? 4
                                                            : 1;
      if (i + length > input.size())
        length = 1;
      for (std::size_t j = 1; j < length; ++j)
        if ((static_cast<unsigned char>(input[i + j]) & 0xc0) != 0x80) {
          length = 1;
          break;
        }
      result += '^';
      i += length - 1;
      continue;
    }
    char ch = char(value);
    if (ch >= 'A' && ch <= 'Z')
      ch = char(ch - 'A' + 'a');
    if (allowed_character(static_cast<unsigned char>(ch)))
      result += ch;
    else
      switch (ch) {
      case ' ':
      case '\t':
      case '\r':
      case '\n':
      case '\f':
      case '\v':
        result += '_';
        break;
      case '<':
      case '{':
        result += '(';
        break;
      case '>':
      case '}':
        result += ')';
        break;
      case ',':
        result += '.';
        break;
      case ';':
      case ':':
        result += '|';
        break;
      case '\'':
      case '"':
        result += '#';
        break;
      case '\\':
        result += '/';
        break;
      default:
        result += '*';
        break;
      }
  }
  return result;
}
bool valid_interop_id(const std::string &input) {
  if (input.empty() || input.back() == ':' || input.find('\0') != std::string::npos)
    return false;
  int separators = 0;
  for (unsigned char ch : input)
    if (ch == ':') {
      if (++separators > 2)
        return false;
    } else if (!allowed_character(ch))
      return false;
  return true;
}
OCIO::ConstColorSpaceRcPtr find_interop_space(const OCIO::ConstConfigRcPtr &config, const std::string &id) {
  if (!valid_interop_id(id))
    return {};
  if (const auto space = config->getColorSpace(id.c_str()))
    return space;
  const auto first = id.find(':');
  if (first != std::string::npos) {
    const auto stripped = id.substr(first + 1);
    if (const auto space = config->getColorSpace(stripped.c_str()))
      return space;
    const auto second = id.find(':', first + 1);
    if (second != std::string::npos && id.substr(first + 1, second - first - 1) == "local" &&
        id.substr(0, first) == sanitize_interop_component(config->getName())) {
      const auto name = local_space_name(config, id.substr(second + 1));
      if (!name.empty())
        return config->getColorSpace(name.c_str());
    }
  }
  return {};
}
ColorInteropIdentity working_interop_identity(const OCIO::ConstConfigRcPtr &config) {
  const auto working = config->getColorSpace(OCIO::ROLE_SCENE_LINEAR);
  if (!working)
    return {"unknown", "missing scene_linear role"};
  if (working->isData())
    return {"data", "authored data space"};
  const std::string authored = working->getInteropID();
  if (!authored.empty())
    return valid_interop_id(authored) ? ColorInteropIdentity{authored, "authored"}
                                      : ColorInteropIdentity{"unknown", "invalid authored identifier"};
  const auto standard = builtin();
  if (config->hasRole("aces_interchange") && working->getReferenceSpaceType() == OCIO::REFERENCE_SPACE_SCENE) {
    std::vector<std::string> candidates;
    for (const char *id : {"lin_ap1_scene", "lin_ap0_scene", "lin_rec709_scene", "lin_rec2020_scene", "lin_p3d65_scene",
                           "lin_adobergb_scene"})
      if (const auto space = standard->getColorSpace(id))
        candidates.emplace_back(space->getName());
    for (int i = 0; i < standard->getNumColorSpaces(OCIO::SEARCH_REFERENCE_SPACE_SCENE, OCIO::COLORSPACE_ALL); ++i)
      candidates.emplace_back(
          standard->getColorSpaceNameByIndex(OCIO::SEARCH_REFERENCE_SPACE_SCENE, OCIO::COLORSPACE_ALL, i));
    std::set<std::string> visited;
    for (const auto &name : candidates) {
      if (!visited.insert(name).second)
        continue;
      const auto space = standard->getColorSpace(name.c_str());
      if (space->isData() || std::string(space->getInteropID()).empty())
        continue;
      try {
        const auto processor =
            OCIO::Config::GetProcessorFromConfigs(config, working->getName(), standard, name.c_str());
        if (identity_processor(processor))
          return {space->getInteropID(), "verified builtin equivalence"};
      } catch (const OCIO::Exception &) { /* An unavailable comparison does not establish an ID. */
      }
    }
  }
  const auto config_name = sanitize_interop_component(config->getName());
  const auto space_name = sanitize_interop_component(working->getName());
  if (!config_name.empty() && !space_name.empty() && local_space_name(config, space_name) == working->getName())
    return {config_name + ":local:" + space_name, "configuration-local"};
  return {"unknown", "no established standard or unambiguous local identity"};
}
PortableColorConfiguration make_portable_color_configuration(const OCIO::ConstConfigRcPtr &user) {
  PortableColorConfiguration result;
  result.config = user;
  const auto standard = builtin();
  result.builtin_configuration = standard->getName();
  // A user-provided interchange role establishes the connection to AP0.
  // Never guess a reference from a familiar working-space name.
  if (!user->hasRole("aces_interchange"))
    return result;
  const auto interchange = user->getColorSpace("aces_interchange");
  if (!interchange || interchange->isData() || interchange->getReferenceSpaceType() != OCIO::REFERENCE_SPACE_SCENE)
    return result;
  auto overlay = user->createEditableCopy();
  const std::string prefix = "__cycles_houdini_portable_";
  std::string reference = prefix + "reference";
  while (overlay->getColorSpace(reference.c_str()))
    reference += '_';
  auto reference_space = OCIO::ColorSpace::Create(OCIO::REFERENCE_SPACE_SCENE);
  reference_space->setName(reference.c_str());
  overlay->addColorSpace(reference_space);
  const auto to_reference = overlay->getProcessor("aces_interchange", reference.c_str())->createGroupTransform();
  std::string inactive = overlay->getInactiveColorSpaces();
  const auto hide = [&](const std::string &name) {
    if (!inactive.empty())
      inactive += ',';
    inactive += name;
  };
  hide(reference);
  for (int i = 0; i < standard->getNumColorSpaces(OCIO::SEARCH_REFERENCE_SPACE_SCENE, OCIO::COLORSPACE_ALL); ++i) {
    const auto source = standard->getColorSpace(
        standard->getColorSpaceNameByIndex(OCIO::SEARCH_REFERENCE_SPACE_SCENE, OCIO::COLORSPACE_ALL, i));
    const std::string id = source->getInteropID();
    if (id.empty() || source->isData() || find_interop_space(user, id))
      continue;
    PortableInputSpace entry;
    entry.label = source->getName();
    try {
      auto transform = OCIO::GroupTransform::Create();
      transform->appendTransform(standard->getProcessor(source->getName(), "aces_interchange")->createGroupTransform());
      transform->appendTransform(to_reference);
      std::string name = prefix + std::to_string(i);
      while (overlay->getColorSpace(name.c_str()))
        name += '_';
      auto space = OCIO::ColorSpace::Create(OCIO::REFERENCE_SPACE_SCENE);
      space->setName(name.c_str());
      space->setInteropID(id.c_str());
      space->setTransform(transform, OCIO::COLORSPACE_DIR_TO_REFERENCE);
      overlay->addColorSpace(space);
      hide(name);
      entry.colorspace = name;
    } catch (const OCIO::Exception &error) {
      entry.error = error.what();
    }
    result.inputs.emplace(source->getName(), std::move(entry));
  }
  overlay->setInactiveColorSpaces(inactive.c_str());
  result.config = overlay;
  return result;
}
PortableInputSpace portable_input_space(const OCIO::ConstConfigRcPtr &user, const PortableColorConfiguration &overlay,
                                        const std::string &id) {
  if (id == "data")
    return {"data", "Data", {}};
  if (id.empty() || id == "unknown")
    return {};
  if (const auto space = find_interop_space(user, id))
    return {space->isData() ? "data" : space->getName(), space->getName(), {}};
  if (const auto source = find_interop_space(builtin(), id)) {
    if (source->isData())
      return {"data", "Data", {}};
    if (const auto found = overlay.inputs.find(source->getName()); found != overlay.inputs.end())
      return found->second;
    return {{}, source->getName(), "Standard input conversion requires a scene-referred aces_interchange role"};
  }
  return {};
}
} // namespace cycles_houdini
