// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <functional>
#include <initializer_list>
#include <map>
#include <string>
#include <vector>

namespace cycles_houdini {
// Authored host semantics. Socket names remain the versioned engine names;
// visibility never removes/reindexes saved inputs or their connections.
struct ShaderUiTest {
  std::string parameter, value;
  bool equal{true};
};
using ShaderUiClause = std::vector<ShaderUiTest>;
using ShaderUiHidden = std::vector<ShaderUiClause>;
inline ShaderUiHidden ui_when(const char *parameter, std::initializer_list<const char *> values) {
  ShaderUiHidden result;
  for (const auto *value : values)
    result.push_back({{parameter, value, true}});
  return result;
}
inline ShaderUiHidden ui_unless(const char *parameter, std::initializer_list<const char *> values) {
  ShaderUiClause clause;
  for (const auto *value : values)
    clause.push_back({parameter, value, false});
  return {clause};
}
inline ShaderUiHidden native_shader_hidden(const std::string &kind, const std::string &socket) {
  if (kind == "mix_color") {
    if (socket == "factor_mode" || socket == "a_vector" || socket == "b_vector")
      return ui_unless("data_type", {"vector"});
    if (socket == "a_float" || socket == "b_float")
      return ui_unless("data_type", {"float"});
    if (socket == "a" || socket == "b" || socket == "blend_type" || socket == "use_clamp_result")
      return ui_unless("data_type", {"color"});
    if (socket == "fac")
      return {{{"data_type", "vector", true}, {"factor_mode", "non_uniform", true}}};
    if (socket == "fac_vector")
      return {{{"data_type", "vector", false}}, {{"factor_mode", "non_uniform", false}}};
  } else if (kind == "boolean_math") {
    if (socket == "boolean2")
      return ui_when("math_type", {"not"});
  } else if (kind == "integer_math") {
    if (socket == "value2")
      return ui_when("math_type", {"absolute", "negate", "sign"});
    if (socket == "value3")
      return ui_unless("math_type", {"multiply_add"});
  } else if (kind == "math") {
    if (socket == "value2")
      return ui_when("math_type",
                     {"absolute", "arccosine", "arcsine", "arctangent", "ceil",        "cosh",    "cosine",
                      "degrees",  "exponent",  "floor",   "fraction",   "inversesqrt", "radians", "round",
                      "sign",     "sine",      "sinh",    "sqrt",       "tangent",     "tanh",    "trunc"});
    if (socket == "value3")
      return ui_unless("math_type", {"compare", "multiply_add", "smoothmax", "smoothmin", "wrap"});
  } else if (kind == "vector_math") {
    if (socket == "vector2")
      return ui_when("math_type", {"absolute", "ceil", "cosine", "floor", "fraction", "length", "normalize", "round",
                                   "scale", "sign", "sine", "tangent"});
    if (socket == "vector3")
      return ui_unless("math_type", {"faceforward", "multiply_add", "wrap"});
    if (socket == "scale")
      return ui_unless("math_type", {"scale", "refract"});
  } else if (kind == "map_range" || kind == "vector_map_range") {
    if (socket == "steps")
      return ui_unless("range_type", {"stepped"});
    if (socket == "clamp" || socket == "use_clamp")
      return ui_when("range_type", {"smoothstep", "smootherstep"});
  } else if (kind == "noise_texture" || kind == "voronoi_texture" || kind == "white_noise_texture") {
    if (socket == "vector")
      return ui_when("dimensions", {"1D"});
    if (socket == "w")
      return ui_unless("dimensions", {"1D", "4D"});
    if (kind == "noise_texture") {
      if (socket == "offset")
        return ui_when("type", {"fBM", "multifractal"});
      if (socket == "gain")
        return ui_unless("type", {"hybrid_multifractal", "ridged_multifractal"});
      if (socket == "use_normalize")
        return ui_unless("type", {"fBM"});
    } else if (kind == "voronoi_texture") {
      if (socket == "detail" || socket == "roughness" || socket == "lacunarity" || socket == "use_normalize")
        return ui_when("feature", {"n_sphere_radius"});
      if (socket == "smoothness")
        return ui_unless("feature", {"smooth_f1"});
      if (socket == "metric" || socket == "exponent") {
        auto result = ui_when("feature", {"distance_to_edge", "n_sphere_radius"});
        result.push_back({{"dimensions", "1D", true}});
        if (socket == "exponent")
          result.push_back({{"metric", "minkowski", false}});
        return result;
      }
    }
  } else if (kind == "gabor_texture") {
    if (socket == "orientation_2d")
      return ui_unless("type", {"2D"});
    if (socket == "orientation_3d")
      return ui_unless("type", {"3D"});
  } else if (kind == "wave_texture") {
    if (socket == "bands_direction")
      return ui_unless("wave_type", {"bands"});
    if (socket == "rings_direction")
      return ui_unless("wave_type", {"rings"});
  } else if (kind == "vector_rotate") {
    if (socket == "rotation")
      return ui_unless("rotate_type", {"euler_xyz"});
    if (socket == "axis")
      return ui_unless("rotate_type", {"axis"});
    if (socket == "angle")
      return ui_when("rotate_type", {"euler_xyz"});
  } else if (kind == "mapping") {
    if (socket == "location")
      return ui_when("mapping_type", {"normal", "vector"});
  } else if (kind == "image_texture") {
    if (socket == "projection_blend")
      return ui_unless("projection", {"box"});
  } else if (kind == "tangent") {
    if (socket == "attribute")
      return ui_unless("direction_type", {"uv_map"});
    if (socket == "axis")
      return ui_unless("direction_type", {"radial"});
  } else if (kind == "normal_map" || kind == "vector_displacement") {
    if (socket == "attribute" || (kind == "normal_map" && socket == "base"))
      return ui_unless("space", {"tangent"});
  } else if (kind == "metallic_bsdf") {
    if (socket == "color" || socket == "edge_tint")
      return ui_unless("fresnel_type", {"f82"});
    if (socket == "ior" || socket == "k")
      return ui_unless("fresnel_type", {"physical_conductor"});
  } else if (kind == "principled_hair_bsdf") {
    if (socket == "color")
      return ui_unless("parametrization", {"Direct coloring"});
    if (socket == "melanin" || socket == "melanin_redness" || socket == "tint" || socket == "random_color")
      return ui_unless("parametrization", {"Melanin concentration"});
    if (socket == "absorption_coefficient")
      return ui_unless("parametrization", {"Absorption coefficient"});
    if (socket == "radial_roughness" || socket == "coat")
      return ui_unless("model", {"Chiang"});
    if (socket == "aspect_ratio" || socket == "R" || socket == "TT" || socket == "TRT")
      return ui_unless("model", {"Huang"});
  } else if (kind == "scatter_volume" || kind == "volume_coefficients") {
    if (socket == "anisotropy")
      return ui_when("phase", {"Fournier-Forand", "Mie", "Rayleigh"});
    if (socket == "IOR" || socket == "backscatter")
      return ui_unless("phase", {"Fournier-Forand"});
    if (socket == "alpha")
      return ui_unless("phase", {"Draine"});
    if (socket == "diameter")
      return ui_unless("phase", {"Mie"});
  }
  return {};
}
inline bool shader_ui_active(const ShaderUiHidden &hidden,
                             const std::function<std::string(const std::string &)> &value) {
  for (const auto &clause : hidden) {
    bool matches = true;
    for (const auto &test : clause)
      if ((value(test.parameter) == test.value) != test.equal) {
        matches = false;
        break;
      }
    if (matches)
      return false;
  }
  return true;
}
inline std::string shader_ui_hide_when(const ShaderUiHidden &hidden) {
  std::string result;
  for (const auto &clause : hidden) {
    result += "{ ";
    for (const auto &test : clause) {
      result += test.parameter + (test.equal ? " == " : " != ");
      result += '"';
      result += test.value;
      result += "\" ";
    }
    result += "} ";
  }
  return result;
}
struct ShaderUiPanel {
  std::string name, label;
  std::vector<std::string> sockets;
  bool closed{true};
};
inline std::vector<ShaderUiPanel> native_shader_panels(const std::string &kind) {
  if (kind == "principled_bsdf")
    return {
        {"diffuse", "Diffuse", {"diffuse_roughness"}},
        {"subsurface",
         "Subsurface",
         {"subsurface_method", "subsurface_weight", "subsurface_radius", "subsurface_scale", "subsurface_ior",
          "subsurface_anisotropy"}},
        {"specular",
         "Specular",
         {"distribution", "specular_ior_level", "specular_tint", "anisotropic", "anisotropic_rotation", "tangent"}},
        {"transmission",
         "Transmission",
         {"transmission_weight", "thin_wall", "transmission_dispersion_scale", "transmission_dispersion_abbe_number"}},
        {"coat", "Coat", {"coat_weight", "coat_roughness", "coat_ior", "coat_tint", "coat_normal"}},
        {"sheen", "Sheen", {"sheen_weight", "sheen_roughness", "sheen_tint"}},
        {"emission", "Emission", {"emission_color", "emission_strength"}},
        {"thin_film", "Thin Film", {"thin_film_thickness", "thin_film_ior"}}};
  if (kind == "glass_bsdf" || kind == "metallic_bsdf")
    return {{"thin_film", "Thin Film", {"thin_film_thickness", "thin_film_ior"}}};
  if (kind == "volume_coefficients")
    return {{"absorption", "Absorption", {"absorption_coeffs"}, false},
            {"scatter",
             "Scatter",
             {"phase", "scatter_coeffs", "anisotropy", "IOR", "backscatter", "alpha", "diameter"},
             false},
            {"emission", "Emission", {"emission_coeffs"}, false}};
  return {};
}
inline bool native_shader_is_mix(const std::string &kind) {
  return kind == "mix" || kind == "mix_closure" || kind == "mix_color" || kind == "mix_float" || kind == "mix_vector" ||
         kind == "mix_vector_non_uniform";
}
inline std::string native_shader_short_label(const std::string &kind, const std::string &socket,
                                             const std::string &original) {
  if (kind == "mix_closure" || kind == "add_closure") {
    if (socket == "closure1")
      return "Shader 1";
    if (socket == "closure2")
      return "Shader 2";
  }
  if (native_shader_is_mix(kind)) {
    if (socket == "fac" || socket == "fac_vector")
      return "Factor";
    if (socket == "a_float" || socket == "a_vector")
      return "A";
    if (socket == "b_float" || socket == "b_vector")
      return "B";
    if (socket == "color1")
      return "A";
    if (socket == "color2")
      return "B";
    if (socket == "mix_type" || socket == "blend_type")
      return "Blend Mode";
    if (socket == "use_clamp")
      return kind == "mix" ? "Clamp Result" : "Clamp Factor";
    if (socket == "use_clamp_result")
      return "Clamp Result";
  }
  if (socket == "math_type")
    return "Operation";
  if (kind == "principled_bsdf") {
    const std::map<std::string, std::string> labels = {{"diffuse_roughness", "Roughness"},
                                                       {"subsurface_weight", "Weight"},
                                                       {"subsurface_radius", "Radius"},
                                                       {"subsurface_scale", "Scale"},
                                                       {"subsurface_ior", "IOR"},
                                                       {"subsurface_anisotropy", "Anisotropy"},
                                                       {"subsurface_method", "Method"},
                                                       {"distribution", "Distribution"},
                                                       {"specular_ior_level", "IOR Level"},
                                                       {"specular_tint", "Tint"},
                                                       {"anisotropic", "Anisotropy"},
                                                       {"anisotropic_rotation", "Rotation"},
                                                       {"transmission_weight", "Weight"},
                                                       {"transmission_dispersion_scale", "Dispersion Scale"},
                                                       {"transmission_dispersion_abbe_number", "Abbe Number"},
                                                       {"coat_weight", "Weight"},
                                                       {"coat_roughness", "Roughness"},
                                                       {"coat_ior", "IOR"},
                                                       {"coat_tint", "Tint"},
                                                       {"coat_normal", "Normal"},
                                                       {"sheen_weight", "Weight"},
                                                       {"sheen_roughness", "Roughness"},
                                                       {"sheen_tint", "Tint"},
                                                       {"emission_color", "Color"},
                                                       {"emission_strength", "Strength"},
                                                       {"thin_film_thickness", "Thickness"},
                                                       {"thin_film_ior", "IOR"}};
    if (const auto found = labels.find(socket); found != labels.end())
      return found->second;
  }
  if ((kind == "glass_bsdf" || kind == "metallic_bsdf") && socket == "thin_film_thickness")
    return "Thickness";
  if ((kind == "glass_bsdf" || kind == "metallic_bsdf") && socket == "thin_film_ior")
    return "IOR";
  return original;
}
} // namespace cycles_houdini
