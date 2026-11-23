// SPDX-License-Identifier: Apache-2.0
#include "../src/core/shader_switches.h"
#include <algorithm>
#include <cmath>
#include <functional>
#include <iostream>
#include <map>
#include <stdexcept>
using namespace cycles_houdini;
// The standalone structural suite has no Cycles library. Runtime/native suites
// exercise the real upstream ConstantFolder implementation separately.
namespace cycles_houdini {
std::vector<ShaderDefinition> shader_definitions() { return {}; }
std::optional<ShaderValue> shader_constant_value(const ShaderGraphData &graph, const ShaderEndpoint &endpoint) {
  for (const auto &node : graph.nodes)
    if (node.id == endpoint.node && node.definition == "cycles/value/1" && endpoint.socket == "value") {
      const auto value = node.parameters.find("value");
      return value == node.parameters.end() ? ShaderValue{0.f} : value->second;
    }
  return {};
}
} // namespace cycles_houdini
void require(bool condition, const char *message) {
  if (!condition)
    throw std::runtime_error(message);
}
// Independent execution of the small resulting scalar graph. The oracle below
// uses direct conditional/index selection rather than copying the Mix chain.
double run(const ShaderGraphData &graph, double varying) {
  std::map<std::string, const ShaderNodeData *> nodes;
  for (const auto &n : graph.nodes)
    nodes[n.id] = &n;
  std::function<double(const ShaderEndpoint &)> evaluate;
  const auto input = [&](const ShaderNodeData &n, const char *socket, double fallback) {
    for (const auto &link : graph.links)
      if (link.to.node == n.id && link.to.socket == socket)
        return evaluate(link.from);
    if (const auto p = n.parameters.find(socket); p != n.parameters.end())
      return std::visit(
          [](const auto &v) -> double {
            using T = std::decay_t<decltype(v)>;
            if constexpr (std::is_arithmetic_v<T>)
              return double(v);
            throw std::runtime_error("Nonscalar parameter");
          },
          p->second);
    return fallback;
  };
  evaluate = [&](const ShaderEndpoint &endpoint) {
    const auto &n = *nodes.at(endpoint.node);
    if (n.definition == "cycles/attribute/1")
      return varying;
    if (n.definition == "cycles/value/1")
      return input(n, "value", 0);
    if (n.definition == "cycles/integer_math/1")
      return input(n, "value1", 0) + input(n, "value2", 0);
    if (n.definition == "cycles/boolean_math/1")
      return double(input(n, "boolean1", 0) != 0 || input(n, "boolean2", 0) != 0);
    if (n.definition == "cycles/math/1") {
      const auto op = std::get<std::string>(n.parameters.at("math_type"));
      const double a = input(n, "value1", 0), b = input(n, "value2", 0);
      if (op == "greater_than")
        return double(a > b);
      if (op == "trunc")
        return std::trunc(a);
      if (op == "add")
        return a + b;
      throw std::runtime_error("Unknown math op");
    }
    if (n.definition == "cycles/mix_float/1") {
      const auto factor = std::clamp(input(n, "fac", .5), 0., 1.);
      return (1 - factor) * input(n, "a", 0) + factor * input(n, "b", 0);
    }
    throw std::runtime_error("Unknown lowered node");
  };
  return evaluate(graph.surface);
}
int main() {
  require(shader_switch_definitions().size() == 19, "Typed selector and side-effect catalogue");
  int checks = 0;
  for (const auto &type : {"float", "integer", "boolean", "vector", "color", "shader"}) {
    ShaderGraphData graph;
    graph.nodes = {{"s", std::string("cycles/switch_") + type + "/1", {{"switch", false}}},
                   {"invalid", "invalid/dormant/1", {}}};
    graph.links = {{{"invalid", "invalid"}, {"s", "true"}}};
    graph.surface = {"s", "output"};
    const auto lower = lower_shader_switches(graph);
    require(std::none_of(lower.nodes.begin(), lower.nodes.end(), [](const auto &n) { return n.id == "invalid"; }),
            "Static selector retained inactive source");
    ++checks;
  }
  ShaderGraphData scalar;
  scalar.nodes = {{"s", "cycles/switch_integer/1", {{"switch", true}, {"false", 1}, {"true", 16777217}}}};
  scalar.surface = {"s", "output"};
  const auto exact = lower_shader_switches(scalar);
  require(std::get<int>(exact.nodes.back().parameters.at("value1")) == 16777217,
          "Static selection lost int32 precision");
  ++checks;
  scalar.nodes = {{"s", "cycles/switch_float/1", {{"false", .25f}, {"true", .75f}}},
                  {"varying", "cycles/attribute/1", {}}};
  scalar.links = {{{"varying", "fac"}, {"s", "switch"}}};
  const auto dynamic = lower_shader_switches(scalar);
  for (double x : {-2., -.001, 0., .001, .5, 1., 2.}) {
    require(std::abs(run(dynamic, x) - (x > 0 ? .75 : .25)) < 1e-9,
            "Varying Switch differs from upstream >0 conversion");
    ++checks;
  }
  ShaderGraphData index;
  index.nodes = {
      {"s", "cycles/index_switch_float/1", {{"item_count", 3}, {"item0", .2f}, {"item1", .4f}, {"item2", .8f}}},
      {"varying", "cycles/attribute/1", {}}};
  index.surface = {"s", "output"};
  index.links = {{{"varying", "fac"}, {"s", "index"}}};
  const auto indexed = lower_shader_switches(index);
  for (double x : {-3.4, -1.1, -.9, 0., .4, .9, 1., 1.9, 2., 2.9, 3., 4.}) {
    const auto i = int(x);
    const double expected = i == 0 ? .2 : i == 1 ? .4 : i == 2 ? .8 : 0;
    require(std::abs(run(indexed, x) - expected) < 1e-7, "Index truncation or fallback differs from oracle");
    ++checks;
  }
  index.links.clear();
  index.nodes.pop_back();
  index.nodes.front().parameters = {{"item_count", 4097}, {"index", 4096}, {"item4096", .625f}};
  require(run(lower_shader_switches(index), 0) == .625, "Native input limit replaced by artificial small cap");
  ++checks;
  index.nodes.front().parameters["index"] = -1;
  std::vector<SourceDiagnostic> diagnostics;
  require(run(lower_shader_switches(index, &diagnostics), 0) == 0 && diagnostics.size() == 1,
          "Out-of-range static fallback/diagnostic");
  ++checks;
  index.nodes.front().parameters = {{"item_count", 0}};
  require(run(lower_shader_switches(index), 0) == 0, "Zero-item fallback");
  ++checks;
  index.nodes.front().parameters = {{"item_count", 9999}};
  bool rejected = false;
  try {
    lower_shader_switches(index);
  } catch (const std::invalid_argument &) {
    rejected = true;
  }
  require(rejected, "Houdini socket budget not enforced");
  ++checks;
  index.nodes.front().parameters = {{"item_count", 128}};
  index.nodes.push_back({"varying", "cycles/attribute/1", {}});
  index.links = {{{"varying", "fac"}, {"s", "index"}}};
  rejected = false;
  try {
    lower_shader_switches(index);
  } catch (const std::invalid_argument &) {
    rejected = true;
  }
  require(rejected, "Expanded engine node budget not enforced");
  ++checks;
  index.nodes.front().definition = "cycles/index_switch_shader/1";
  index.nodes.front().parameters = {{"item_count", 2}};
  const auto closures = lower_shader_switches(index);
  require(std::count_if(closures.nodes.begin(), closures.nodes.end(),
                        [](const auto &n) { return n.definition == "cycles/mix_closure/1"; }) == 3,
          "Shader selection must use Mix Shader");
  ++checks;
  ShaderGraphData aov;
  aov.nodes = {{"surface", "cycles/value/1", {{"value", .2f}}},
               {"aov_select", "cycles/switch_float/1", {{"switch", true}, {"true", .7f}}},
               {"writer", "cycles/aov_output/1", {{"name", std::string("selected")}}}};
  aov.surface = {"surface", "value"};
  aov.links = {{{"aov_select", "output"}, {"writer", "value"}}};
  const auto lowered_aov = lower_shader_switches(aov);
  require(
      std::any_of(lowered_aov.nodes.begin(), lowered_aov.nodes.end(), [](const auto &n) { return n.id == "writer"; }),
      "Independent AOV writer was pruned");
  require(std::any_of(lowered_aov.links.begin(), lowered_aov.links.end(),
                      [](const auto &l) { return l.to.node == "writer"; }),
          "Independent AOV selector was disconnected");
  ++checks;
  ShaderGraphData enabled;
  enabled.nodes = {{"e", "cycles/enable_output_float/1", {{"enable", false}}}, {"bad", "invalid/dormant/1", {}}};
  enabled.links = {{{"bad", "missing"}, {"e", "value"}}};
  enabled.surface = {"e", "output"};
  require(run(lower_shader_switches(enabled), 0) == 0, "Disabled Enable Output evaluated its dormant branch");
  ++checks;
  enabled.links.clear();
  enabled.nodes.pop_back();
  enabled.nodes[0].parameters = {{"enable", true}, {"value", .875f}};
  require(run(lower_shader_switches(enabled), 0) == .875, "Enabled output changed primitive value");
  ++checks;
  enabled.nodes.push_back(
      {"warning",
       "cycles/warning/1",
       {{"show", true}, {"message", std::string("Independent warning")}, {"severity", std::string("info")}}});
  diagnostics.clear();
  lower_shader_switches(enabled, &diagnostics);
  require(diagnostics.size() == 1 && diagnostics[0].message == "Independent warning" &&
              diagnostics[0].severity == "info",
          "Unconnected Warning side effect was omitted");
  ++checks;
  enabled.nodes.back().parameters["show"] = false;
  diagnostics.clear();
  lower_shader_switches(enabled, &diagnostics);
  require(diagnostics.empty(), "False Warning emitted a message");
  ++checks;
  std::cout << "{\"result\":\"passed\",\"checks\":" << checks
            << ",\"native_maximum_items\":9998,\"upstream_commits\":[\"b4174fbb1b\",\"f5381d8e31d\"]}\n";
}
