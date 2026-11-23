// SPDX-License-Identifier: Apache-2.0
#include "cycles_houdini/shader.h"
#include "cycles_houdini/session.h"
#include "shader_switches.h"
#include <algorithm>
#include <cmath>
#include <iomanip>
#include <limits>
#include <sstream>
#include <set>
#include <stdexcept>
#include "graph/node.h"
#include "scene/shader_nodes.h"
#include "util/transform.h"

namespace cycles_houdini {
bool shader_pointiness_request(const ShaderGraphData &graph, const std::set<std::string> &aovs) {
  if (std::any_of(graph.nodes.begin(), graph.nodes.end(),
                  [](const auto &n) { return shader_switch_definition(n.definition); }))
    return shader_pointiness_request(lower_shader_switches(graph), aovs);
  const auto pointiness = [](std::string value) {
    for (char &c : value)
      if (c >= 'A' && c <= 'Z')
        c += char('a' - 'A');
    return value == "pointiness";
  };
  std::set<std::string> reachable;
  if (!graph.surface.node.empty())
    reachable.insert(graph.surface.node);
  if (graph.displacement)
    reachable.insert(graph.displacement->node);
  for (const auto &node : graph.nodes)
    if (node.definition == "cycles/aov_output/1") {
      const auto name = node.parameters.find("name");
      if (name != node.parameters.end())
        if (const auto *text = std::get_if<std::string>(&name->second); text && aovs.contains(*text))
          reachable.insert(node.id);
    }
  bool changed = true;
  while (changed) {
    changed = false;
    for (const auto &link : graph.links)
      if (reachable.contains(link.to.node))
        changed |= reachable.insert(link.from.node).second;
  }
  for (const auto &node : graph.nodes)
    if (node.definition == "cycles/geometry/1" && reachable.contains(node.id)) {
      if (graph.surface.node == node.id && pointiness(graph.surface.socket))
        return true;
      if (graph.displacement && graph.displacement->node == node.id && pointiness(graph.displacement->socket))
        return true;
      for (const auto &link : graph.links)
        if (link.from.node == node.id && reachable.contains(link.to.node) && pointiness(link.from.socket))
          return true;
    }
  return false;
}
bool shader_generated_request(const ShaderGraphData &graph) {
  if (std::any_of(graph.nodes.begin(), graph.nodes.end(),
                  [](const auto &n) { return shader_switch_definition(n.definition); }))
    return shader_generated_request(lower_shader_switches(graph));
  const auto lower = [](std::string text) {
    for (char &c : text)
      if (c >= 'A' && c <= 'Z')
        c += char('a' - 'A');
    return text;
  };
  std::set<std::string> reachable{graph.surface.node};
  if (graph.volume)
    reachable.insert(graph.volume->node);
  if (graph.displacement)
    reachable.insert(graph.displacement->node);
  for (const auto &node : graph.nodes)
    if (node.definition == "cycles/aov_output/1")
      reachable.insert(node.id);
  bool changed = true;
  while (changed) {
    changed = false;
    for (const auto &link : graph.links)
      if (reachable.contains(link.to.node))
        changed |= reachable.insert(link.from.node).second;
  }
  std::map<std::string, std::set<std::string>> inputs, outputs;
  for (const auto &link : graph.links)
    if (reachable.contains(link.to.node)) {
      inputs[link.to.node].insert(lower(link.to.socket));
      outputs[link.from.node].insert(lower(link.from.socket));
    }
  outputs[graph.surface.node].insert(lower(graph.surface.socket));
  if (graph.volume)
    outputs[graph.volume->node].insert(lower(graph.volume->socket));
  if (graph.displacement)
    outputs[graph.displacement->node].insert(lower(graph.displacement->socket));
  static const auto defaults = []() {
    std::map<std::string, std::set<std::string>> result;
    for (const auto &definition : shader_definitions())
      for (const auto &socket : definition.inputs)
        if (std::find(socket.default_links.begin(), socket.default_links.end(), "texture_generated") !=
            socket.default_links.end())
          result[definition.id].insert(socket.id);
    return result;
  }();
  for (const auto &node : graph.nodes)
    if (reachable.contains(node.id)) {
      if (node.definition == "cycles/texture_coordinate/1" && outputs[node.id].contains("generated"))
        return true;
      if (node.definition == "cycles/geometry/1" && outputs[node.id].contains("tangent"))
        return true;
      if (node.definition == "cycles/tangent/1") {
        const auto found = node.parameters.find("direction_type");
        if (found == node.parameters.end() || std::get<std::string>(found->second) != "uv_map")
          return true;
      }
      if (node.definition == "cycles/sky_texture/1" && !inputs[node.id].contains("vector") &&
          !node.parameters.contains("vector"))
        return true;
      if (const auto found = defaults.find(node.definition); found != defaults.end())
        for (const auto &socket : found->second)
          if (!inputs[node.id].contains(lower(socket)) && !node.parameters.contains(socket))
            return true;
    }
  return false;
}
std::set<std::string> shader_attribute_requests(const ShaderGraphData &graph) {
  if (std::any_of(graph.nodes.begin(), graph.nodes.end(),
                  [](const auto &n) { return shader_switch_definition(n.definition); }))
    return shader_attribute_requests(lower_shader_switches(graph));
  std::set<std::string> reachable{graph.surface.node}, result;
  if (graph.displacement)
    reachable.insert(graph.displacement->node);
  for (const auto &node : graph.nodes)
    if (node.definition == "cycles/aov_output/1")
      reachable.insert(node.id);
  bool changed = true;
  while (changed) {
    changed = false;
    for (const auto &link : graph.links)
      if (reachable.contains(link.to.node))
        changed |= reachable.insert(link.from.node).second;
  }
  for (const auto &node : graph.nodes)
    if (reachable.contains(node.id)) {
      const char *parameter = node.definition == "cycles/attribute/1" || node.definition == "cycles/uvmap/1"
                                  ? "attribute"
                              : node.definition == "cycles/vertex_color/1" ? "layer_name"
                                                                           : nullptr;
      const auto selected = [&](const char *key, const char *fallback) {
        const auto found = node.parameters.find(key);
        return found != node.parameters.end() && std::holds_alternative<std::string>(found->second)
                   ? std::get<std::string>(found->second)
                   : std::string(fallback);
      };
      if ((node.definition == "cycles/tangent/1" && selected("direction_type", "radial") == "uv_map") ||
          ((node.definition == "cycles/normal_map/1" || node.definition == "cycles/vector_displacement/1") &&
           selected("space", "tangent") == "tangent"))
        parameter = "attribute";
      if (node.definition == "cycles/vertex_color/1" && selected("layer_name", "").empty()) {
        result.insert("Cd");
        continue;
      }
      if (!parameter)
        continue;
      const auto found = node.parameters.find(parameter);
      if (found == node.parameters.end())
        continue;
      const auto *name = std::get_if<std::string>(&found->second);
      if (name && !name->empty() && (*name != "uv" || node.definition == "cycles/attribute/1") &&
          *name != "point_random" && *name != "curve_random")
        result.insert(*name);
    }
  return result;
}

std::set<std::string> shader_volume_attribute_requests(const ShaderGraphData &graph) {
  if (std::any_of(graph.nodes.begin(), graph.nodes.end(),
                  [](const auto &n) { return shader_switch_definition(n.definition); }))
    return shader_volume_attribute_requests(lower_shader_switches(graph));
  std::set<std::string> result;
  if (!graph.volume)
    return result;
  const auto lower = [](std::string text) {
    for (char &c : text)
      if (c >= 'A' && c <= 'Z')
        c += char('a' - 'A');
    return text;
  };
  std::set<std::string> reachable{graph.volume->node};
  bool changed = true;
  while (changed) {
    changed = false;
    for (const auto &link : graph.links)
      if (reachable.contains(link.to.node))
        changed |= reachable.insert(link.from.node).second;
  }
  const auto text = [](const ShaderNodeData &node, const char *key, const char *fallback) {
    const auto found = node.parameters.find(key);
    return found != node.parameters.end() && std::holds_alternative<std::string>(found->second)
               ? std::get<std::string>(found->second)
               : std::string(fallback);
  };
  const auto number = [](const ShaderNodeData &node, const char *key, float fallback) {
    const auto found = node.parameters.find(key);
    return found != node.parameters.end() && std::holds_alternative<float>(found->second)
               ? std::get<float>(found->second)
               : fallback;
  };
  const auto linked = [&](const ShaderNodeData &node, const char *socket) {
    return std::any_of(graph.links.begin(), graph.links.end(),
                       [&](const auto &link) { return link.to.node == node.id && lower(link.to.socket) == socket; });
  };
  const auto add = [&](const std::string &name) {
    if (!name.empty())
      result.insert(name);
  };
  // The names Cycles' own nodes look up in a medium (PrincipledVolumeNode::attributes,
  // AttributeNode, VolumeInfoNode::expand), with the engine's socket defaults.
  for (const auto &node : graph.nodes) {
    if (!reachable.contains(node.id))
      continue;
    if (node.definition == "cycles/attribute/1")
      add(text(node, "attribute", ""));
    else if (node.definition == "cycles/principled_volume/1") {
      if (linked(node, "density") || number(node, "density", 1.f) > 0.f) {
        add(text(node, "density_attribute", "density"));
        add(text(node, "color_attribute", ""));
      }
      if (linked(node, "blackbody_intensity") || number(node, "blackbody_intensity", 0.f) > 0.f)
        add(text(node, "temperature_attribute", "temperature"));
    } else if (node.definition == "cycles/volume_info/1")
      for (const auto &link : graph.links)
        if (link.from.node == node.id && reachable.contains(link.to.node)) {
          const auto socket = lower(link.from.socket);
          if (socket == "color" || socket == "density" || socket == "flame" || socket == "temperature")
            add(socket);
        }
  }
  return result;
}

namespace {
using Socket = ccl::SocketType;
std::string quoted(const std::string &text) {
  static constexpr char hex[] = "0123456789abcdef";
  std::string result = "\"";
  for (const unsigned char ch : text) {
    if (ch == '"' || ch == '\\') {
      result += '\\';
      result += char(ch);
    } else if (ch < 32) {
      result += "\\u00";
      result += hex[ch >> 4];
      result += hex[ch & 15];
    } else
      result += char(ch);
  }
  return result + '"';
}
void scalar(std::ostream &out, float value) {
  if (!std::isfinite(value))
    throw std::runtime_error("Nonfinite upstream shader default");
  out << value;
}
template <class T> void vector2(std::ostream &out, const T &value) {
  out << '[';
  scalar(out, value.x);
  out << ',';
  scalar(out, value.y);
  out << ']';
}
template <class T> void vector3(std::ostream &out, const T &value) {
  out << '[';
  scalar(out, value.x);
  out << ',';
  scalar(out, value.y);
  out << ',';
  scalar(out, value.z);
  out << ']';
}
void matrix(std::ostream &out, const ccl::Transform &value) {
  out << '[';
  bool comma = false;
  for (const auto row : {value.x, value.y, value.z})
    for (const float entry : {row.x, row.y, row.z, row.w}) {
      if (comma)
        out << ',';
      comma = true;
      scalar(out, entry);
    }
  out << ']';
}
template <class Array, class Writer> void array(std::ostream &out, const Array &values, Writer writer) {
  out << '[';
  bool comma = false;
  for (const auto &value : values) {
    if (comma)
      out << ',';
    comma = true;
    writer(value);
  }
  out << ']';
}
std::string default_value(const ccl::Node &node, const Socket &socket) {
  std::ostringstream out;
  out << std::setprecision(std::numeric_limits<float>::max_digits10);
  switch (socket.type) {
  case Socket::BOOLEAN:
    out << (node.get_bool(socket) ? "true" : "false");
    break;
  case Socket::INT:
    out << node.get_int(socket);
    break;
  case Socket::UINT:
    out << node.get_uint(socket);
    break;
  case Socket::UINT64:
    out << node.get_uint64(socket);
    break;
  case Socket::FLOAT:
    scalar(out, node.get_float(socket));
    break;
  case Socket::COLOR:
  case Socket::VECTOR:
  case Socket::POINT:
  case Socket::NORMAL:
    vector3(out, node.get_float3(socket));
    break;
  case Socket::POINT2:
    vector2(out, node.get_float2(socket));
    break;
  case Socket::STRING:
    out << quoted(node.get_string(socket).string());
    break;
  case Socket::ENUM: {
    const auto value = node.get_int(socket);
    if (!socket.enum_values || !socket.enum_values->exists(value))
      throw std::runtime_error("Invalid upstream shader enum default: " + socket.name.string());
    out << quoted((*socket.enum_values)[value].string());
    break;
  }
  case Socket::TRANSFORM:
    matrix(out, node.get_transform(socket));
    break;
  case Socket::BOOLEAN_ARRAY:
    array(out, node.get_bool_array(socket), [&](bool value) { out << (value ? "true" : "false"); });
    break;
  case Socket::INT_ARRAY:
    array(out, node.get_int_array(socket), [&](int value) { out << value; });
    break;
  case Socket::FLOAT_ARRAY:
    array(out, node.get_float_array(socket), [&](float value) { scalar(out, value); });
    break;
  case Socket::COLOR_ARRAY:
  case Socket::VECTOR_ARRAY:
  case Socket::POINT_ARRAY:
  case Socket::NORMAL_ARRAY:
    array(out, node.get_float3_array(socket), [&](const auto &value) { vector3(out, value); });
    break;
  case Socket::POINT2_ARRAY:
    array(out, node.get_float2_array(socket), [&](const auto &value) { vector2(out, value); });
    break;
  case Socket::STRING_ARRAY:
    array(out, node.get_string_array(socket), [&](const auto &value) { out << quoted(value.string()); });
    break;
  case Socket::TRANSFORM_ARRAY:
    array(out, node.get_transform_array(socket), [&](const auto &value) { matrix(out, value); });
    break;
  // Closure inputs are connections. Native node pointers are deliberately
  // not serialized as values; authored adapters must resolve resources.
  default:
    out << "null";
    break;
  }
  return out.str();
}
ShaderSocketDefinition describe(const Socket &socket, const ccl::Node *node) {
  ShaderSocketDefinition result;
  result.id = socket.name.string();
  result.label = socket.ui_name.string();
  result.type = Socket::type_name(socket.type).string();
  result.default_json = node ? default_value(*node, socket) : "null";
  result.linkable = !node || (socket.flags & Socket::LINKABLE) != 0;
  result.internal_svm = (socket.flags & Socket::SVM_INTERNAL) != 0;
  result.internal_osl = (socket.flags & Socket::OSL_INTERNAL) != 0;
  if (node)
    switch (socket.type) {
    case Socket::BOOLEAN:
      result.authoring_default = node->get_bool(socket);
      break;
    case Socket::INT:
      result.authoring_default = node->get_int(socket);
      break;
    case Socket::FLOAT:
      result.authoring_default = node->get_float(socket);
      break;
    case Socket::COLOR:
    case Socket::VECTOR:
    case Socket::POINT:
    case Socket::NORMAL: {
      const auto value = node->get_float3(socket);
      result.authoring_default = std::array<float, 3>{value.x, value.y, value.z};
      break;
    }
    case Socket::STRING:
      result.authoring_default = node->get_string(socket).string();
      break;
    case Socket::ENUM:
      result.authoring_default = (*socket.enum_values)[node->get_int(socket)].string();
      break;
    case Socket::INT_ARRAY: {
      const auto &values = node->get_int_array(socket);
      result.authoring_default = std::vector<int>(values.begin(), values.end());
      break;
    }
    default:
      break;
    }
  for (const auto &[flag, name] :
       std::vector<std::pair<int, const char *>>{{Socket::LINK_TEXTURE_GENERATED, "texture_generated"},
                                                 {Socket::LINK_TEXTURE_NORMAL, "texture_normal"},
                                                 {Socket::LINK_TEXTURE_UV, "texture_uv"},
                                                 {Socket::LINK_TEXTURE_INCOMING, "texture_incoming"},
                                                 {Socket::LINK_INCOMING, "incoming"},
                                                 {Socket::LINK_NORMAL, "normal"},
                                                 {Socket::LINK_POSITION, "position"},
                                                 {Socket::LINK_TANGENT, "tangent"},
                                                 {Socket::LINK_OSL_INITIALIZER, "osl_initializer"}})
    if (socket.flags & flag)
      result.default_links.emplace_back(name);
  if (socket.enum_values)
    for (const auto &[token, value] : *socket.enum_values)
      result.enum_values.push_back({token.string(), value});
  std::sort(result.enum_values.begin(), result.enum_values.end(),
            [](const auto &a, const auto &b) { return a.token < b.token; });
  return result;
}
void sockets(std::ostream &out, const std::vector<ShaderSocketDefinition> &values) {
  out << '[';
  bool comma = false;
  for (const auto &socket : values) {
    if (comma)
      out << ',';
    comma = true;
    out << "{\"id\":" << quoted(socket.id) << ",\"label\":" << quoted(socket.label)
        << ",\"type\":" << quoted(socket.type) << ",\"default\":" << socket.default_json
        << ",\"linkable\":" << (socket.linkable ? "true" : "false")
        << ",\"internal_svm\":" << (socket.internal_svm ? "true" : "false")
        << ",\"internal_osl\":" << (socket.internal_osl ? "true" : "false") << ",\"default_links\":";
    array(out, socket.default_links, [&](const auto &name) { out << quoted(name); });
    out << ",\"enum_values\":";
    array(out, socket.enum_values, [&](const auto &item) {
      out << "{\"token\":" << quoted(item.token) << ",\"engine_value\":" << item.engine_value << '}';
    });
    out << '}';
  }
  out << ']';
}
} // namespace

std::vector<ShaderDefinition> shader_definitions() {
  // Explicitly retain the main node-definition translation unit in static
  // builds. Registry initialization itself is synchronized upstream.
  ccl::DiffuseBsdfNode::get_node_type();
  auto names = ccl::NodeType::type_names();
  std::sort(names.begin(), names.end(), [](const auto &a, const auto &b) { return a.string() < b.string(); });
  std::vector<ShaderDefinition> result;
  for (const auto &name : names) {
    const auto *type = ccl::NodeType::find(name);
    if (type->type != ccl::NodeType::SHADER || !type->create)
      continue;
    auto node = type->create(type);
    ShaderDefinition definition;
    definition.engine_type = name.string();
    definition.id = "cycles/" + definition.engine_type + "/1";
    for (const auto &input : type->inputs)
      definition.inputs.push_back(describe(input, node.get()));
    if (definition.engine_type == "float_curve" || definition.engine_type == "rgb_curves" ||
        definition.engine_type == "vector_curves") {
      ShaderSocketDefinition source;
      source.id = "curve_source";
      source.label = "Owned Curve Controls";
      source.type = "int_array";
      source.default_json = "[]";
      source.authoring_default = std::vector<int>{};
      definition.inputs.push_back(std::move(source));
    }
    for (const auto &output : type->outputs)
      definition.outputs.push_back(describe(output, nullptr));
    result.push_back(std::move(definition));
  }
  auto selectors = shader_switch_definitions();
  result.insert(result.end(), selectors.begin(), selectors.end());
  std::sort(result.begin(), result.end(), [](const auto &a, const auto &b) { return a.engine_type < b.engine_type; });
  return result;
}
ShaderGraphData displacement_snapshot(ShaderGraphData graph) {
  if (std::any_of(graph.nodes.begin(), graph.nodes.end(),
                  [](const auto &n) { return shader_switch_definition(n.definition); }))
    graph = lower_shader_switches(graph);
  if (!graph.displacement)
    return graph;
  std::map<std::string, std::vector<std::string>> upstream;
  for (const auto &link : graph.links)
    upstream[link.to.node].push_back(link.from.node);
  std::set<std::string> active;
  std::vector<std::string> pending{graph.displacement->node};
  while (!pending.empty()) {
    auto id = std::move(pending.back());
    pending.pop_back();
    if (active.insert(id).second)
      for (const auto &source : upstream[id])
        pending.push_back(source);
  }
  std::erase_if(graph.nodes, [&](const auto &node) { return !active.contains(node.id); });
  std::erase_if(graph.links, [&](const auto &link) { return !active.contains(link.to.node); });
  std::string id = "__displacement_surface";
  while (active.contains(id))
    id += '_';
  graph.nodes.push_back({id, "cycles/diffuse_bsdf/1", {{"color", std::array<float, 3>{0, 0, 0}}}});
  graph.surface = {id, "BSDF"};
  graph.volume.reset();
  graph.displacement_time_dependent = false;
  if (!std::any_of(graph.nodes.begin(), graph.nodes.end(), [](const auto &node) {
        const auto found = node.parameters.find("filename");
        return found != node.parameters.end() && std::holds_alternative<std::string>(found->second) &&
               std::get<std::string>(found->second).starts_with("op:");
      }))
    graph.image_time.reset();
  return graph;
}
std::string shader_catalogue_json() {
  std::ostringstream out;
  const auto supported = surface_graph_definitions();
  const auto volume_supported = volume_graph_definitions();
  const auto world_supported = world_graph_definitions();
  out << "{\"schema_version\":1,\"engine_commit\":" << quoted(capabilities().engine_commit)
      << ",\"scope\":\"Engine shader reflection plus authored tree-inlining definitions, identified by "
         "definition_source. Rendering certification is separate.\",\"definitions\":[";
  bool comma = false;
  for (const auto &definition : shader_definitions()) {
    if (comma)
      out << ',';
    comma = true;
    out << "{\"id\":" << quoted(definition.id) << ",\"engine_type\":" << quoted(definition.engine_type)
        << ",\"definition_source\":" << quoted(shader_switch_definition(definition.id) ? "tree_inline" : "engine")
        << ",\"definition_version\":" << definition.definition_version << ",\"surface_graph_adapter\":"
        << (std::find(supported.begin(), supported.end(), definition.id) != supported.end() ? "true" : "false")
        << ",\"volume_graph_adapter\":"
        << (std::find(volume_supported.begin(), volume_supported.end(), definition.id) != volume_supported.end()
                ? "true"
                : "false")
        << ",\"world_graph_adapter\":"
        << (std::find(world_supported.begin(), world_supported.end(), definition.id) != world_supported.end() ? "true"
                                                                                                              : "false")
        << ",\"inputs\":";
    sockets(out, definition.inputs);
    out << ",\"outputs\":";
    sockets(out, definition.outputs);
    out << '}';
  }
  out << "]}";
  return out.str();
}
} // namespace cycles_houdini
