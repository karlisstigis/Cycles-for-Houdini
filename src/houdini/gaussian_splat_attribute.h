// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "scene_adapter.h"
#include "named_attribute.h"
#include "../core/color_internal.h"
#include <GA/GA_Handle.h>
#include <GU/GU_Detail.h>
#include <array>
#include <cmath>
#include <stdexcept>

namespace cycles_houdini {
// The selected representation and owners mirror NativeGaussianSplats capture.
// Ordinary point caches need only remember resolution, not unrelated attributes.
enum class NativeGaussianSplatFormat { None, Raw, Prepared };
inline NativeGaussianSplatFormat native_gaussian_splat_format(const GU_Detail &source, const GeometryRequest &request) {
  if (request.point_render_mode == NativePointRenderMode::Spheres)
    return NativeGaussianSplatFormat::None;
  if (named_attribute(source, "GS_Alpha", false))
    return NativeGaussianSplatFormat::Prepared;
  if (request.point_render_mode == NativePointRenderMode::GaussianSplats)
    return NativeGaussianSplatFormat::Raw;
  // These markers are deliberate. Incomplete marker data must fail rather
  // than silently become spheres; ordinary scale/orient/opacity are not markers.
  for (const auto *name : {"f_dc", "f_dc_0", "f_dc_1", "f_dc_2", "GS_SPH_R", "GS_SPH_G", "GS_SPH_B"})
    if (source.findPointAttribute(name))
      return NativeGaussianSplatFormat::Raw;
  return NativeGaussianSplatFormat::None;
}
inline bool native_gaussian_splats(const GU_Detail &source, const GeometryRequest &request) {
  return native_gaussian_splat_format(source, request) != NativeGaussianSplatFormat::None;
}
template <class Visitor>
void gaussian_splat_fields(const GU_Detail &source, NativeGaussianSplatFormat format, Visitor &&visit) {
  if (format == NativeGaussianSplatFormat::None)
    return;
  if (format == NativeGaussianSplatFormat::Prepared) {
    for (const auto *name : {"GS_Alpha", "scale", "pscale", "orient"})
      visit(name, named_attribute(source, name, false));
    const bool harmonics = named_attribute(source, "GS_SPH_R", false) || named_attribute(source, "GS_SPH_G", false) ||
                           named_attribute(source, "GS_SPH_B", false);
    if (harmonics) {
      for (const auto *name : {"GS_SPH_R", "GS_SPH_G", "GS_SPH_B", "restorient"})
        visit(name, named_attribute(source, name, false));
    } else
      visit("Cd", named_attribute(source, "Cd", false));
    // The prepared constructor validates this explicit encoding override.
    visit("cycles_gsplat_srgb", source.findGlobalAttribute("cycles_gsplat_srgb"));
    return;
  }
  for (const auto &[name, count] : {std::pair{"f_dc", 3}, {"f_rest", 45}, {"scale", 3}, {"rot", 4}, {"opacity", 1}}) {
    const auto *grouped = source.findPointAttribute(name);
    visit(name, grouped);
    if (grouped)
      continue;
    // NativeGaussianField prefers a grouped tuple over expanded components.
    // Missing optional rest coefficients require one absence stamp, not 45.
    bool expanded = false;
    for (int i = 0; i < count && !expanded; ++i)
      expanded = source.findPointAttribute((std::string(name) + "_" + std::to_string(i)).c_str()) != nullptr;
    if (expanded)
      for (int i = 0; i < count; ++i) {
        const auto component = std::string(name) + "_" + std::to_string(i);
        visit(component.c_str(), source.findPointAttribute(component.c_str()));
      }
  }
}
inline std::string gaussian_splat_color_identity(const GU_Detail &source, const GeometryRequest &request) {
  if (native_gaussian_splat_format(source, request) == NativeGaussianSplatFormat::Prepared &&
      !named_attribute(source, "GS_SPH_R", false))
    return current_color_configuration()->policy().render_policy_id;
  return {};
}
inline void gaussian_splat_identifiers(std::vector<std::int64_t> &result, const GU_Detail &source,
                                       const GeometryRequest &request) {
  const auto format = native_gaussian_splat_format(source, request);
  result.push_back(std::int64_t(request.point_render_mode));
  result.push_back(std::int64_t(format));
  if (format == NativeGaussianSplatFormat::None)
    return;
  gaussian_splat_fields(source, format, [&](const char *name, const GA_Attribute *attribute) {
    std::uint64_t field = 14695981039346656037ull;
    for (const auto *c = name; *c; ++c) {
      field ^= static_cast<unsigned char>(*c);
      field *= 1099511628211ull;
    }
    result.push_back(static_cast<std::int64_t>(field));
    result.push_back(attribute != nullptr);
    if (attribute)
      result.insert(result.end(), {attribute->getDataId(), attribute->getOwner(), attribute->getStorageClass(),
                                   attribute->getTupleSize()});
  });
  const auto color = gaussian_splat_color_identity(source, request);
  result.push_back(std::int64_t(color.size()));
  for (unsigned char c : color)
    result.push_back(c);
}
struct NativeGaussianField {
  GA_ROHandleF tuple;
  std::vector<GA_ROHandleF> components;
  int size{};
  bool present{};
  NativeGaussianField() = default;
  NativeGaussianField(const GU_Detail &source, const char *name, int count, bool required, bool split = true)
      : size(count) {
    const auto bind = [&](const GA_Attribute *attr, int n) {
      if (!attr || attr->getStorageClass() != GA_STORECLASS_FLOAT || attr->getTupleSize() != n)
        throw std::invalid_argument(std::string("Gaussian splat point ") + name + " requires " + std::to_string(n) +
                                    " float component(s)");
      return GA_ROHandleF(attr);
    };
    if (const auto *attribute = source.findPointAttribute(name)) {
      tuple = bind(attribute, count);
      present = true;
      return;
    }
    bool any = false;
    if (split)
      for (int i = 0; i < count; ++i)
        any |= source.findPointAttribute((std::string(name) + "_" + std::to_string(i)).c_str()) != nullptr;
    if (any) {
      for (int i = 0; i < count; ++i)
        components.push_back(bind(source.findPointAttribute((std::string(name) + "_" + std::to_string(i)).c_str()), 1));
      present = true;
    }
    if (required && !present)
      throw std::invalid_argument(std::string("Gaussian splat point attribute is missing: ") + name);
  }
  float get(GA_Offset offset, int component = 0) const {
    const float value = components.empty() ? tuple.get(offset, component) : components.at(component).get(offset);
    if (!std::isfinite(value))
      throw std::invalid_argument("Gaussian splat point attribute contains a non-finite value");
    return value;
  }
};
} // namespace cycles_houdini
