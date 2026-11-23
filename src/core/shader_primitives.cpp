// SPDX-License-Identifier: Apache-2.0
#include "scene/constant_fold.h"
#include "scene/shader_nodes.h"
#include "shader_switches.h"
#include <cmath>
#include <functional>
#include <limits>
#include <set>

namespace cycles_houdini {
namespace {
bool assign(ccl::ShaderNode &node, const ccl::SocketType &socket, const ShaderValue &value, bool conversion) {
  using Socket = ccl::SocketType;
  if (const auto *v = std::get_if<std::string>(&value)) {
    if (socket.type == Socket::ENUM && socket.enum_values && socket.enum_values->exists(ccl::ustring(*v))) {
      node.set(socket, (*socket.enum_values)[ccl::ustring(*v)]);
      return true;
    }
    return false;
  }
  if (const auto *v = std::get_if<std::array<float, 3>>(&value)) {
    if (!Socket::is_float3(socket.type)) {
      return false;
    }
    for (float f : *v) {
      if (!std::isfinite(f)) {
        return false;
      }
    }
    node.set(socket, ccl::make_float3((*v)[0], (*v)[1], (*v)[2]));
    return true;
  }
  double number;
  if (const auto *v = std::get_if<bool>(&value)) {
    number = *v;
  } else if (const auto *v = std::get_if<int>(&value)) {
    number = *v;
  } else if (const auto *v = std::get_if<float>(&value)) {
    number = *v;
  } else {
    return false;
  }
  if (!std::isfinite(number)) {
    return false;
  }
  if (socket.type == Socket::FLOAT) {
    node.set(socket, float(number));
    return true;
  }
  if (socket.type == Socket::INT) {
    if (!conversion && !std::holds_alternative<int>(value)) {
      return false;
    }
    if (number < std::numeric_limits<int>::min() || number > std::numeric_limits<int>::max()) {
      return false;
    }
    node.set(socket, int(number));
    return true;
  }
  if (socket.type == Socket::BOOLEAN) {
    if (!conversion && !std::holds_alternative<bool>(value)) {
      return false;
    }
    node.set(socket, number > 0);
    return true;
  }
  if (conversion && Socket::is_float3(socket.type)) {
    node.set(socket, ccl::make_float3(float(number)));
    return true;
  }
  return false;
}
} // namespace
std::optional<ShaderValue> shader_constant_value(const ShaderGraphData &graph, const ShaderEndpoint &endpoint) {
  std::map<std::string, const ShaderNodeData *> nodes;
  std::map<std::pair<std::string, std::string>, ShaderEndpoint> inputs;
  for (const auto &node : graph.nodes) {
    nodes.emplace(node.id, &node);
  }
  for (const auto &link : graph.links) {
    inputs.emplace(std::pair(link.to.node, link.to.socket), link.from);
  }
  std::set<std::string> active;
  std::map<std::pair<std::string, std::string>, std::optional<ShaderValue>> memo;
  const std::set<std::string> supported = {
      "math",        "integer_math", "boolean_math", "vector_math", "get_vector_component",
      "combine_xyz", "separate_xyz", "mix_float",    "mix_vector"};
  std::function<std::optional<ShaderValue>(const ShaderEndpoint &)> evaluate;
  evaluate = [&](const ShaderEndpoint &output) -> std::optional<ShaderValue> {
    const auto key = std::pair(output.node, output.socket);
    if (const auto cached = memo.find(key); cached != memo.end()) {
      return cached->second;
    }
    const auto found = nodes.find(output.node);
    if (found == nodes.end() || !active.insert(output.node).second) {
      return {};
    }
    const auto finish = [&](std::optional<ShaderValue> result) {
      active.erase(output.node);
      memo.emplace(key, result);
      return result;
    };
    const auto &data = *found->second;
    if (data.definition == "cycles/value/1" && output.socket == "value") {
      const auto p = data.parameters.find("value");
      return finish(p == data.parameters.end() ? ShaderValue{0.f} : p->second);
    }
    if (data.definition == "cycles/color/1" && output.socket == "color") {
      const auto p = data.parameters.find("value");
      return finish(p == data.parameters.end() ? ShaderValue{std::array<float, 3>{.8f, .8f, .8f}} : p->second);
    }
    if (!data.definition.starts_with("cycles/") || !data.definition.ends_with("/1")) {
      return finish({});
    }
    const auto name = data.definition.substr(7, data.definition.size() - 9);
    if (!supported.contains(name)) {
      return finish({});
    }
    const auto *type = ccl::NodeType::find(ccl::ustring(name));
    if (!type || !type->create) {
      return finish({});
    }
    const auto *socket = type->find_output(ccl::ustring(output.socket));
    if (!socket) {
      return finish({});
    }
    auto owned = ccl::make_unique<ccl::ShaderGraph>();
    auto *node = owned->create_node(type);
    for (const auto &[name, value] : data.parameters) {
      const auto *input = type->find_input(ccl::ustring(name));
      if (!input || !assign(*node, *input, value, false)) {
        return finish({});
      }
    }
    for (const auto &input : type->inputs) {
      const auto source = inputs.find({data.id, input.name.string()});
      if (source == inputs.end()) {
        continue;
      }
      const auto value = evaluate(source->second);
      if (!value || !assign(*node, input, *value, true)) {
        return finish({});
      }
    }
    ccl::ShaderNode *receiver;
    const char *input;
    if (socket->type == ccl::SocketType::INT) {
      receiver = owned->create_node<ccl::IntegerMathNode>();
      input = "Value1";
    } else if (socket->type == ccl::SocketType::FLOAT) {
      receiver = owned->create_node<ccl::MathNode>();
      input = "Value1";
    } else if (ccl::SocketType::is_float3(socket->type)) {
      receiver = owned->create_node<ccl::VectorMathNode>();
      input = "Vector1";
    } else {
      return finish({});
    }
    auto *target = receiver->input(input);
    auto *from = node->output(socket->ui_name.c_str());
    owned->connect(from, target);
    const ccl::ConstantFolder folder(owned.get(), node, from, nullptr);
    node->constant_fold(folder);
    if (target->link) {
      return finish({});
    }
    if (socket->type == ccl::SocketType::INT) {
      return finish(receiver->get_int(target->socket_type));
    }
    if (socket->type == ccl::SocketType::FLOAT) {
      return finish(receiver->get_float(target->socket_type));
    }
    const auto value = receiver->get_float3(target->socket_type);
    return finish(std::array<float, 3>{value.x, value.y, value.z});
  };
  return evaluate(endpoint);
}
} // namespace cycles_houdini
