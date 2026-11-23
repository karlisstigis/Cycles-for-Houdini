// SPDX-License-Identifier: Apache-2.0
#include "shader_graph.h"
#include "attribute_names.h"
#include "owned_ramp.h"
#include "shader_curves.h"
#include "shader_switches.h"
#include <algorithm>
#include <cmath>
#include <functional>
#include <limits>
#include <numbers>
#include <set>
#include <stdexcept>
#include <type_traits>
#include "scene/shader_nodes.h"
#include "util/md5.h"

namespace cycles_houdini {
namespace {
using Socket = ccl::SocketType;
const std::set<std::string> &supported() {
  static const std::set<std::string> names = {"add_closure",
                                              "aov_output",
                                              "attribute",
                                              "color",
                                              "combine_xyz",
                                              "diffuse_bsdf",
                                              "emission",
                                              "fresnel",
                                              "geometry",
                                              "layer_weight",
                                              "light_path",
                                              "math",
                                              "mix",
                                              "mix_closure",
                                              "mix_float",
                                              "normal",
                                              "object_info",
                                              "principled_bsdf",
                                              "rgb_to_bw",
                                              "separate_xyz",
                                              "value",
                                              "vector_math",
                                              "displacement",
                                              "vector_displacement",
                                              "image_texture",
                                              "environment_texture",
                                              "sky_texture",
                                              "uvmap",
                                              "mapping",
                                              "bump",
                                              "normal_map",
                                              "transparent_bsdf",
                                              "glass_bsdf",
                                              "holdout",
                                              "noise_texture",
                                              "ambient_occlusion",
                                              "bevel",
                                              "blackbody",
                                              "brick_texture",
                                              "brightness_contrast",
                                              "camera_info",
                                              "checker_texture",
                                              "clamp",
                                              "combine_color",
                                              "gabor_texture",
                                              "gamma",
                                              "glossy_bsdf",
                                              "gradient_texture",
                                              "hair_bsdf",
                                              "hair_info",
                                              "hsv",
                                              "invert",
                                              "light_falloff",
                                              "ies_light",
                                              "magic_texture",
                                              "map_range",
                                              "metallic_bsdf",
                                              "mix_color",
                                              "mix_vector",
                                              "mix_vector_non_uniform",
                                              "point_info",
                                              "principled_hair_bsdf",
                                              "radial_tiling",
                                              "ray_portal_bsdf",
                                              "raycast",
                                              "refraction_bsdf",
                                              "scene_time",
                                              "separate_color",
                                              "set_normal",
                                              "sheen_bsdf",
                                              "subsurface_scattering",
                                              "tangent",
                                              "texture_coordinate",
                                              "toon_bsdf",
                                              "translucent_bsdf",
                                              "vector_map_range",
                                              "vector_rotate",
                                              "vector_transform",
                                              "vertex_color",
                                              "voronoi_texture",
                                              "wave_texture",
                                              "wavelength",
                                              "white_noise_texture",
                                              "wireframe",
                                              "owned_ramp",
                                              "float_curve",
                                              "rgb_curves",
                                              "vector_curves",
                                              "boolean_math",
                                              "integer_math",
                                              "get_vector_component",
                                              "switch_float",
                                              "switch_integer",
                                              "switch_boolean",
                                              "switch_vector",
                                              "switch_color",
                                              "switch_shader",
                                              "index_switch_float",
                                              "index_switch_integer",
                                              "index_switch_boolean",
                                              "index_switch_vector",
                                              "index_switch_color",
                                              "index_switch_shader",
                                              "enable_output_float",
                                              "enable_output_integer",
                                              "enable_output_boolean",
                                              "enable_output_vector",
                                              "enable_output_color",
                                              "enable_output_shader",
                                              "warning"};
  return names;
}
const std::set<std::string> &volume_nodes() {
  static const std::set<std::string> names = {"absorption_volume", "principled_volume", "scatter_volume", "volume_info",
                                              "volume_coefficients"};
  return names;
}
const std::set<std::string> &surface_closures() {
  static const std::set<std::string> names = {"diffuse_bsdf",          "principled_bsdf", "glass_bsdf",
                                              "transparent_bsdf",      "holdout",         "glossy_bsdf",
                                              "metallic_bsdf",         "hair_bsdf",       "principled_hair_bsdf",
                                              "ray_portal_bsdf",       "refraction_bsdf", "sheen_bsdf",
                                              "subsurface_scattering", "toon_bsdf",       "translucent_bsdf"};
  return names;
}
void identifier(const std::string &value, const char *kind) {
  if (value.empty() || value.size() > 256 || value.find('\0') != std::string::npos)
    throw std::invalid_argument(std::string("Invalid shader ") + kind + " identifier");
}
const ccl::NodeType *definition(const std::string &id) {
  if (!id.starts_with("cycles/") || !id.ends_with("/1"))
    throw std::invalid_argument("Unknown shader definition version: " + id);
  const auto name = id.substr(7, id.size() - 9);
  if (!supported().contains(name) && !volume_nodes().contains(name) && name != "background_shader")
    throw std::invalid_argument("Shader definition needs an authored/validated adapter: " + id);
  const auto *type = ccl::NodeType::find(ccl::ustring(name == "owned_ramp" ? "rgb_ramp" : name));
  if (!type || type->type != ccl::NodeType::SHADER || !type->create)
    throw std::invalid_argument("Unavailable shader definition: " + id);
  return type;
}
void set_parameter(ccl::ShaderNode &node, const std::string &name, const ShaderValue &value) {
  identifier(name, "parameter");
  if ((node.type->name == ccl::ustring("float_curve") || node.type->name == ccl::ustring("rgb_curves") ||
       node.type->name == ccl::ustring("vector_curves")) &&
      name != "fac" && name != "value")
    throw std::invalid_argument("Shader-curve tables and range are worker-prepared from owned controls");
  const auto *socket = node.type->find_input(ccl::ustring(name));
  if (!socket || (socket->flags & Socket::INTERNAL))
    throw std::invalid_argument("Unknown/internal shader parameter: " + name);
  if (node.type->name == ccl::ustring("ies_light") && name == "ies") {
    const auto *text = std::get_if<std::string>(&value);
    if (!text || text->find('\0') != std::string::npos)
      throw std::invalid_argument("IES text requires an owned string without embedded NULs");
    static_cast<ccl::IESLightNode &>(node).owned_ies =
        text->empty() ? nullptr : std::make_shared<const std::string>(*text);
    return;
  }
  if (node.type->name == ccl::ustring("bump") && (name == "sample_center" || name == "sample_x" || name == "sample_y"))
    throw std::invalid_argument("Bump sample parameters are reserved for engine derivative evaluation");
  const auto mismatch = [&]() {
    throw std::invalid_argument("Shader parameter type mismatch: " + name + " expects " +
                                Socket::type_name(socket->type).string());
  };
  std::visit(
      [&](const auto &parameter) {
        using T = std::decay_t<decltype(parameter)>;
        if constexpr (std::is_same_v<T, bool>) {
          if (socket->type != Socket::BOOLEAN)
            mismatch();
          node.set(*socket, parameter);
        } else if constexpr (std::is_same_v<T, int>) {
          if (socket->type == Socket::INT)
            node.set(*socket, parameter);
          else if (socket->type == Socket::UINT && parameter >= 0)
            node.set(*socket, static_cast<unsigned>(parameter));
          else if (socket->type == Socket::FLOAT)
            node.set(*socket, static_cast<float>(parameter));
          else
            mismatch();
        } else if constexpr (std::is_same_v<T, float>) {
          if (socket->type != Socket::FLOAT)
            mismatch();
          if (!std::isfinite(parameter))
            throw std::invalid_argument("Nonfinite shader parameter: " + name);
          node.set(*socket, parameter);
        } else if constexpr (std::is_same_v<T, std::array<float, 3>>) {
          if (!Socket::is_float3(socket->type))
            mismatch();
          for (const auto entry : parameter)
            if (!std::isfinite(entry))
              throw std::invalid_argument("Nonfinite shader vector: " + name);
          node.set(*socket, ccl::make_float3(parameter[0], parameter[1], parameter[2]));
        } else if constexpr (std::is_same_v<T, std::string>) {
          if (parameter.find('\0') != std::string::npos)
            throw std::invalid_argument("Embedded NUL in shader parameter: " + name);
          if (socket->type == Socket::STRING)
            node.set(*socket, ccl::ustring(parameter));
          else if (socket->type == Socket::ENUM) {
            if (!socket->enum_values || !socket->enum_values->exists(ccl::ustring(parameter)))
              throw std::invalid_argument("Unknown shader enum token: " + name + " = " + parameter);
            node.set(*socket, (*socket->enum_values)[ccl::ustring(parameter)]);
          } else
            mismatch();
        } else if constexpr (std::is_same_v<T, std::vector<int>>) {
          if (socket->type != Socket::INT_ARRAY)
            mismatch();
          if (parameter.size() > 8999)
            throw std::invalid_argument("Too many texture tile IDs");
          ccl::array<int> entries;
          entries.resize(parameter.size());
          std::copy(parameter.begin(), parameter.end(), entries.begin());
          node.set(*socket, entries);
        }
      },
      value);
}
bool compatible(Socket::Type from, Socket::Type to) {
  const auto numeric = [](Socket::Type type) {
    return type == Socket::FLOAT || type == Socket::INT || Socket::is_float3(type);
  };
  return from == to || (numeric(from) && (numeric(to) || to == Socket::CLOSURE));
}
} // namespace

std::vector<std::string> surface_graph_definitions() {
  std::vector<std::string> result;
  for (const auto &name : supported())
    if (name != "owned_ramp")
      result.push_back("cycles/" + name + "/1");
  return result;
}
std::vector<std::string> volume_graph_definitions() {
  auto result = surface_graph_definitions();
  // Surface BSDFs cannot contribute to a volume closure. Emission and the
  // arithmetic/input nodes can be used in either domain.
  std::erase_if(result, [](const auto &id) {
    return surface_closures().contains(id.substr(7, id.size() - 9)) || id == "cycles/displacement/1" ||
           id == "cycles/vector_displacement/1" || id == "cycles/aov_output/1";
  });
  for (const auto &name : volume_nodes())
    result.push_back("cycles/" + name + "/1");
  return result;
}
std::vector<std::string> shader_graph_definitions() {
  auto result = surface_graph_definitions();
  for (const auto &name : volume_nodes())
    result.push_back("cycles/" + name + "/1");
  result.push_back("cycles/background_shader/1");
  return result;
}
std::vector<std::string> world_graph_definitions() {
  auto result = surface_graph_definitions();
  const std::set<std::string> object_only = {"attribute",   "vertex_color",      "uvmap",        "tangent",
                                             "bump",        "normal_map",        "displacement", "vector_displacement",
                                             "object_info", "hair_info",         "point_info",   "wireframe",
                                             "bevel",       "ambient_occlusion", "raycast",      "aov_output"};
  std::erase_if(result, [&](const auto &id) {
    const auto name = id.substr(7, id.size() - 9);
    return surface_closures().contains(name) || object_only.contains(name);
  });
  for (const auto &name : volume_nodes())
    if (name != "volume_info")
      result.push_back("cycles/" + name + "/1");
  result.push_back("cycles/background_shader/1");
  return result;
}
std::vector<std::string> light_graph_definitions() {
  auto result = world_graph_definitions();
  std::erase_if(result, [](const auto &id) {
    return volume_nodes().contains(id.substr(7, id.size() - 9)) || id == "cycles/background_shader/1";
  });
  result.push_back("cycles/uvmap/1");
  return result;
}
namespace {
ShaderGraphData missing_image_graph(const ShaderGraphData &source, const std::string &image, bool &displaced,
                                    bool &normals, bool &gated, bool gate_emission) {
  auto result = source;
  std::set<std::string> affected{image};
  bool changed = true;
  while (changed) {
    changed = false;
    for (const auto &link : source.links)
      if (affected.contains(link.from.node))
        changed |= affected.insert(link.to.node).second;
  }
  displaced = result.displacement && affected.contains(result.displacement->node);
  if (displaced) {
    result.displacement.reset();
    result.displacement_method = DisplacementMethod::Bump;
  }
  std::set<std::string> bad_normals;
  for (const auto &node : source.nodes)
    if (affected.contains(node.id) && (node.definition == "cycles/bump/1" || node.definition == "cycles/normal_map/1"))
      bad_normals.insert(node.id);
  normals = !bad_normals.empty();
  // Falling back to the geometric normal is safer than interpreting a
  // diagnostic color as a normal/height. Other surface inputs stay connected.
  std::erase_if(result.links,
                [&](const auto &link) { return bad_normals.contains(link.from.node) || link.to.node == image; });
  std::string alpha = "_missing_image_alpha";
  while (std::any_of(result.nodes.begin(), result.nodes.end(), [&](const auto &node) { return node.id == alpha; }))
    alpha += "_";
  bool need_alpha = false;
  for (auto &link : result.links)
    if (link.from.node == image && link.from.socket == "alpha") {
      link.from = {alpha, "value"};
      need_alpha = true;
    }
  for (auto &node : result.nodes)
    if (node.id == image) {
      node.definition = "cycles/color/1";
      node.parameters = {{"value", std::array<float, 3>{1, 0, 1}}};
    }
  if (need_alpha)
    result.nodes.push_back({alpha, "cycles/value/1", {{"value", 1.0f}}});
  // The diagnostic color must stay visible without lighting the scene.
  // Gate only links from the unavailable branch into emission inputs, so
  // independent valid emission, BSDF inputs and displacement are unchanged.
  gated = false;
  if (gate_emission) {
    static const std::map<std::string, std::set<std::string>> emissive = {
        {"cycles/emission/1", {"color", "strength"}},
        {"cycles/principled_bsdf/1", {"emission_color", "emission_strength"}},
        {"cycles/principled_volume/1",
         {"emission_color", "emission_strength", "blackbody_intensity", "blackbody_tint", "temperature"}}};
    std::map<std::string, std::string> definitions;
    for (const auto &node : result.nodes)
      definitions[node.id] = node.definition;
    const auto unique = [&](std::string id) {
      while (std::any_of(result.nodes.begin(), result.nodes.end(),
                         [&](const auto &node) { return node.id.starts_with(id); }))
        id += "_";
      return id;
    };
    const auto prefix = unique("_missing_image_camera");
    const std::string ray = prefix + "ray";
    std::vector<ShaderNodeData> gates;
    std::vector<ShaderLink> added;
    for (auto &link : result.links) {
      if (!affected.contains(link.from.node))
        continue;
      const auto definition = definitions.find(link.to.node);
      if (definition == definitions.end())
        continue;
      const auto inputs = emissive.find(definition->second);
      if (inputs == emissive.end() || !inputs->second.contains(link.to.socket))
        continue;
      const bool color = link.to.socket.ends_with("color") || link.to.socket == "blackbody_tint";
      const auto gate = prefix + std::to_string(gates.size());
      if (color) {
        gates.push_back({gate, "cycles/vector_math/1", {{"math_type", std::string("multiply")}}});
        added.push_back({link.from, {gate, "vector1"}});
        added.push_back({{ray, "is_camera_ray"}, {gate, "vector2"}});
        link.from = {gate, "vector"};
      } else {
        gates.push_back({gate, "cycles/math/1", {{"math_type", std::string("multiply")}}});
        added.push_back({link.from, {gate, "value1"}});
        added.push_back({{ray, "is_camera_ray"}, {gate, "value2"}});
        link.from = {gate, "value"};
      }
    }
    if (!gates.empty()) {
      gated = true;
      result.nodes.push_back({ray, "cycles/light_path/1", {}});
      for (auto &gate : gates)
        result.nodes.push_back(std::move(gate));
      for (auto &link : added)
        result.links.push_back(std::move(link));
    }
  }
  std::set<std::string> retained;
  if (!result.surface.node.empty())
    retained.insert(result.surface.node);
  if (result.volume)
    retained.insert(result.volume->node);
  if (result.displacement)
    retained.insert(result.displacement->node);
  for (const auto &node : result.nodes)
    if (node.definition == "cycles/aov_output/1")
      retained.insert(node.id);
  changed = true;
  while (changed) {
    changed = false;
    for (const auto &link : result.links)
      if (retained.contains(link.to.node))
        changed |= retained.insert(link.from.node).second;
  }
  std::erase_if(result.nodes, [&](const auto &node) { return !retained.contains(node.id); });
  std::erase_if(result.links, [&](const auto &link) {
    return !retained.contains(link.from.node) || !retained.contains(link.to.node);
  });
  return result;
}
} // namespace
namespace {
// A binding can lack UVs while another binding of the same source shader has
// them. Cut only dependent outputs; never substitute a color for displacement.
void prune_graph(ShaderGraphData &result) {
  std::set<std::string> retained;
  if (!result.surface.node.empty())
    retained.insert(result.surface.node);
  if (result.volume)
    retained.insert(result.volume->node);
  if (result.displacement)
    retained.insert(result.displacement->node);
  for (const auto &node : result.nodes)
    if (node.definition == "cycles/aov_output/1")
      retained.insert(node.id);
  bool changed = true;
  while (changed) {
    changed = false;
    for (const auto &link : result.links)
      if (retained.contains(link.to.node))
        changed |= retained.insert(link.from.node).second;
  }
  std::erase_if(result.nodes, [&](const auto &node) { return !retained.contains(node.id); });
  std::erase_if(result.links, [&](const auto &link) {
    return !retained.contains(link.from.node) || !retained.contains(link.to.node);
  });
}
void diagnostic_surface(ShaderGraphData &result) {
  std::string prefix = "_binding_marker";
  while (std::any_of(result.nodes.begin(), result.nodes.end(),
                     [&](const auto &node) { return node.id.starts_with(prefix); }))
    prefix += "_";
  result.nodes.push_back({prefix + "ray", "cycles/light_path/1", {}});
  result.nodes.push_back({prefix + "diffuse", "cycles/diffuse_bsdf/1", {{"color", std::array<float, 3>{1, 0, 1}}}});
  result.nodes.push_back(
      {prefix + "emission", "cycles/emission/1", {{"color", std::array<float, 3>{1, 0, 1}}, {"strength", 1.f}}});
  result.nodes.push_back({prefix + "mix", "cycles/mix_closure/1", {}});
  result.links.push_back({{prefix + "ray", "is_camera_ray"}, {prefix + "mix", "fac"}});
  result.links.push_back({{prefix + "diffuse", "BSDF"}, {prefix + "mix", "closure1"}});
  result.links.push_back({{prefix + "emission", "emission"}, {prefix + "mix", "closure2"}});
  result.surface = {prefix + "mix", "closure"};
}
// Recognize exactly the marker built above, by structure rather than by user
// node names, so recovered bindings can disable emission sampling.
bool has_diagnostic_surface(const ShaderGraphData &data) {
  const auto &mix = data.surface.node;
  if (data.surface.socket != "closure" || !mix.starts_with("_binding_marker") || !mix.ends_with("mix"))
    return false;
  const auto prefix = mix.substr(0, mix.size() - 3);
  const auto defined = [&](const std::string &id, const char *definition) {
    return std::any_of(data.nodes.begin(), data.nodes.end(),
                       [&](const auto &node) { return node.id == id && node.definition == definition; });
  };
  const auto linked = [&](const std::string &from, const char *output, const char *input) {
    return std::any_of(data.links.begin(), data.links.end(), [&](const auto &link) {
      return link.from.node == from && link.from.socket == output && link.to.node == mix && link.to.socket == input;
    });
  };
  return defined(mix, "cycles/mix_closure/1") && defined(prefix + "ray", "cycles/light_path/1") &&
         defined(prefix + "diffuse", "cycles/diffuse_bsdf/1") && defined(prefix + "emission", "cycles/emission/1") &&
         linked(prefix + "ray", "is_camera_ray", "fac") && linked(prefix + "diffuse", "BSDF", "closure1") &&
         linked(prefix + "emission", "emission", "closure2");
}
ShaderGraphData missing_attribute_graph(const ShaderGraphData &source, const std::string &id,
                                        const std::string &socket) {
  auto result = source;
  std::set<std::string> affected;
  for (const auto &link : source.links)
    if (link.from.node == id && (socket.empty() || link.from.socket == socket))
      affected.insert(link.to.node);
  affected.insert(id);
  bool changed = true;
  while (changed) {
    changed = false;
    for (const auto &link : source.links)
      if (affected.contains(link.from.node) && (link.from.node != id || socket.empty() || link.from.socket == socket))
        changed |= affected.insert(link.to.node).second;
  }
  if (result.displacement && affected.contains(result.displacement->node) &&
      (result.displacement->node != id || socket.empty() || result.displacement->socket == socket)) {
    result.displacement.reset();
    result.displacement_method = DisplacementMethod::Bump;
  }
  std::set<std::string> normals;
  for (const auto &node : source.nodes)
    if (affected.contains(node.id) && (node.definition == "cycles/normal_map/1" || node.definition == "cycles/bump/1"))
      normals.insert(node.id);
  std::erase_if(result.links, [&](const auto &link) {
    const bool affected_output =
        affected.contains(link.from.node) && (link.from.node != id || socket.empty() || link.from.socket == socket);
    return normals.contains(link.from.node) || (affected_output && link.to.socket == "normal");
  });
  // Re-evaluate surface dependency after removing affected normal branches.
  affected = {id};
  changed = true;
  while (changed) {
    changed = false;
    for (const auto &link : result.links)
      if (affected.contains(link.from.node) && (link.from.node != id || socket.empty() || link.from.socket == socket))
        changed |= affected.insert(link.to.node).second;
  }
  if (affected.contains(result.surface.node)) {
    diagnostic_surface(result);
  }
  // Omit affected AOV writers as well; they must not keep an invalid branch alive.
  std::set<std::string> retained{result.surface.node};
  if (result.volume)
    retained.insert(result.volume->node);
  if (result.displacement)
    retained.insert(result.displacement->node);
  for (const auto &node : result.nodes)
    if (node.definition == "cycles/aov_output/1" && !affected.contains(node.id))
      retained.insert(node.id);
  changed = true;
  while (changed) {
    changed = false;
    for (const auto &link : result.links)
      if (retained.contains(link.to.node))
        changed |= retained.insert(link.from.node).second;
  }
  std::erase_if(result.nodes, [&](const auto &node) { return !retained.contains(node.id); });
  std::erase_if(result.links, [&](const auto &link) {
    return !retained.contains(link.from.node) || !retained.contains(link.to.node);
  });
  return result;
}

struct SceneQueryDisplacementRecovery {
  ShaderGraphData graph;
  std::set<std::string> queries;
  bool omitted_output{};
  bool capacity_limited{};
};
// True displacement runs before Cycles builds its BVH. Keep shared surface
// queries untouched: rewrite a private displacement path, never the query node.
// An Add has separable contributions; other arithmetic is conservatively
// inseparable. Explicit scalar-displacement normals can fall back to geometry.
std::optional<SceneQueryDisplacementRecovery>
recover_scene_query_displacement(const ShaderGraphData &source,
                                 const std::map<std::string, ccl::ShaderNode *> &engine_nodes) {
  if (!source.displacement || source.displacement_method == DisplacementMethod::Bump)
    return {};
  SceneQueryDisplacementRecovery result{source};
  std::map<std::string, const ShaderNodeData *> nodes;
  std::map<std::string, std::vector<const ShaderLink *>> inputs;
  std::set<std::string> names;
  for (const auto &node : source.nodes) {
    nodes.emplace(node.id, &node);
    names.insert(node.id);
  }
  for (const auto &link : source.links)
    inputs[link.to.node].push_back(&link);
  for (auto &[id, links] : inputs)
    std::sort(links.begin(), links.end(), [](const auto *a, const auto *b) { return a->to.socket < b->to.socket; });
  unsigned serial = 0;
  const auto unique = [&]() {
    std::string id;
    do {
      id = "_displacement_recovery" + std::to_string(serial++);
    } while (!names.insert(id).second);
    return id;
  };
  const auto constant = [&](bool vector) {
    ShaderNodeData node;
    node.id = unique();
    node.definition = vector ? "cycles/color/1" : "cycles/value/1";
    node.parameters.emplace("value", vector ? ShaderValue{std::array<float, 3>{0, 0, 0}} : ShaderValue{0.f});
    const auto id = node.id;
    result.graph.nodes.push_back(std::move(node));
    return ShaderEndpoint{id, vector ? "color" : "value"};
  };
  const auto scalar = [&](ccl::ShaderNode *node, const char *socket) -> std::optional<float> {
    const auto *input = node->input(socket);
    if (!input->link)
      return node->get_float(input->socket_type);
    if (input->link->parent->type->name == ccl::ustring("value"))
      return static_cast<ccl::ValueNode *>(input->link->parent)->get_value();
    return {};
  };
  std::map<std::pair<std::string, std::string>, std::optional<ShaderEndpoint>> memo;
  const std::function<std::optional<ShaderEndpoint>(const ShaderEndpoint &)> rewrite =
      [&](const ShaderEndpoint &endpoint) -> std::optional<ShaderEndpoint> {
    const auto key = std::pair(endpoint.node, endpoint.socket);
    if (const auto found = memo.find(key); found != memo.end())
      return found->second;
    const auto finish = [&](std::optional<ShaderEndpoint> value) {
      memo.emplace(key, value);
      return value;
    };
    const auto &original = *nodes.at(endpoint.node);
    auto *engine = engine_nodes.at(endpoint.node);
    const auto type = engine->type->name.string();
    bool query = false;
    std::set<std::string> passthrough;
    if (type == "raycast") {
      const auto length = scalar(engine, "Length");
      query = !length || *length > 0;
    } else if (type == "ambient_occlusion") {
      const auto distance = scalar(engine, "Distance");
      // Zero means global AO radius. A linked Value=0 is folded into
      // that same unlinked socket before SVM compilation.
      const bool global = distance && *distance == 0;
      query = static_cast<std::uint8_t>(static_cast<ccl::AmbientOcclusionNode *>(engine)->get_samples()) != 0 &&
              (!distance || *distance > 0 || global);
      if (endpoint.socket == "color")
        passthrough.insert("color");
    } else if (type == "bevel") {
      const auto radius = scalar(engine, "Radius");
      query = static_cast<std::uint8_t>(static_cast<ccl::BevelNode *>(engine)->get_samples()) != 0 &&
              (!radius || *radius > 0);
      passthrough.insert("normal");
    }
    if (query) {
      result.queries.insert(endpoint.node);
      return finish({});
    }
    const bool disabled_query = type == "raycast" || type == "ambient_occlusion" || type == "bevel";
    const bool scalar_add =
        type == "math" && static_cast<ccl::MathNode *>(engine)->get_math_type() == ccl::NODE_MATH_ADD;
    const bool vector_add = type == "vector_math" &&
                            static_cast<ccl::VectorMathNode *>(engine)->get_math_type() == ccl::NODE_VECTOR_MATH_ADD;
    const bool additive = scalar_add || vector_add;
    std::map<std::string, std::optional<ShaderEndpoint>> changed;
    std::set<std::string> unavailable;
    for (const auto *link : inputs[endpoint.node]) {
      if (disabled_query && !passthrough.contains(link->to.socket))
        continue;
      if (additive && link->to.socket != (scalar_add ? "value1" : "vector1") &&
          link->to.socket != (scalar_add ? "value2" : "vector2"))
        continue;
      const auto value = rewrite(link->from);
      if (!value)
        unavailable.insert(link->to.socket);
      if (!value || *value != link->from)
        changed.emplace(link->to.socket, value);
    }
    if (changed.empty())
      return finish(endpoint);
    const bool normal_fallback = type == "displacement" && unavailable == std::set<std::string>{"normal"};
    if (!unavailable.empty() && !normal_fallback && !additive)
      return finish({});
    if (additive && unavailable.contains(scalar_add ? "value1" : "vector1") &&
        unavailable.contains(scalar_add ? "value2" : "vector2"))
      return finish({});
    auto clone = original;
    clone.id = unique();
    if (normal_fallback)
      clone.parameters.erase("normal");
    const auto id = clone.id;
    result.graph.nodes.push_back(std::move(clone));
    for (const auto *link : inputs[endpoint.node]) {
      auto value = std::optional(link->from);
      if (const auto found = changed.find(link->to.socket); found != changed.end())
        value = found->second;
      if (!value) {
        if (normal_fallback)
          continue;
        value = constant(vector_add);
      }
      result.graph.links.push_back({*value, {id, link->to.socket}});
    }
    return finish(ShaderEndpoint{id, endpoint.socket});
  };
  result.graph.displacement = rewrite(*source.displacement);
  if (result.queries.empty())
    return {};
  if (!result.graph.displacement)
    result.graph.displacement_method = DisplacementMethod::Bump;
  prune_graph(result.graph);
  // Shared surface branches can consume the full graph budget. Recovery must
  // never turn that legitimate source graph into an internal size failure.
  if (result.graph.nodes.size() > 256 || result.graph.links.size() > 2048) {
    result.graph = source;
    result.graph.displacement.reset();
    result.graph.displacement_method = DisplacementMethod::Bump;
    prune_graph(result.graph);
    result.capacity_limited = true;
  }
  result.omitted_output = !result.graph.displacement;
  return result;
}
} // namespace
ShaderGraphData diagnostic_surface_graph() {
  ShaderGraphData graph;
  diagnostic_surface(graph);
  return graph;
}
ccl::unique_ptr<ccl::ShaderGraph>
prepare_shader_graph(const ShaderGraphData &data, ShaderGraphRequirements &requirements,
                     const TextureInputPolicy &policy, const std::vector<AovDefinition> &aovs, ShaderContext context,
                     bool has_uv, bool has_color, ShaderGeometry geometry, const AttributeLayout *attributes,
                     const AttributeLayout *instance_attributes) {
  if (std::any_of(data.nodes.begin(), data.nodes.end(),
                  [](const auto &n) { return shader_switch_definition(n.definition); })) {
    std::vector<SourceDiagnostic> diagnostics;
    const auto lowered = lower_shader_switches(data, &diagnostics);
    auto graph = prepare_shader_graph(lowered, requirements, policy, aovs, context, has_uv, has_color, geometry,
                                      attributes, instance_attributes);
    requirements.source_diagnostics.insert(requirements.source_diagnostics.end(), diagnostics.begin(),
                                           diagnostics.end());
    return graph;
  }
  const bool world = context == ShaderContext::World, light = context == ShaderContext::Light;
  // Local geometry Cd retains its varying domain. When absent, a packed
  // instance supplies a uniform named field without duplicating its prototype.
  const bool instance_color = !has_color && instance_attributes && instance_attributes->contains("Cd");
  if (data.schema_version != 1)
    throw std::invalid_argument("Unsupported shader graph schema version");
  if (data.image_time && !std::isfinite(*data.image_time))
    throw std::invalid_argument("Image evaluation time must be finite");
  if (data.nodes.empty() || data.nodes.size() > 256 || data.links.size() > 2048)
    throw std::invalid_argument("Shader graph requires 1..256 nodes and at most 2048 links");
  requirements = {};
  requirements.surface = !data.surface.node.empty() || !data.surface.socket.empty();
  requirements.volume = data.volume.has_value();
  requirements.diagnostic_surface = has_diagnostic_surface(data);
  if (!requirements.surface && !requirements.volume)
    throw std::invalid_argument("Connect a surface or volume output");
  requirements.displacement = data.displacement.has_value();
  if (world && requirements.displacement)
    throw std::invalid_argument("World materials cannot use displacement");
  if (!requirements.surface && requirements.displacement)
    throw std::invalid_argument("Volume-only materials cannot have displacement outputs");
  if (data.displacement_method != DisplacementMethod::Bump && data.displacement_method != DisplacementMethod::True &&
      data.displacement_method != DisplacementMethod::Both)
    throw std::invalid_argument("Unknown displacement method");
  if (!requirements.displacement && data.displacement_method != DisplacementMethod::Bump)
    throw std::invalid_argument("True displacement requires a connected displacement output");
  if (int(data.settings.emission_sampling) < int(EmissionSampling::None) ||
      int(data.settings.emission_sampling) > int(EmissionSampling::FrontBack) ||
      int(data.settings.volume_sampling) < int(VolumeSampling::Distance) ||
      int(data.settings.volume_sampling) > int(VolumeSampling::MultipleImportance) ||
      int(data.settings.volume_interpolation) < int(VolumeInterpolation::Linear) ||
      int(data.settings.volume_interpolation) > int(VolumeInterpolation::Cubic) ||
      !std::isfinite(data.settings.volume_step_rate) || data.settings.volume_step_rate < 0.001f ||
      data.settings.volume_step_rate > 1000.f)
    throw std::invalid_argument("Invalid material settings");
  if (light && (requirements.volume || requirements.displacement))
    throw LightSourceError("Analytic lights require an emission surface without volume or displacement");
  const auto recover_image = [&](const std::string &id, const std::string &filename, const TextureSourceError &error) {
    if (world)
      throw TextureSourceError(error.what());
    bool displacement_disabled = false, normals_disabled = false, emission_gated = false;
    auto recovered = missing_image_graph(data, id, displacement_disabled, normals_disabled, emission_gated, !light);
    if (light)
      for (auto &node : recovered.nodes)
        if (node.id == id)
          node.parameters = {{"value", std::array<float, 3>{1, 1, 1}}};
    ShaderGraphRequirements resolved;
    auto graph = prepare_shader_graph(recovered, resolved, policy, aovs, context, has_uv, has_color, geometry,
                                      attributes, instance_attributes);
    TextureAsset failure;
    failure.filename = filename;
    failure.image_time = data.image_time.value_or(0.);
    if (filename.starts_with("op:") && policy.resolve_image)
      failure.memory_image = policy.resolve_image(filename, failure.image_time);
    failure.failure_revision = texture_watch_revision({filename}, policy.identity ? policy.identity() : std::string{});
    failure.revision = failure.failure_revision;
    resolved.textures.push_back(std::move(failure));
    std::string action = light ? "Disabled this texture modulation; retaining the light's color and power"
                               : "Using magenta for unavailable color inputs";
    if (displacement_disabled)
      action += "; disabled affected displacement";
    if (normals_disabled)
      action += "; using geometric normals for affected normal/bump inputs";
    if (emission_gated)
      action += "; affected emission is visible to the camera only";
    resolved.source_diagnostics.push_back(
        {light ? "light.texture_unavailable" : "texture.unavailable", filename, id, error.what(), action});
    requirements = std::move(resolved);
    return graph;
  };
  const auto recover_uv = [&](const std::string &id, const std::string &socket = std::string{}) {
    if (light)
      throw LightSourceError("The analytic light has no requested UV/coordinate field: " + id);
    const auto recovered = missing_attribute_graph(data, id, socket);
    auto result = prepare_shader_graph(recovered, requirements, policy, aovs, context, false, has_color, geometry,
                                       attributes, instance_attributes);
    requirements.source_diagnostics.push_back(
        {"material.missing_uv", id, "", "Assigned geometry has no usable UV attribute",
         "Using a diagnostic surface where UVs affect color; disabling affected normal/displacement branches"});
    return result;
  };
  const auto recover_color = [&](const std::string &id) {
    if (light)
      throw LightSourceError("Analytic light shaders have no Cd field: " + id);
    const auto recovered = missing_attribute_graph(data, id, {});
    auto result = prepare_shader_graph(recovered, requirements, policy, aovs, context, has_uv, false, geometry,
                                       attributes, instance_attributes);
    requirements.source_diagnostics.push_back(
        {"material.missing_color", id, "", "Assigned geometry has no usable Cd color attribute",
         "Using a diagnostic surface where Cd affects color; disabling affected normal/displacement branches"});
    return result;
  };
  const auto recover_volume = [&](const std::string &id, const std::string &reason) {
    if (!data.volume)
      throw std::invalid_argument(reason);
    auto recovered = data;
    if (world) {
      if (!requirements.surface)
        throw WorldSourceError(reason);
      recovered.volume.reset();
      prune_graph(recovered);
      auto result = prepare_shader_graph(recovered, requirements, policy, aovs, context, has_uv, has_color, geometry,
                                         attributes, instance_attributes);
      requirements.source_diagnostics.push_back(
          {"world.volume_source_unavailable", id, "world", reason,
           "Disabled the world medium; retained the independent background shader"});
      return result;
    }
    std::string marker = "_medium_marker";
    while (std::any_of(recovered.nodes.begin(), recovered.nodes.end(), [&](const auto &n) { return n.id == marker; }))
      marker += "_";
    // A closed mesh has no sparse fields. Sparse bindings replace this
    // medium with the density-grid marker, without inventing density.
    recovered.nodes.push_back(
        {marker, "cycles/scatter_volume/1", {{"color", std::array<float, 3>{1, 0, 1}}, {"density", 1.f}}});
    recovered.volume = ShaderEndpoint{marker, "volume"};
    prune_graph(recovered);
    auto result = prepare_shader_graph(recovered, requirements, policy, aovs, context, has_uv, has_color, geometry,
                                       attributes, instance_attributes);
    requirements.volume_diagnostic = true;
    requirements.source_diagnostics.push_back(
        {"material.volume_source_unavailable", id, "", reason,
         "Using magenta scattering with no emission; sparse volumes use their retained density grid"});
    return result;
  };
  const auto recover_geometry = [&](bool omit_volume, bool omit_displacement, const std::string &reason) {
    auto recovered = data;
    if (omit_volume) {
      recovered.volume.reset();
      if (!requirements.surface)
        diagnostic_surface(recovered);
    }
    if (omit_displacement) {
      recovered.displacement.reset();
      recovered.displacement_method = DisplacementMethod::Bump;
    }
    prune_graph(recovered);
    auto result = prepare_shader_graph(recovered, requirements, policy, aovs, context, has_uv, has_color, geometry,
                                       attributes, instance_attributes);
    requirements.source_diagnostics.push_back({"material.geometry_binding", "", "", reason,
                                               "Retaining usable surface shading; omitting unsupported effects, with "
                                               "diagnostic shading if no surface remains"});
    return result;
  };
  // Known host-adapter gaps are source problems, not malformed engine input.
  // Cut only dependent outputs, retaining independent displacement and media.
  const auto recover_attribute =
      [&](const std::string &id, const std::string &reason, const std::string &socket = std::string{},
          const std::string &code = "material.unsupported_attribute", const std::string &attribute = std::string{}) {
        if (light)
          throw LightSourceError(reason);
        if (world)
          throw std::invalid_argument(reason);
        const auto recovered = missing_attribute_graph(data, id, socket);
        // A volume-only dependency is outside this surface recovery policy.
        // Keep strict validation and never recurse on an unchanged graph.
        if (recovered == data)
          throw std::invalid_argument(reason);
        auto result = prepare_shader_graph(recovered, requirements, policy, aovs, context, has_uv, has_color, geometry,
                                           attributes, instance_attributes);
        requirements.source_diagnostics.push_back(
            {code, id, "", reason,
             "Using diagnostic shading for affected color; disabling dependent normal, displacement and AOV branches",
             "warning", attribute});
        return result;
      };
  const auto tangent_map = [&](ccl::ShaderNode *node, ccl::ustring &map, unsigned base,
                               bool displacement) -> ccl::unique_ptr<ccl::ShaderGraph> {
    const auto name = map.string();
    if (!name.empty() && name != "uv") {
      if (name.size() > 256)
        return recover_attribute(node->name.string(), "Tangent UV map requires a name of at most 256 bytes", {},
                                 "material.missing_attribute", name);
      identifier(name, "tangent UV map");
      requirements.named_attributes.insert(name);
      if (instance_attributes && instance_attributes->contains(name))
        return recover_attribute(
            node->name.string(),
            "Packed field '" + name +
                "' is uniform per instance; tangent bases require a UV field on the prototype mesh",
            {}, "material.geometry_binding", name);
      if (attributes && (!attributes->contains(name) || attributes->at(name) < 2))
        return recover_attribute(node->name.string(),
                                 "Assigned geometry has no usable two/three-component tangent UV map: " + name, {},
                                 "material.missing_attribute", name);
      requirements.named_tangent_bases[name] |= base;
      if (displacement) {
        requirements.displacement_named_attributes.insert(name);
        requirements.displacement_deformation_dependent = true;
      }
      map = ccl::ustring(engine_attribute_name(name));
    } else {
      if (!has_uv)
        return recover_uv(node->name.string());
      map = ccl::ustring();
      requirements.requires_uv = true;
      if (displacement) {
        requirements.displacement_requires_uv = true;
        requirements.displacement_deformation_dependent = true;
      }
    }
    requirements.requires_mesh_tangents = true;
    requirements.tangent_bases |= base;
    return {};
  };
  auto graph = ccl::make_unique<ccl::ShaderGraph>();
  std::map<std::string, ccl::ShaderNode *> nodes;
  std::map<std::string, std::vector<int>> ramp_sources, curve_sources;
  std::map<std::string, std::vector<std::string>> edges;
  const auto world_allowed = world   ? world_graph_definitions()
                             : light ? light_graph_definitions()
                                     : std::vector<std::string>{};
  for (const auto &entry : data.nodes) {
    identifier(entry.id, "node");
    if (nodes.contains(entry.id))
      throw std::invalid_argument("Duplicate shader node: " + entry.id);
    if ((world || light) && entry.definition != "cycles/owned_ramp/1" &&
        std::find(world_allowed.begin(), world_allowed.end(), entry.definition) == world_allowed.end()) {
      definition(entry.definition); // Unknown definitions/schema remain a strict contract failure.
      if (light)
        throw LightSourceError("Shader node is not supported by an analytic light: " + entry.definition);
      throw WorldSourceError("Shader node is not supported in a world material: " + entry.definition);
    }

    auto *node = graph->create_node(definition(entry.definition));
    node->name = ccl::ustring(entry.id);
    if (node->special_type == ccl::SHADER_SPECIAL_TYPE_OUTPUT_AOV)
      static_cast<ccl::OutputAOVNode *>(node)->set_name(ccl::ustring());
    const bool ramp = entry.definition == "cycles/owned_ramp/1";
    const bool curve = entry.definition == "cycles/float_curve/1" || entry.definition == "cycles/rgb_curves/1" ||
                       entry.definition == "cycles/vector_curves/1";
    for (const auto &[name, value] : entry.parameters) {
      if ((ramp && name == "source") || (curve && name == "curve_source")) {
        const auto *controls = std::get_if<std::vector<int>>(&value);
        if (!controls || controls->size() > 780u)
          throw std::invalid_argument("Invalid owned ramp/curve controls");
        (ramp ? ramp_sources : curve_sources).emplace(entry.id, *controls);
      } else if (ramp) {
        if (name != "factor")
          throw std::invalid_argument("Ramp tables are private host-baked resources");
        set_parameter(*node, "fac", value);
      } else
        set_parameter(*node, name, value);
    }
    if (ramp && !ramp_sources.contains(entry.id))
      throw std::invalid_argument("Missing owned ramp controls");
    nodes.emplace(entry.id, node);
  }
  const auto lookup = [&](const ShaderEndpoint &endpoint) {
    identifier(endpoint.node, "node");
    identifier(endpoint.socket, "socket");
    const auto found = nodes.find(endpoint.node);
    if (found == nodes.end())
      throw std::invalid_argument("Missing shader node: " + endpoint.node);
    return found->second;
  };
  const auto output = [&](const ShaderEndpoint &endpoint) {
    auto *node = lookup(endpoint);
    const bool ramp = ramp_sources.contains(endpoint.node);
    if (ramp && endpoint.socket != "color" && endpoint.socket != "value")
      throw std::invalid_argument("Missing owned ramp output socket: " + endpoint.socket);
    const auto *socket =
        node->type->find_output(ccl::ustring(ramp && endpoint.socket == "value" ? "alpha" : endpoint.socket));
    if (!socket)
      throw std::invalid_argument("Missing shader output socket: " + endpoint.node + "." + endpoint.socket);
    auto *result = node->output(socket->ui_name.c_str());
    if (!result)
      throw std::invalid_argument("Unavailable shader output: " + endpoint.socket);
    return result;
  };
  const auto input = [&](const ShaderEndpoint &endpoint) {
    auto *node = lookup(endpoint);
    const bool ramp = ramp_sources.contains(endpoint.node);
    if (ramp && endpoint.socket != "factor")
      throw std::invalid_argument("Missing owned ramp input socket: " + endpoint.socket);
    const auto *socket = node->type->find_input(ccl::ustring(ramp ? "fac" : endpoint.socket));
    // OSL_INTERNAL marks an input whose unlinked default the OSL shader supplies (Fresnel and
    // Layer Weight Normal); it still takes links, as in Blender. SVM_INTERNAL inputs are the
    // engine graph's own (closure mix weights).
    if (!socket || !(socket->flags & Socket::LINKABLE) || (socket->flags & Socket::SVM_INTERNAL))
      throw std::invalid_argument("Missing/nonlinkable shader input socket: " + endpoint.node + "." + endpoint.socket);
    auto *result = node->input(socket->ui_name.c_str());
    if (!result)
      throw std::invalid_argument("Unavailable shader input: " + endpoint.socket);
    return result;
  };
  std::set<std::pair<std::string, std::string>> bound_inputs;
  for (const auto &link : data.links) {
    auto *from = output(link.from);
    auto *to = input(link.to);
    if (!bound_inputs.emplace(link.to.node, link.to.socket).second)
      throw std::invalid_argument("Duplicate shader input connection: " + link.to.node + "." + link.to.socket);
    if (!compatible(from->type(), to->type()))
      throw std::invalid_argument("Shader link requires an explicit type conversion: " + link.from.socket + " -> " +
                                  link.to.socket);
    edges[link.from.node].push_back(link.to.node);
  }
  std::map<std::string, int> visited;
  const std::function<void(const std::string &)> visit = [&](const std::string &node) {
    if (visited[node] == 1)
      throw std::invalid_argument("Cyclic shader graph at node: " + node);
    if (visited[node] == 2)
      return;
    visited[node] = 1;
    for (const auto &child : edges[node])
      visit(child);
    visited[node] = 2;
  };
  for (const auto &[id, node] : nodes)
    visit(id);
  auto *surface = requirements.surface ? output(data.surface) : nullptr;
  auto *volume = requirements.volume ? output(*data.volume) : nullptr;
  if (surface && !compatible(surface->type(), Socket::CLOSURE))
    throw std::invalid_argument("Surface output requires a shader, color, vector or numeric value");
  if (volume && volume->type() != Socket::CLOSURE)
    throw std::invalid_argument("Volume output requires a closure");
  for (const auto &link : data.links)
    graph->connect(output(link.from), input(link.to));
  // Native meshes do not provide Blender's Generated coordinate attribute.
  // Author an explicit, documented world-position default instead of letting
  // the engine silently evaluate noise using a missing attribute. Insert the
  // dependency before displacement analysis so instances/deformation cannot
  // incorrectly reuse a world-dependent displaced mesh.
  for (const auto &[id, node] : nodes)
    if (auto *vector = node->input("Vector");
        vector && (vector->socket_type.flags & Socket::LINK_TEXTURE_GENERATED) && !vector->link) {
      // Sky is directional: retain upstream's Generated default. In a
      // background it is the world ray, and preserving this connection lets
      // Cycles sample the solar disc directly instead of building a 4K map.
      // On a surface it is the adapter's normalized nominal SOP bounds.
      if (node->type->name == ccl::ustring("sky_texture")) {
        continue; // Validate attributes only if this node contributes.
      }
      auto *coordinates = graph->create_node<ccl::GeometryNode>();
      coordinates->name = ccl::ustring(id + "/@world_position");
      graph->connect(coordinates->output("Position"), node->input("Vector"));
    }
  std::set<ccl::ShaderNode *> reachable;
  std::set<ccl::ShaderOutput *> used_outputs;
  std::set<ccl::ShaderNode *> surface_nodes, medium_nodes;
  std::set<ccl::ShaderOutput *> surface_outputs, medium_outputs;
  const std::function<void(ccl::ShaderNode *)> dependencies = [&](ccl::ShaderNode *node) {
    if (!reachable.insert(node).second)
      return;
    for (auto *socket : node->inputs)
      if (socket->link) {
        used_outputs.insert(socket->link);
        dependencies(socket->link->parent);
      }
  };
  const std::function<void(ccl::ShaderNode *, std::set<ccl::ShaderNode *> &, std::set<ccl::ShaderOutput *> &)>
      domain_dependencies = [&](ccl::ShaderNode *node, auto &scope, auto &outputs) {
        if (!scope.insert(node).second)
          return;
        for (auto *socket : node->inputs)
          if (socket->link) {
            outputs.insert(socket->link);
            domain_dependencies(socket->link->parent, scope, outputs);
          }
      };
  if (surface) {
    used_outputs.insert(surface);
    dependencies(surface->parent);
    surface_outputs.insert(surface);
    domain_dependencies(surface->parent, surface_nodes, surface_outputs);
  }
  if (volume) {
    used_outputs.insert(volume);
    dependencies(volume->parent);
    medium_outputs.insert(volume);
    domain_dependencies(volume->parent, medium_nodes, medium_outputs);
  }
  // AOVs are additional graph roots. Only the input selected by the ROP's
  // type requires attributes/textures; upstream removes the other input.
  std::set<std::string> aov_writers;
  for (const auto &[id, node] : nodes)
    if (node->special_type == ccl::SHADER_SPECIAL_TYPE_OUTPUT_AOV) {
      if (!requirements.surface)
        throw std::invalid_argument("AOV Output is a surface shader feature in this adapter");
      const auto name = static_cast<ccl::OutputAOVNode *>(node)->get_name().string();
      validate_aovs({{name, true}});
      if (!aov_writers.insert(name).second)
        throw std::invalid_argument("Duplicate AOV Output writer in material: " + name);
      const auto selected = std::find_if(aovs.begin(), aovs.end(), [&](const auto &aov) { return aov.name == name; });
      if (selected == aovs.end())
        continue;
      reachable.insert(node);
      const auto *socket = node->input(selected->color ? "Color" : "Value");
      if (socket->link) {
        used_outputs.insert(socket->link);
        dependencies(socket->link->parent);
        surface_outputs.insert(socket->link);
        domain_dependencies(socket->link->parent, surface_nodes, surface_outputs);
      }
    }
  ccl::ShaderOutput *displacement = nullptr;
  std::set<ccl::ShaderNode *> displacement_nodes;
  std::set<ccl::ShaderOutput *> displacement_outputs;
  if (data.displacement) {
    displacement = output(*data.displacement);
    if (!Socket::is_float3(displacement->type()))
      throw std::invalid_argument("Displacement output requires a float3 vector");
    if (auto recovered = recover_scene_query_displacement(data, nodes)) {
      auto prepared = prepare_shader_graph(recovered->graph, requirements, policy, aovs, context, has_uv, has_color,
                                           geometry, attributes, instance_attributes);
      for (const auto &id : recovered->queries) {
        const auto type = nodes.at(id)->type->name.string();
        const std::string label = type == "ambient_occlusion" ? "Ambient Occlusion"
                                  : type == "bevel"           ? "Bevel"
                                                              : "Raycast";
        requirements.source_diagnostics.push_back(
            {"material.displacement_scene_query", id, "",
             "True displacement cannot evaluate " + label + " before the scene BVH exists" +
                 (recovered->capacity_limited
                      ? "; retaining separate valid contributions exceeds the owned graph capacity"
                      : ""),
             recovered->omitted_output ? "Omitted the dependent displacement output; retained usable surface shading"
                                       : "Omitted the query's displacement contribution; retained separable Add terms "
                                         "and usable geometry/normals",
             "warning", "displacement"});
      }
      return prepared;
    }
    used_outputs.insert(displacement);
    dependencies(displacement->parent);
    surface_outputs.insert(displacement);
    domain_dependencies(displacement->parent, surface_nodes, surface_outputs);
    displacement_outputs.insert(displacement);
    const std::function<void(ccl::ShaderNode *)> displace_dependencies = [&](ccl::ShaderNode *node) {
      if (!displacement_nodes.insert(node).second)
        return;
      for (auto *socket : node->inputs)
        if (socket->link) {
          displacement_outputs.insert(socket->link);
          displace_dependencies(socket->link->parent);
        }
    };
    displace_dependencies(displacement->parent);
    // The session separately validates owned temporal graphs. This flag
    // describes the spatial inputs, including on each frozen shutter graph.
    requirements.displacement_motion_supported = true;
    // The engine output is a world vector. Its object-space converter can
    // make a local, instance-independent result; other branches need the
    // complete instance transform and/or identity in the variant key.
    requirements.displacement_transform_dependent = true;
    const auto root_type = displacement->parent->type->name.string();
    if (root_type == "displacement")
      requirements.displacement_transform_dependent =
          static_cast<ccl::DisplacementNode *>(displacement->parent)->get_space() != ccl::NODE_NORMAL_MAP_OBJECT;
    else if (root_type == "vector_displacement")
      requirements.displacement_transform_dependent =
          static_cast<ccl::VectorDisplacementNode *>(displacement->parent)->get_space() != ccl::NODE_NORMAL_MAP_OBJECT;
    for (auto *node : displacement_nodes) {
      const auto name = node->type->name.string();
      // These inputs need additional shutter snapshots/bases. Do not
      // evaluate them with centre-only attributes while claiming motion.
      if ((name == "attribute" &&
           (static_cast<ccl::AttributeNode *>(node)->get_attribute() == ccl::ustring("point_random") ||
            static_cast<ccl::AttributeNode *>(node)->get_attribute() == ccl::ustring("curve_random"))) ||
          (name == "tangent" &&
           static_cast<ccl::TangentNode *>(node)->get_direction_type() != ccl::NODE_TANGENT_UVMAP) ||
          name == "bump" || name == "camera_info" || name == "object_info" || name == "bevel" || name == "raycast" ||
          name == "ambient_occlusion")
        requirements.displacement_motion_supported = false;
      if (name == "texture_coordinate") {
        for (auto *output : node->outputs)
          if (displacement_outputs.contains(output) && output->name() != "Object" && output->name() != "Normal" &&
              output->name() != "UV" && output->name() != "Generated")
            requirements.displacement_motion_supported = false;
        // Camera coordinates read worldtocamera; Window additionally
        // reads the active projection/viewplane (including aspect).
        // Unconnected outputs must not make ordinary meshes depend on
        // camera navigation or lens edits.
        requirements.displacement_camera_transform_dependent |= displacement_outputs.contains(node->output("Camera")) ||
                                                                displacement_outputs.contains(node->output("Window"));
        requirements.displacement_camera_projection_dependent |= displacement_outputs.contains(node->output("Window"));
      }
      if (name == "geometry") {
        // Original-mesh scalar fields are shared by every instance.
        // Only spatial/ray outputs need an instance transform here.
        const bool spatial = std::any_of(node->outputs.begin(), node->outputs.end(), [&](auto *socket) {
          return displacement_outputs.contains(socket) && socket->name() != "Pointiness" &&
                 socket->name() != "Random Per Island";
        });
        requirements.displacement_transform_dependent |= spatial;
        requirements.displacement_deformation_dependent |= spatial;
      } else if (name == "normal" || name == "fresnel" || name == "layer_weight") {
        requirements.displacement_transform_dependent = true;
        requirements.displacement_deformation_dependent = true;
      } else if (name == "object_info") {
        const auto used = [&](const char *output) { return displacement_outputs.contains(node->output(output)); };
        requirements.displacement_transform_dependent |= used("Location");
        requirements.displacement_identity_dependent |= used("Random");
        requirements.displacement_requires_object_index |= used("Object Index");
        requirements.displacement_requires_material_index |= used("Material Index");
      } else if (name == "attribute") {
        const auto attribute = static_cast<ccl::AttributeNode *>(node)->get_attribute();
        if (attribute == ccl::ustring("Cd") && !instance_color) {
          requirements.displacement_requires_color = true;
          requirements.displacement_deformation_dependent = true;
        }
        if (attribute == ccl::ustring("point_random") || attribute == ccl::ustring("curve_random"))
          requirements.displacement_identity_dependent = true;
      } else if (name == "displacement") {
        const auto *scalar = static_cast<ccl::DisplacementNode *>(node);
        requirements.displacement_deformation_dependent = true;
        if (scalar->get_space() != ccl::NODE_NORMAL_MAP_OBJECT || node->input("Normal")->link)
          requirements.displacement_transform_dependent = true;
      } else if (name == "vector_displacement") {
        const auto *vector = static_cast<ccl::VectorDisplacementNode *>(node);
        if (vector->get_space() != ccl::NODE_NORMAL_MAP_OBJECT)
          requirements.displacement_transform_dependent = true;
        if (vector->get_space() == ccl::NODE_NORMAL_MAP_TANGENT) {
          requirements.displacement_deformation_dependent = true;
        }
      } else if (name == "bump" || name == "normal_map") {
        requirements.displacement_transform_dependent = true;
        requirements.displacement_deformation_dependent = true;
      } else if (name == "texture_coordinate") {
        // UV values belong to geometry, independently of the instance
        // transform. Other coordinate outputs keep their conservative
        // transform-dependent variant policy.
        for (auto *socket : node->outputs)
          if (displacement_outputs.contains(socket) && socket->name() != "UV" && socket->name() != "Generated")
            requirements.displacement_transform_dependent = true;
        for (auto *socket : node->outputs)
          if (displacement_outputs.contains(socket) && socket->name() != "Generated")
            requirements.displacement_deformation_dependent = true;
      } else if (name == "vector_transform") {
        const auto *vector = static_cast<ccl::VectorTransformNode *>(node);
        const auto from = vector->get_convert_from(), to = vector->get_convert_to();
        if (from != to) {
          requirements.displacement_transform_dependent |= from == ccl::NODE_VECTOR_TRANSFORM_CONVERT_SPACE_OBJECT ||
                                                           to == ccl::NODE_VECTOR_TRANSFORM_CONVERT_SPACE_OBJECT;
          requirements.displacement_camera_transform_dependent |=
              from == ccl::NODE_VECTOR_TRANSFORM_CONVERT_SPACE_CAMERA ||
              to == ccl::NODE_VECTOR_TRANSFORM_CONVERT_SPACE_CAMERA;
        }
      } else if (name == "camera_info") {
        requirements.displacement_camera_transform_dependent = true;
        requirements.displacement_transform_dependent = true;
        requirements.displacement_deformation_dependent = true;
      } else if (name == "tangent" || name == "raycast" || name == "bevel" || name == "ambient_occlusion") {
        requirements.displacement_transform_dependent = true;
        requirements.displacement_deformation_dependent = true;
      }
    }
  }
  if (geometry == ShaderGeometry::Strands &&
      (requirements.volume || (requirements.displacement && data.displacement_method != DisplacementMethod::Bump)))
    return recover_geometry(requirements.volume,
                            requirements.displacement && data.displacement_method != DisplacementMethod::Bump,
                            "Points and curves cannot use interior media or true displacement in this adapter");
  // Closure sockets alone do not encode whether the closure is a BSDF or a
  // medium. Validate the contributing nodes before Cycles silently drops it.
  for (auto *node : reachable) {
    const auto name = node->type->name.string();
    const bool on_surface = surface_nodes.contains(node), on_volume = medium_nodes.contains(node);
    if (name == "float_curve" || name == "rgb_curves" || name == "vector_curves") {
      const bool scalar = name == "float_curve";
      const auto kind = scalar                 ? ShaderCurveKind::Float
                        : name == "rgb_curves" ? ShaderCurveKind::RGB
                                               : ShaderCurveKind::Vector;
      const auto &source = curve_sources[node->name.string()];
      try {
        const auto table = [&] {
          try {
            return prepare_shader_curve({source.data(), source.size()}, kind);
          } catch (const ShaderCurveSourceError &) {
            const auto *factor = node->input(scalar ? "Factor" : "Fac");
            const auto *link = factor->link;
            const bool zero = !link ? (scalar ? static_cast<ccl::FloatCurveNode *>(node)->get_fac()
                                              : static_cast<ccl::CurvesNode *>(node)->get_fac()) == 0.f
                                    : link->parent->type->name == ccl::ustring("value") &&
                                          static_cast<ccl::ValueNode *>(link->parent)->get_value() == 0.f;
            if (zero)
              return prepare_shader_curve({}, kind);
            throw;
          }
        }();
        if (scalar) {
          auto *curve = static_cast<ccl::FloatCurveNode *>(node);
          ccl::array<float> values;
          values.resize(table.values.size());
          for (std::size_t i = 0; i < table.values.size(); ++i)
            values[i] = table.values[i][0];
          curve->set_curve(values);
          curve->set_min_x(table.minimum);
          curve->set_max_x(table.maximum);
          curve->set_extrapolate(table.extrapolate);
        } else {
          auto *curve = static_cast<ccl::CurvesNode *>(node);
          ccl::array<ccl::packed_float3> values;
          values.resize(table.values.size());
          for (std::size_t i = 0; i < table.values.size(); ++i) {
            const auto &v = table.values[i];
            values[i] = ccl::make_float3(v[0], v[1], v[2]);
          }
          curve->set_curves(values);
          curve->set_min_x(table.minimum);
          curve->set_max_x(table.maximum);
          curve->set_extrapolate(table.extrapolate);
        }
      } catch (const ShaderCurveSourceError &error) {
        if (on_volume)
          return recover_volume(node->name.string(), error.what());
        if (world)
          throw WorldSourceError(error.what());
        return recover_attribute(node->name.string(), error.what(), {}, "material.curve_unavailable");
      }
    }
    if (const auto source = ramp_sources.find(node->name.string()); source != ramp_sources.end()) {
      auto *ramp = static_cast<ccl::RGBRampNode *>(node);
      try {
        const auto table = prepare_owned_ramp(source->second);
        ccl::array<ccl::packed_float3> values;
        ccl::array<float> alpha;
        values.resize(table.values.size());
        alpha.resize(table.values.size(), 1.f);
        for (std::size_t i = 0; i < table.values.size(); ++i) {
          const auto &v = table.values[i];
          values[i] = ccl::make_float3(v[0], v[1], v[2]);
          alpha[i] = v[0];
        }
        ramp->set_ramp(values);
        ramp->set_ramp_alpha(alpha);
        ramp->set_interpolate(table.interpolate);
      } catch (const RampSourceError &error) {
        if (on_volume)
          return recover_volume(node->name.string(), error.what());
        if (world)
          throw WorldSourceError(error.what());
        return recover_attribute(node->name.string(), error.what(), {}, "material.ramp_unavailable");
      }
    }
    bool node_requires_uv = false;
    const bool on_displacement = displacement_nodes.contains(node);
    if (geometry == ShaderGeometry::Strands &&
        ((name == "tangent" &&
          static_cast<ccl::TangentNode *>(node)->get_direction_type() == ccl::NODE_TANGENT_UVMAP) ||
         (name == "normal_map" &&
          static_cast<ccl::NormalMapNode *>(node)->get_space() == ccl::NODE_NORMAL_MAP_TANGENT) ||
         (name == "vector_displacement" &&
          static_cast<ccl::VectorDisplacementNode *>(node)->get_space() == ccl::NODE_NORMAL_MAP_TANGENT)))
      return recover_attribute(node->name.string(), "This shader input requires polygon mesh tangents", {},
                               "material.geometry_binding");
    if (on_volume && (name == "uvmap" ||
                      (name == "tangent" &&
                       static_cast<ccl::TangentNode *>(node)->get_direction_type() == ccl::NODE_TANGENT_UVMAP) ||
                      (name == "normal_map" &&
                       static_cast<ccl::NormalMapNode *>(node)->get_space() == ccl::NODE_NORMAL_MAP_TANGENT) ||
                      (name == "image_texture" && !node->input("Vector")->link)))
      return recover_volume(node->name.string(),
                            "Volume inputs require explicit coordinates; surface UV/tangent data is unavailable");
    if (world && on_surface && volume_nodes().contains(name))
      throw WorldSourceError("Volume shader cannot feed the world background output: " + name);
    if (on_surface && (volume_nodes().contains(name) || (!world && name == "background_shader")))
      return recover_attribute(node->name.string(), "Shader type cannot feed a surface output: " + name, {},
                               "material.shader_domain");
    if (on_volume && (surface_closures().contains(name) || name == "background_shader"))
      return recover_volume(node->name.string(), "Shader type cannot feed a volume output: " + name);
    if (name == "attribute") {
      const auto value = static_cast<ccl::AttributeNode *>(node)->get_attribute().string();
      if (!on_volume && value == "Cd" && !instance_color) {
        requirements.requires_color = true;
        if (!has_color)
          return recover_color(node->name.string());
      }
      if (on_surface && (value != "Cd" || instance_color) && value != "point_random" && value != "curve_random") {
        if (value.empty() || value.size() > 256)
          return recover_attribute(node->name.string(), "Surface Attribute requires a field name of 1..256 bytes", {},
                                   "material.missing_attribute", value);
        identifier(value, "attribute");
        requirements.named_attributes.insert(value);
        if (on_displacement) {
          requirements.displacement_deformation_dependent = true;
          requirements.displacement_named_attributes.insert(value);
        }
        if (attributes && !attributes->contains(value))
          return recover_attribute(node->name.string(), "Assigned geometry has no usable numeric attribute: " + value,
                                   {}, "material.missing_attribute", value);
        static_cast<ccl::AttributeNode *>(node)->set_attribute(ccl::ustring(engine_attribute_name(value)));
      }
      if (on_volume && value.empty())
        return recover_volume(node->name.string(), "Volume Attribute requires a nonempty field name");
    }
    if (name == "geometry") {
      for (const auto *output : {"Pointiness", "Random Per Island"})
        if (used_outputs.contains(node->output(output))) {
          if (!world && !light && !medium_outputs.contains(node->output(output)) &&
              geometry != ShaderGeometry::Strands) {
            if (std::string_view(output) == "Pointiness") {
              requirements.requires_mesh_pointiness = true;
              requirements.displacement_requires_pointiness |= displacement_outputs.contains(node->output(output));
            } else
              requirements.requires_mesh_islands = true;
            continue;
          }
          const std::string reason = std::string("Geometry ") + output + " requires a polygon mesh";
          if (medium_outputs.contains(node->output(output)))
            return recover_volume(node->name.string(), reason);
          return recover_attribute(node->name.string(), reason,
                                   output == std::string("Pointiness") ? "pointiness" : "random_per_island");
        }
      requirements.requires_generated |= used_outputs.contains(node->output("Tangent"));
    } else if (name == "texture_coordinate") {
      auto *coordinate = static_cast<ccl::TextureCoordinateNode *>(node);
      for (const auto *socket : {"Generated", "UV", "Object"}) {
        const bool unsupported =
            std::string(socket) == "Object" ? coordinate->get_use_transform() : coordinate->get_from_dupli();
        if (!unsupported || !used_outputs.contains(node->output(socket)))
          continue;
        const std::string reason =
            std::string("Texture Coordinate ") + socket + " requires an instancer/reference-object adapter";
        if (medium_outputs.contains(node->output(socket)))
          return recover_volume(node->name.string(), reason);
        return recover_attribute(node->name.string(), reason,
                                 std::string(socket) == "UV"       ? "UV"
                                 : std::string(socket) == "Object" ? "object"
                                                                   : "generated");
      }
      // Cycles evaluates Generated as direction in the background,
      // rather than looking up an object attribute.
      requirements.requires_generated |= !world && !light && used_outputs.contains(node->output("Generated"));
      requirements.requires_uv |= used_outputs.contains(node->output("UV"));
      if (!has_uv && used_outputs.contains(node->output("UV")))
        return recover_uv(node->name.string(), "UV");
      node_requires_uv = medium_outputs.contains(node->output("UV"));
    } else if (name == "vertex_color") {
      if (on_volume)
        return recover_volume(node->name.string(),
                              "Color Attribute is a surface input; use Volume Info or a named Volume Attribute");
      auto layer = static_cast<ccl::VertexColorNode *>(node)->get_layer_name();
      if (layer.empty())
        layer = ccl::ustring("Cd");
      const bool named = layer != ccl::ustring("Cd") || instance_color;
      if (named) {
        identifier(layer.string(), "color attribute");
        requirements.named_attributes.insert(layer.string());
        if (attributes && !attributes->contains(layer.string()))
          return recover_attribute(node->name.string(),
                                   "Assigned geometry has no usable numeric color attribute: " + layer.string(), {},
                                   "material.missing_attribute", layer.string());
        if (on_displacement) {
          requirements.displacement_named_attributes.insert(layer.string());
          requirements.displacement_deformation_dependent = true;
        }
      } else {
        requirements.requires_color = true;
        if (on_displacement) {
          requirements.displacement_requires_color = true;
          requirements.displacement_deformation_dependent = true;
        }
        if (!has_color)
          return recover_color(node->name.string());
      }
      // Lower the host's float3 Cd to the generic attribute node. Cycles'
      // Blender VertexColor path expects byte corner colors instead.
      auto *attribute = graph->create_node<ccl::AttributeNode>();
      attribute->set_attribute(named ? ccl::ustring(engine_attribute_name(layer.string())) : ccl::ustring("Cd"));
      for (const auto *output : {"Color", "Alpha"}) {
        auto *original = node->output(output);
        const auto links = original->links;
        if (displacement == original)
          displacement = attribute->output(output);
        for (auto *input : links) {
          graph->disconnect(input);
          graph->connect(attribute->output(output), input);
        }
      }
    } else if (name == "tangent") {
      auto *tangent = static_cast<ccl::TangentNode *>(node);
      if (tangent->get_direction_type() == ccl::NODE_TANGENT_UVMAP) {
        const unsigned bases = (on_surface ? 2u : 0u) | (on_displacement ? 1u : 0u);
        tangent->set_context_tangent_bases(int(bases));
        auto map = tangent->get_attribute();
        if (auto recovered = tangent_map(node, map, bases, on_displacement))
          return recovered;
        tangent->set_attribute(map);
      } else
        requirements.requires_generated = true;
    } else if (name == "vector_displacement") {
      auto *vector = static_cast<ccl::VectorDisplacementNode *>(node);
      if (vector->get_space() == ccl::NODE_NORMAL_MAP_TANGENT) {
        auto map = vector->get_attribute();
        if (auto recovered = tangent_map(node, map, 1, on_displacement))
          return recovered;
        vector->set_attribute(map);
      }
    }
    if (name == "sky_texture") {
      auto *sky = static_cast<ccl::SkyTextureNode *>(node);
      if (!sky->input("Vector")->link) {
        if (on_volume)
          return recover_volume(node->name.string(), "Volume Sky Texture requires explicit direction coordinates");
        requirements.requires_generated |= !world && !light;
      }
      std::string source_error;
      const auto range = [&](float value, float minimum, float maximum, const char *name) {
        if ((value < minimum || value > maximum) && source_error.empty())
          source_error = std::string("Sky Texture ") + name + " outside supported range";
      };
      const auto type = sky->get_sky_type();
      if (type == ccl::NODE_SKY_PREETHAM || type == ccl::NODE_SKY_HOSEK) {
        range(sky->get_turbidity(), 1, 10, "Turbidity (1..10)");
        if (type == ccl::NODE_SKY_HOSEK)
          range(sky->get_ground_albedo(), 0, 1, "Ground Albedo (0..1)");
        const auto direction = sky->get_sun_direction();
        const auto length = ccl::len(direction);
        if (!std::isfinite(length) || length < 1e-8f) {
          if (source_error.empty())
            source_error = "Sky Texture Sun Direction must be nonzero";
        } else
          sky->set_sun_direction(direction / length);
        // These legacy models have no separately evaluated solar
        // disc. Do not enable the engine's Nishita sun-guiding path.
        sky->set_sun_disc(false);
      } else {
        range(sky->get_sun_size(), 0, std::numbers::pi_v<float>, "Sun Size (0..pi radians)");
        range(sky->get_sun_intensity(), 0, std::numeric_limits<float>::max(), "Sun Intensity (nonnegative)");
        range(sky->get_altitude(), 0, 100000, "Altitude (0..100000 metres)");
        range(sky->get_air_density(), 0, 10, "Air (0..10)");
        range(sky->get_aerosol_density(), 0, 10, "Aerosols (0..10)");
        range(sky->get_ozone_density(), 0, 10, "Ozone (0..10)");
      }
      if (!source_error.empty()) {
        if (on_volume)
          return recover_volume(node->name.string(), source_error);
        if (world)
          throw WorldSourceError(source_error);
        return recover_attribute(node->name.string(), source_error, {}, "material.sky_unavailable");
      }
    } else if (name == "ies_light") {
      auto *ies = static_cast<ccl::IESLightNode *>(node);
      IesAsset asset;
      try {
        auto text = prepare_ies_asset(ies->get_filename().string(),
                                      ies->owned_ies ? *ies->owned_ies : ies->get_ies().string(), asset);
        const auto &revision = asset.content_revision;
        ies->set_resource_revision_high(std::stoull(revision.substr(0, 16), nullptr, 16));
        ies->set_resource_revision_low(std::stoull(revision.substr(16, 16), nullptr, 16));
        ies->owned_ies = std::move(text);
        ies->set_ies(ccl::ustring());
        ies->set_filename(ccl::ustring());
      } catch (const IesSourceError &error) {
        // IES recovery substitutes a neutral 1, not a diagnostic color, so its
        // emission paths stay ungated.
        bool displaced = false, normals = false, gated = false;
        auto recovered = missing_image_graph(data, node->name.string(), displaced, normals, gated, false);
        for (auto &item : recovered.nodes)
          if (item.id == node->name.string()) {
            item.definition = "cycles/value/1";
            item.parameters = {{"value", 1.f}};
          }
        for (auto &link : recovered.links)
          if (link.from.node == node->name.string())
            link.from.socket = "value";
        auto result = prepare_shader_graph(recovered, requirements, policy, aovs, context, has_uv, has_color, geometry,
                                           attributes, instance_attributes);
        requirements.ies.push_back(std::move(asset));
        requirements.source_diagnostics.push_back({light ? "light.ies_unavailable" : "texture.ies_unavailable",
                                                   ies->get_filename().string(), node->name.string(), error.what(),
                                                   "Disabled the IES modulation; retained independent color and "
                                                   "emission; affected normal/displacement branches are disabled"});
        return result;
      }
      requirements.ies.push_back(std::move(asset));
    } else if (name == "environment_texture") {
      auto *image = static_cast<ccl::EnvironmentTextureNode *>(node);
      TextureAsset asset;
      try {
        asset = prepare_texture_asset(image->get_filename().string(), image->get_colorspace().string(), {}, policy,
                                      data.image_time.value_or(0.));
      } catch (const TextureSourceError &error) {
        return recover_image(node->name.string(), image->get_filename().string(), error);
      }
      if (!asset.tiles.empty())
        throw std::invalid_argument("Environment Texture requires one image; UDIM environments are unsupported");
      image->set_colorspace(ccl::ustring(asset.colorspace));
      image->set_filename(ccl::ustring(asset.filename));
      image->set_resource_revision_high(std::stoull(asset.revision.substr(0, 16), nullptr, 16));
      image->set_resource_revision_low(std::stoull(asset.revision.substr(16, 16), nullptr, 16));
      image->cache_namespace = asset.cache_namespace;
      asset.shader_node = node->name.string();
      requirements.textures.push_back(std::move(asset));
    } else if (name == "image_texture") {
      auto *image = static_cast<ccl::ImageTextureNode *>(node);
      if (!has_uv && !node->input("Vector")->link)
        return recover_uv(node->name.string());
      if (image->get_colorspace() == ccl::ustring("auto"))
        image->set_colorspace(ccl::ustring());
      const auto &engine_tiles = image->get_tiles();
      const std::vector<int> tiles(engine_tiles.begin(), engine_tiles.end());
      TextureAsset asset;
      try {
        asset = prepare_texture_asset(image->get_filename().string(), image->get_colorspace().string(), tiles, policy,
                                      data.image_time.value_or(0.));
      } catch (const TextureSourceError &error) {
        return recover_image(node->name.string(), image->get_filename().string(), error);
      }
      image->set_colorspace(ccl::ustring(asset.colorspace));
      image->set_filename(ccl::ustring(asset.filename));
      image->set_resource_revision_high(std::stoull(asset.revision.substr(0, 16), nullptr, 16));
      image->set_resource_revision_low(std::stoull(asset.revision.substr(16, 16), nullptr, 16));
      image->cache_namespace = asset.cache_namespace;
      ccl::array<int> resolved;
      resolved.resize(asset.tiles.size());
      std::copy(asset.tiles.begin(), asset.tiles.end(), resolved.begin());
      image->set_tiles(resolved);
      if (!node->input("Vector")->link) {
        requirements.requires_uv = true;
        node_requires_uv = true;
      }
      asset.shader_node = node->name.string();
      requirements.textures.push_back(std::move(asset));
    } else if (name == "uvmap") {
      auto *uv = static_cast<ccl::UVMapNode *>(node);
      if (light && !uv->get_attribute().empty() && uv->get_attribute() != ccl::ustring("uv"))
        throw LightSourceError("Analytic lights have only their generated UV coordinates, not named mesh UV fields");
      if (uv->get_from_dupli()) {
        if (on_volume)
          return recover_volume(node->name.string(),
                                "UV Map from an instancer requires an instance-coordinate adapter");
        return recover_attribute(node->name.string(),
                                 "UV Map from an instancer requires an instance-coordinate adapter");
      }
      const auto map = uv->get_attribute().string();
      if (!map.empty() && map != "uv") {
        identifier(map, "UV map");
        requirements.named_attributes.insert(map);
        if (attributes && (!attributes->contains(map) || attributes->at(map) < 2))
          return recover_attribute(node->name.string(),
                                   "Assigned geometry has no usable two/three-component UV map: " + map, {},
                                   "material.missing_attribute", map);
        uv->set_attribute(ccl::ustring(engine_attribute_name(map)));
        if (on_displacement) {
          requirements.displacement_named_attributes.insert(map);
          requirements.displacement_deformation_dependent = true;
        }
      } else {
        if (!has_uv)
          return recover_uv(node->name.string());
        uv->set_attribute(ccl::ustring());
        requirements.requires_uv = true;
        node_requires_uv = true;
      }
    } else if (name == "normal_map") {
      auto *normal = static_cast<ccl::NormalMapNode *>(node);
      if (normal->get_space() == ccl::NODE_NORMAL_MAP_TANGENT) {
        const unsigned bases =
            (on_surface ? (normal->get_base() == ccl::NODE_NORMAL_MAP_BASE_DISPLACED ? 2u : 1u) : 0u) |
            (on_displacement ? 1u : 0u);
        normal->set_context_tangent_bases(int(bases));
        auto map = normal->get_attribute();
        if (auto recovered = tangent_map(node, map, bases, on_displacement))
          return recovered;
        normal->set_attribute(map);
      }
    }
    const bool displacement_uv =
        on_displacement &&
        (node_requires_uv || (name == "texture_coordinate" && displacement_outputs.contains(node->output("UV"))));
    if (displacement_uv) {
      requirements.displacement_requires_uv = true;
      requirements.displacement_deformation_dependent = true;
    }
    if (on_volume && node_requires_uv)
      return recover_volume(node->name.string(),
                            "Volume textures require explicit coordinates; a medium has no surface UV map");
    if (world && (requirements.requires_uv || requirements.requires_generated))
      throw std::invalid_argument(
          "World textures require direction coordinates; surface UV/Generated attributes are unavailable");
    if (!on_volume)
      continue;
    const auto field = [&](const std::string &value) {
      if (!value.empty()) {
        identifier(value, "volume field");
        requirements.volume_fields.insert(value);
      }
    };
    if (name == "attribute")
      field(static_cast<ccl::AttributeNode *>(node)->get_attribute().string());
    else if (name == "volume_info") {
      for (const auto *socket : {"Color", "Density", "Flame", "Temperature"})
        if (medium_outputs.contains(node->output(socket))) {
          const std::map<std::string, std::string> names = {
              {"Color", "color"}, {"Density", "density"}, {"Flame", "flame"}, {"Temperature", "temperature"}};
          field(names.at(socket));
        }
    } else if (name == "principled_volume") {
      const auto *volume = static_cast<ccl::PrincipledVolumeNode *>(node);
      const auto typed_field = [&](const ccl::ustring &name, bool vector) {
        const auto value = name.string();
        field(value);
        if (!value.empty())
          (vector ? requirements.vector_volume_fields : requirements.scalar_volume_fields).insert(value);
      };
      if (node->input("Density")->link || volume->get_density() > 0) {
        typed_field(volume->get_density_attribute(), false);
        typed_field(volume->get_color_attribute(), true);
      }
      if (node->input("Blackbody Intensity")->link || volume->get_blackbody_intensity() > 0)
        typed_field(volume->get_temperature_attribute(), false);
    }
  }
  if (world && requirements.volume && !requirements.volume_fields.empty())
    return recover_volume(data.volume->node,
                          "World volumes have no geometry grid named " + *requirements.volume_fields.begin());
  if (geometry == ShaderGeometry::Mesh && requirements.volume && !requirements.volume_fields.empty()) {
    // A polygon-mesh medium has no voxel attributes: Cycles' lookups find nothing
    // (find_attribute with PRIM_NONE accepts only mesh, voxel and object elements),
    // so Principled Volume keeps its unscaled density, colour and temperature and
    // an Attribute node returns its Missing value, exactly as Blender renders it.
    // These names are engine lookups, not sparse-grid requirements.
    for (const auto &name : requirements.volume_fields)
      if ((attributes && attributes->contains(name)) || (instance_attributes && instance_attributes->contains(name)))
        requirements.source_diagnostics.push_back(
            {"material.mesh_volume_attribute", "", "",
             "Volume shader reads '" + name + "' as a volume field; the mesh attribute is not used (as in Blender)",
             "Rendering the medium with the shader's own values; use a volume (VDB) for attribute-driven density",
             "info", name});
    requirements.volume_fields.clear();
    requirements.scalar_volume_fields.clear();
    requirements.vector_volume_fields.clear();
  }
  if (surface)
    graph->connect(surface, graph->output()->input("Surface"));
  if (volume)
    graph->connect(volume, graph->output()->input("Volume"));
  if (displacement)
    graph->connect(displacement, graph->output()->input("Displacement"));
  return graph;
}
} // namespace cycles_houdini
