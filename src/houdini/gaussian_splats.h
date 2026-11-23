// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "../core/gaussian_splats.h"
#include "named_attribute.h"
#include "gaussian_splat_attribute.h"

namespace cycles_houdini {
class NativeGaussianSplats {
  struct Field {
    const GA_Attribute *attribute{};
    int components{};
    std::string name;
    Field(const GU_Detail &source, const char *token, int size)
        : attribute(named_attribute(source, token, false)), components(size), name(token) {}
    template <std::size_t N>
    std::array<float, N> read(GA_Offset point, GA_Offset primitive, std::array<float, N> fallback = {}) const {
      if (!attribute) {
        return fallback;
      }
      if (attribute->getStorageClass() != GA_STORECLASS_FLOAT || attribute->getTupleSize() != components ||
          !attribute->getAIFTuple()) {
        throw std::invalid_argument("Gaussian " + name + " requires a float tuple of size " +
                                    std::to_string(components));
      }
      const auto owner = attribute->getOwner();
      const auto offset = owner == GA_ATTRIB_POINT       ? point
                          : owner == GA_ATTRIB_PRIMITIVE ? primitive
                                                         : GA_DETAIL_OFFSET;
      if (offset == GA_INVALID_OFFSET ||
          !attribute->getAIFTuple()->get(attribute, offset, fallback.data(), components)) {
        throw std::invalid_argument("Gaussian " + name + " has no unambiguous value on this point");
      }
      for (const auto v : fallback) {
        if (!std::isfinite(v)) {
          throw std::invalid_argument("Gaussian " + name + " contains a nonfinite value");
        }
      }
      return fallback;
    }
  };
  Field alpha_, scale_, pscale_, orient_, rest_, color_, red_, green_, blue_;
  struct Raw {
    NativeGaussianField dc, rest, scale, rotation, opacity;
    explicit Raw(const GU_Detail &source)
        : dc(source, "f_dc", 3, true), rest(source, "f_rest", 45, false), scale(source, "scale", 3, true),
          rotation(source, "rot", 4, true), opacity(source, "opacity", 1, true) {}
    void read(GaussianSplatData &data, GA_Offset point) const {
      // Original PLY coefficients use log scales and a wxyz quaternion.
      // Preserve the existing runtime's xyzw and unquantized SH representation.
      std::array<float, 3> axes;
      for (int i = 0; i < 3; ++i) {
        axes[i] = std::exp(scale.get(point, i));
        if (!std::isfinite(axes[i]) || axes[i] <= 0) {
          throw std::invalid_argument("Gaussian splat scale must be positive and representable");
        }
      }
      data.scales.push_back({axes[0], axes[1], axes[2]});
      data.rotations.push_back(normalized_splat_quaternion(
          {rotation.get(point, 1), rotation.get(point, 2), rotation.get(point, 3), rotation.get(point, 0)}));
      const double logit = opacity.get(point);
      const float alpha = float(logit >= 0 ? 1 / (1 + std::exp(-logit)) : std::exp(logit) / (1 + std::exp(logit)));
      data.radiance.push_back({dc.get(point, 0), dc.get(point, 1), dc.get(point, 2), alpha});
      if (rest.present) {
        std::array<Float3, 15> coefficients;
        for (int i = 0; i < 15; ++i) {
          coefficients[i] = {rest.get(point, i), rest.get(point, 15 + i), rest.get(point, 30 + i)};
        }
        data.harmonics.push_back(coefficients);
      }
    }
  };
  std::optional<Raw> raw_;
  bool srgb_{};
  mutable SplatHarmonicRotation harmonic_rotation_;

public:
  bool harmonics{};
  NativeGaussianSplats(const GU_Detail &source)
      : alpha_(source, "GS_Alpha", 1), scale_(source, "scale", 3), pscale_(source, "pscale", 1),
        orient_(source, "orient", 4), rest_(source, "restorient", 4), color_(source, "Cd", 3),
        red_(source, "GS_SPH_R", 16), green_(source, "GS_SPH_G", 16), blue_(source, "GS_SPH_B", 16) {
    // Prepared Bake fields take precedence when the original PLY fields remain.
    // Keep their established defaults, attribute owners and color encoding.
    if (!alpha_.attribute) {
      raw_.emplace(source);
      harmonics = raw_->rest.present;
      return;
    }
    harmonics = red_.attribute || green_.attribute || blue_.attribute;
    if (harmonics && !(red_.attribute && green_.attribute && blue_.attribute)) {
      throw std::invalid_argument("Gaussian harmonics require GS_SPH_R, GS_SPH_G and GS_SPH_B together");
    }
    // Cd follows Houdini's working-space convention. This explicit detail
    // override also supports a Bake GSplats SOP with Linearize Color off.
    if (const auto *a = source.findGlobalAttribute("cycles_gsplat_srgb")) {
      GA_ROHandleI value(a);
      if (!value.isValid()) {
        throw std::invalid_argument("cycles_gsplat_srgb must be an integer detail attribute");
      }
      srgb_ = value.get(GA_DETAIL_OFFSET) != 0;
    }
  }
  static bool present(const GU_Detail &source) { return named_attribute(source, "GS_Alpha", false) != nullptr; }
  void initialize(GaussianSplatData &data) const { data.scene_linear_color = !raw_ && !harmonics && !srgb_; }
  void read(GaussianSplatData &data, GA_Offset point, GA_Offset primitive) const {
    if (raw_) {
      raw_->read(data, point);
      return;
    }
    const auto alpha = alpha_.read<1>(point, primitive)[0];
    if (alpha < 0 || alpha > 1) {
      throw std::invalid_argument("GS_Alpha must be in 0..1");
    }
    const auto scale = scale_.read<3>(point, primitive, {1, 1, 1});
    const auto multiplier = pscale_.read<1>(point, primitive, {1})[0];
    if (multiplier < 0 || scale[0] < 0 || scale[1] < 0 || scale[2] < 0) {
      throw std::invalid_argument("Gaussian scale and pscale must be nonnegative");
    }
    data.scales.push_back({scale[0] * multiplier, scale[1] * multiplier, scale[2] * multiplier});
    const auto orient = normalized_splat_quaternion(orient_.read<4>(point, primitive, {0, 0, 0, 1}));
    data.rotations.push_back(orient);
    std::array<float, 4> radiance;
    if (harmonics) {
      const auto red = red_.read<16>(point, primitive), green = green_.read<16>(point, primitive),
                 blue = blue_.read<16>(point, primitive);
      radiance = {red[0], green[0], blue[0], alpha};
      std::array<Float3, 15> rest;
      for (int i = 0; i < 15; ++i) {
        rest[i] = {red[i + 1], green[i + 1], blue[i + 1]};
      }
      const auto original = normalized_splat_quaternion(rest_.read<4>(point, primitive, orient));
      const auto delta =
          normalized_splat_quaternion(splat_quaternion_product(orient, splat_quaternion_inverse(original)));
      data.harmonics.push_back(harmonic_rotation_(rest, delta));
    } else {
      const auto color = color_.read<3>(point, primitive, {1, 1, 1});
      radiance = {color[0], color[1], color[2], alpha};
      if (srgb_) {
        for (int c = 0; c < 3; ++c) {
          radiance[c] = (radiance[c] - .5f) / .282094792f;
        }
      }
    }
    data.radiance.push_back(radiance);
  }
};
} // namespace cycles_houdini
